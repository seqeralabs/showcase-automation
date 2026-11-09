import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import launch_pipelines
from launch_evidence import LaunchEvidence, write_json_atomic
from launch_pipelines import ComputeEnvironment, LaunchConfig, Pipeline

ROOT = Path(__file__).resolve().parents[1]


def configuration(name="hello", ref="aws", revision=None):
    return LaunchConfig(
        pipeline=Pipeline(
            name=name,
            url="nextflow-io/hello",
            latest=False,
            profiles=["test"],
            revision=revision,
        ),
        compute_environment=ComputeEnvironment(
            ref=ref,
            name=f"seqera_{ref}",
            workdir="s3://example",
            workspace="123",
        ),
    )


def receipt(workflow_id="run-1", success=True):
    return {"workflowId": workflow_id, "workspaceId": "123", "launchSuccess": success}


class LaunchEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "evidence.json"

    def read(self):
        return json.loads(self.path.read_text())

    def test_inventory_uses_resolved_matrix_with_includes_and_excludes(self):
        included = configuration("included_variant", "gcp", revision="v2")
        removed = configuration("excluded", "aws")
        matrix = {
            "pipelines": [
                configuration().pipeline.model_dump(),
                removed.pipeline.model_dump(),
            ],
            "compute-envs": [configuration().compute_environment.model_dump()],
            "include": [included.model_dump()],
            "exclude": [removed.model_dump()],
        }
        inputs = Path(self.directory.name) / "matrix.yaml"
        inputs.write_text(yaml.safe_dump(matrix))
        configs = launch_pipelines.read_yaml([inputs])
        with mock.patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": "seqeralabs/showcase-automation",
                "GITHUB_SHA": "a" * 40,
                "GITHUB_RUN_ID": "42",
                "GITHUB_RUN_ATTEMPT": "2",
            },
            clear=True,
        ):
            LaunchEvidence(self.path, configs, environment="production")
        data = self.read()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["coverage"]["expected"], 2)
        self.assertEqual(data["coverage"]["not_attempted"], 2)
        self.assertEqual(
            [c["pipeline"]["name"] for c in data["cases"]],
            ["hello", "included_variant"],
        )
        self.assertEqual(data["cases"][1]["pipeline"]["requested_revision"], "v2")
        self.assertEqual(data["cases"][1]["compute_environment"]["workspace"], "123")
        self.assertEqual(data["source"]["sha"], "a" * 40)
        self.assertEqual(data["source"]["run_attempt"], "2")
        self.assertEqual(data["source"]["run_id"], "42")
        self.assertIsNone(data["source"]["workflow_ref"])
        self.assertFalse(data["launch_phase_complete"])

    def test_interruption_keeps_receipts_unknown_attempt_and_unreached_cases(self):
        configs = [configuration(f"pipeline_{i}") for i in range(4)]
        evidence = LaunchEvidence(self.path, configs)
        calls = 0

        def launch(**kwargs):
            nonlocal calls
            data = self.read()
            self.assertEqual(data["cases"][calls]["launch_state"], "outcome_unknown")
            calls += 1
            if calls == 1:
                return receipt()
            if calls == 2:
                return receipt(None, False)
            raise RuntimeError("CLI response lost")

        with (
            mock.patch.object(LaunchConfig, "launch_pipeline", side_effect=launch),
            self.assertRaisesRegex(RuntimeError, "CLI response lost"),
        ):
            launch_pipelines.launch_pipelines(mock.Mock(), configs, evidence=evidence)
        data = self.read()
        self.assertEqual(
            [c["launch_state"] for c in data["cases"]],
            ["submitted", "outcome_unknown", "outcome_unknown", "not_attempted"],
        )
        self.assertEqual(data["cases"][0]["receipt"]["workflowId"], "run-1")
        self.assertIsNone(data["cases"][2]["receipt"])
        self.assertEqual(
            data["coverage"],
            {
                "expected": 4,
                "not_attempted": 1,
                "outcome_unknown": 2,
                "submitted": 1,
                "dry_run": 0,
            },
        )
        self.assertFalse(data["launch_phase_complete"])
        self.assertEqual(data["pipeline_health"], "not_assessed")

    def test_completed_launches_do_not_establish_pipeline_health(self):
        configs = [configuration()]
        evidence = LaunchEvidence(self.path, configs)
        with mock.patch.object(LaunchConfig, "launch_pipeline", return_value=receipt()):
            result = launch_pipelines.launch_pipelines(
                mock.Mock(), configs, evidence=evidence
            )
        self.assertEqual(result, [receipt()])
        self.assertTrue(self.read()["launch_phase_complete"])
        self.assertEqual(self.read()["pipeline_health"], "not_assessed")

    def test_cli_wait_error_preserves_uncertain_platform_outcome(self):
        configs = [configuration()]
        evidence = LaunchEvidence(self.path, configs)
        seqera = mock.Mock(dryrun=False)
        seqera.launch.side_effect = launch_pipelines.seqeraplatform.CommandError(
            "Connection reset while waiting for submitted run"
        )
        result = launch_pipelines.launch_pipelines(seqera, configs, evidence=evidence)
        self.assertFalse(result[0]["launchSuccess"])
        data = self.read()
        self.assertEqual(data["coverage"]["outcome_unknown"], 1)
        self.assertEqual(data["coverage"]["submitted"], 0)
        self.assertFalse(data["cases"][0]["receipt"]["launchSuccess"])
        self.assertEqual(
            data["cases"][0]["requested_workflow_name"], result[0]["workflowName"]
        )
        self.assertEqual(
            data["cases"][0]["receipt"]["workflowName"], result[0]["workflowName"]
        )
        self.assertTrue(data["launch_phase_complete"])
        self.assertEqual(data["pipeline_health"], "not_assessed")

    def test_dryrun_and_missing_workflow_id_are_not_submitted(self):
        for dryrun, response, expected in [
            (True, receipt(None, False), "dry_run"),
            (False, receipt(None, True), "outcome_unknown"),
            (False, receipt(None, False), "outcome_unknown"),
            (False, {}, "outcome_unknown"),
        ]:
            with self.subTest(expected=expected, dryrun=dryrun):
                evidence = LaunchEvidence(self.path, [configuration()], dryrun=dryrun)
                evidence.start(0)
                evidence.record(0, response)
                evidence.finish()
                self.assertEqual(self.read()["cases"][0]["launch_state"], expected)
                self.assertEqual(self.read()["coverage"]["submitted"], 0)
                self.assertEqual(self.read()["pipeline_health"], "not_assessed")

    def test_manifest_omits_raw_config_prerun_errors_and_unlisted_environment(self):
        config = configuration()
        config.pipeline.config = "SECRET_CONFIG"
        config.pipeline.pre_run = "SECRET_PRE_RUN"
        with mock.patch.dict(os.environ, {"TOWER_ACCESS_TOKEN": "SECRET_TOKEN"}):
            evidence = LaunchEvidence(self.path, [config])
        evidence.record(
            0,
            {**receipt(None, False), "error": "SECRET_ERROR", "other": "SECRET_EXTRA"},
        )
        serialized = self.path.read_text()
        for value in (
            "SECRET_CONFIG",
            "SECRET_PRE_RUN",
            "SECRET_TOKEN",
            "SECRET_ERROR",
            "SECRET_EXTRA",
        ):
            self.assertNotIn(value, serialized)
        self.assertEqual(len(self.read()["cases"][0]["configuration_sha256"]), 64)

    def test_pipeline_source_omits_url_credentials_query_and_fragment(self):
        for source, expected in [
            (
                "https://user:SECRET_CREDENTIAL@host/repo.git?access_token=SECRET_QUERY#SECRET_FRAGMENT",
                "https://host/repo.git",
            ),
            (
                "ssh://user:SECRET_CREDENTIAL@host:2222/repo.git",
                "ssh://host:2222/repo.git",
            ),
            ("git@github.com:org/repo.git", "github.com:org/repo.git"),
            ("nf-core/rnaseq", "nf-core/rnaseq"),
            ("https://[broken-host", None),
            ("https:/user:SECRET_CREDENTIAL@host/repo.git", None),
            ("https:user:SECRET_CREDENTIAL@host/repo.git", None),
            ("ssh:/user:SECRET_CREDENTIAL@host/repo.git", None),
        ]:
            with self.subTest(source=source):
                config = configuration()
                config.pipeline.url = source
                LaunchEvidence(self.path, [config])
                self.assertEqual(self.read()["cases"][0]["pipeline"]["url"], expected)
                self.assertNotIn("SECRET_", self.path.read_text())

    def test_variants_duplicate_entries_profiles_and_container_selection(self):
        first = configuration(revision="v1")
        second = configuration(revision="v2")
        second.compute_environment.dind = True
        second.compute_environment.profiles = ["docker"]
        LaunchEvidence(
            self.path,
            [first, second, first],
            launch_container="plain",
            launch_container_dind="dind",
        )
        cases = self.read()["cases"]
        self.assertEqual(len({c["case_id"] for c in cases}), 3)
        self.assertNotEqual(
            cases[0]["configuration_sha256"], cases[1]["configuration_sha256"]
        )
        self.assertEqual(
            cases[0]["configuration_sha256"], cases[2]["configuration_sha256"]
        )
        self.assertEqual(cases[1]["pipeline"]["profiles"], ["test", "docker"])
        self.assertEqual(
            [c["requested_launch_container"] for c in cases], ["plain", "dind", "plain"]
        )

    def test_failed_atomic_write_preserves_previous_checkpoint(self):
        write_json_atomic(self.path, {"previous": True})
        with (
            mock.patch("launch_evidence.os.replace", side_effect=OSError("disk error")),
            self.assertRaises(OSError),
        ):
            write_json_atomic(self.path, {"previous": False})
        self.assertEqual(self.read(), {"previous": True})
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_checkpoint_prevents_next_launch(self):
        configs = [configuration()]
        evidence = LaunchEvidence(self.path, configs)
        with (
            mock.patch(
                "launch_evidence.write_json_atomic", side_effect=OSError("disk full")
            ),
            mock.patch.object(LaunchConfig, "launch_pipeline") as launch,
            self.assertRaises(OSError),
        ):
            launch_pipelines.launch_pipelines(mock.Mock(), configs, evidence=evidence)
        launch.assert_not_called()

    def test_empty_inventory_never_assesses_health(self):
        evidence = LaunchEvidence(self.path, [])
        launch_pipelines.launch_pipelines(mock.Mock(), [], evidence=evidence)
        self.assertEqual(self.read()["coverage"]["expected"], 0)
        self.assertEqual(self.read()["pipeline_health"], "not_assessed")

    def test_cli_keeps_legacy_receipts_and_evidence_separate(self):
        legacy = self.path.parent / "receipts.json"
        arguments = [
            "launch_pipelines.py",
            "-i",
            "matrix.yaml",
            "-o",
            str(legacy),
            "--evidence-output",
            str(self.path),
            "--evidence-environment",
            "production",
        ]
        with (
            mock.patch("sys.argv", arguments),
            mock.patch.object(launch_pipelines.seqeraplatform, "SeqeraPlatform"),
            mock.patch.object(
                launch_pipelines, "read_yaml", return_value=[configuration()]
            ),
            mock.patch.object(LaunchConfig, "launch_pipeline", return_value=receipt()),
        ):
            launch_pipelines.main()
        self.assertEqual(json.loads(legacy.read_text()), [receipt()])
        self.assertEqual(self.read()["environment"], "production")
        self.assertTrue(self.read()["launch_phase_complete"])
        arguments[arguments.index(str(legacy))] = str(self.path)
        with (
            mock.patch("sys.argv", arguments),
            mock.patch.object(launch_pipelines.seqeraplatform, "SeqeraPlatform"),
            mock.patch.object(
                launch_pipelines, "read_yaml", return_value=[configuration()]
            ),
            self.assertRaisesRegex(ValueError, "separate files"),
        ):
            launch_pipelines.main()

    def test_production_uploads_separate_attempt_scoped_artifact_even_on_failure(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/seqera-showcase-production.yml").read_text()
        )
        launch = workflow["jobs"]["launch"]["steps"]
        command = next(s["run"] for s in launch if s.get("name") == "launch_pipelines")
        self.assertIn("--evidence-output launch_evidence.json", command)
        self.assertIn("--evidence-environment production", command)
        upload = next(
            s for s in launch if s.get("name") == "Retain launch coverage evidence"
        )
        self.assertEqual(upload["if"], "always()")
        self.assertIn("github.run_attempt", upload["with"]["name"])
        self.assertEqual(upload["with"]["path"], "launch_evidence.json")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        downloads = workflow["jobs"]["clearup-and-delete"]["steps"]
        download = next(
            s for s in downloads if "actions/download-artifact@" in s.get("uses", "")
        )
        self.assertTrue(download["with"]["name"].endswith("_launch_json"))


if __name__ == "__main__":
    unittest.main()
