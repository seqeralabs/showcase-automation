import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seqerakit import seqeraplatform  # noqa: E402

import studios_autotest as sa  # noqa: E402

JUPYTER = "public.cr.seqera.io/platform/data-studio-jupyter"
WORKFLOW = ROOT / ".github/workflows/seqera-showcase-studios-staging.yml"


def dto(
    status: str | None,
    message: str | None = None,
    stop_reason: str | None = None,
    progress: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a `tw -o json studios view` response."""
    return {
        "studio": {
            "sessionId": "sess-1",
            "statusInfo": {
                "status": status,
                "message": message,
                "stopReason": stop_reason,
            },
            "progress": progress or [],
        },
        "workspaceRef": "org/staging",
    }


class FakeClock:
    """A monotonic clock advanced only by `sleep`."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSeqera:
    """Stand-in for seqerakit.SeqeraPlatform recording `tw studios` calls.

    `responses` maps a sub-command to a list of responses consumed in order;
    the last one repeats. An Exception instance is raised instead of returned.
    """

    def __init__(self, responses: dict[str, list[Any]], dryrun: bool = False) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.dryrun = dryrun
        self.calls: list[tuple[str, ...]] = []

    def studios(self, *args: str) -> Any:
        self.calls.append(args)
        if self.dryrun:
            return None
        queue = self.responses[args[0]]
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response

    @contextmanager
    def suppress_output(self):
        yield

    def subcommands(self) -> list[str]:
        return [call[0] for call in self.calls]


def spec(**overrides: Any) -> sa.StudioSpec:
    values: dict[str, Any] = {
        "ref": "aws-cloud",
        "name": "jupyter",
        "workspace": "14715071736572",
        "compute_env": "seqera_aws_cloud",
        "template": JUPYTER,
    }
    values.update(overrides)
    return sa.StudioSpec(**values)


def launch_record(**overrides: Any) -> sa.StudioLaunch:
    values: dict[str, Any] = {
        "ref": "aws-cloud",
        "studioName": "jupyter-aws-cloud-20250101-abc",
        "workspaceId": "14715071736572",
        "workspaceRef": "org/staging",
        "computeEnvironment": "seqera_aws_cloud",
        "template": f"{JUPYTER}:4.6.0-0.12",
        "sessionId": "sess-1",
        "studioUrl": "https://staging.example/studios/sess-1/connect",
        "launchSuccess": True,
        "startupStatus": "running",
        "startupSeconds": 300.0,
    }
    values.update(overrides)
    return sa.StudioLaunch(**values)


class TemplateResolutionTest(unittest.TestCase):
    def test_split_template(self) -> None:
        self.assertEqual(
            sa.split_template(f"{JUPYTER}:4.6.0-0.12"), (JUPYTER, "4.6.0-0.12")
        )
        self.assertEqual(sa.split_template(JUPYTER), (JUPYTER, None))
        self.assertEqual(sa.split_template("host:5000/repo"), ("host:5000/repo", None))
        self.assertEqual(
            sa.split_template("host:5000/repo:1.0-0.1"), ("host:5000/repo", "1.0-0.1")
        )

    def test_parse_template_tag_orders_versions(self) -> None:
        tags = ["4.6.0-0.12", "latest", "4.4.1-u1-0.7", "4.6.0-0.9", "4.2.5-0.8"]
        self.assertEqual(
            sorted(tags, key=sa.parse_template_tag),
            ["latest", "4.2.5-0.8", "4.4.1-u1-0.7", "4.6.0-0.9", "4.6.0-0.12"],
        )

    def test_resolve_template_returns_pinned_template_unchanged(self) -> None:
        self.assertEqual(
            sa.resolve_template(f"{JUPYTER}:4.2.5-0.8", []), f"{JUPYTER}:4.2.5-0.8"
        )

    def test_resolve_template_prefers_recommended_then_newest(self) -> None:
        available = [
            {"repository": f"{JUPYTER}:4.6.0-0.12", "status": "experimental"},
            {"repository": f"{JUPYTER}:4.4.1-0.10", "status": "recommended"},
            {"repository": f"{JUPYTER}:4.5.0-0.11", "status": "recommended"},
            {"repository": f"{JUPYTER}:4.7.0-0.13", "status": "unsupported"},
            {
                "repository": "public.cr.seqera.io/platform/data-studio-ride:2024.12.0-0.12"
            },
        ]
        self.assertEqual(
            sa.resolve_template(JUPYTER, available), f"{JUPYTER}:4.5.0-0.11"
        )

    def test_resolve_template_falls_back_to_newest_without_recommended(self) -> None:
        available = [
            {"repository": f"{JUPYTER}:4.6.0-0.12", "status": "deprecated"},
            {"repository": f"{JUPYTER}:4.6.0-0.13", "status": "deprecated"},
        ]
        self.assertEqual(
            sa.resolve_template(JUPYTER, available), f"{JUPYTER}:4.6.0-0.13"
        )

    def test_resolve_template_missing_raises(self) -> None:
        available = [{"repository": f"{JUPYTER}:4.6.0-0.12", "status": "unsupported"}]
        with self.assertRaises(sa.TemplateNotFoundError):
            sa.resolve_template(JUPYTER, available)


class SpecTest(unittest.TestCase):
    def test_studio_name_is_unique_and_safe(self) -> None:
        name = sa.studio_name(spec(name="Jupyter Lab", ref="aws_cloud"))
        self.assertTrue(name.startswith("Jupyter-Lab-aws-cloud-"))
        self.assertIn(sa.date, name)
        self.assertIn(sa.run_uuid, name)
        self.assertRegex(name, r"^[A-Za-z0-9-]+$")
        self.assertLessEqual(len(name), 80)

    def test_build_add_args(self) -> None:
        args = sa.build_add_args(
            spec(lifespan=2, mount_data_uris=["s3://a", "s3://b"], private=True),
            "jupyter-aws-cloud-1",
            f"{JUPYTER}:4.6.0-0.12",
            labels="automation=showcase",
            description="test studio",
        )
        self.assertEqual(args[0], "add")
        for option, value in (
            ("-n", "jupyter-aws-cloud-1"),
            ("-w", "14715071736572"),
            ("-c", "seqera_aws_cloud"),
            ("-t", f"{JUPYTER}:4.6.0-0.12"),
            ("--cpu", "2"),
            ("--memory", "8192"),
            ("--gpu", "0"),
            ("--lifespan", "2"),
            ("--mount-data-uris", "s3://a,s3://b"),
            ("-d", "test studio"),
            ("--labels", "automation=showcase"),
        ):
            self.assertEqual(args[args.index(option) + 1], value)
        self.assertIn("--auto-start", args)
        self.assertIn("--private", args)

    def test_build_add_args_omits_optional_options(self) -> None:
        args = sa.build_add_args(spec(), "name", f"{JUPYTER}:4.6.0-0.12")
        for option in (
            "--lifespan",
            "--mount-data-uris",
            "--private",
            "-d",
            "--labels",
        ):
            self.assertNotIn(option, args)

    def test_read_yaml_loads_staging_config(self) -> None:
        specs = sa.read_yaml([ROOT / "studios/staging-aws-cloud.yaml"])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].workspace, "14715071736572")
        self.assertEqual(specs[0].compute_env, "seqera_aws_cloud")
        self.assertTrue(
            specs[0].template.startswith("public.cr.seqera.io/platform/data-studio-")
        )
        self.assertIsNotNone(specs[0].lifespan)

    def test_read_yaml_rejects_unknown_key(self) -> None:
        with self.assertRaises(KeyError):
            sa.read_yaml([ROOT / "compute-envs/staging-aws-cloud.yaml"])


class WaitForStatusTest(unittest.TestCase):
    def test_returns_when_condition_is_met(self) -> None:
        clock = FakeClock()
        seqera = FakeSeqera(
            {"view": [dto("starting"), dto("starting"), dto("running")]}
        )
        observation = sa.wait_for_status(
            seqera,
            "sess-1",
            "ws",
            until=lambda status: status == "running",
            timeout_seconds=600,
            poll_seconds=30,
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertEqual(observation.status, "running")
        self.assertFalse(observation.timedOut)
        self.assertEqual(observation.elapsedSeconds, 60)
        self.assertEqual(len(seqera.calls), 3)

    def test_times_out(self) -> None:
        clock = FakeClock()
        seqera = FakeSeqera({"view": [dto("starting", message="pulling image")]})
        observation = sa.wait_for_status(
            seqera,
            "sess-1",
            "ws",
            until=lambda status: status == "running",
            timeout_seconds=100,
            poll_seconds=60,
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertTrue(observation.timedOut)
        self.assertEqual(observation.status, "starting")
        self.assertEqual(observation.message, "pulling image")

    def test_retries_after_cli_error(self) -> None:
        clock = FakeClock()
        seqera = FakeSeqera(
            {"view": [seqeraplatform.CommandError("502 Bad Gateway"), dto("running")]}
        )
        observation = sa.wait_for_status(
            seqera,
            "sess-1",
            "ws",
            until=lambda status: status == "running",
            timeout_seconds=600,
            poll_seconds=30,
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertEqual(observation.status, "running")
        self.assertFalse(observation.timedOut)


class LaunchStudioTest(unittest.TestCase):
    def launch(self, seqera: FakeSeqera, **overrides: Any) -> sa.StudioLaunch:
        clock = FakeClock()
        return sa.launch_studio(
            seqera,
            spec(**overrides),
            labels="automation=showcase",
            startup_timeout_seconds=1800,
            poll_seconds=60,
            templates_cache={},
            clock=clock,
            sleep=clock.sleep,
        )

    def test_records_running_studio(self) -> None:
        seqera = FakeSeqera(
            {
                "templates": [
                    {
                        "templates": [
                            {
                                "repository": f"{JUPYTER}:4.6.0-0.12",
                                "status": "recommended",
                            }
                        ]
                    }
                ],
                "add": [
                    {
                        "sessionId": "sess-1",
                        "studioUrl": "https://staging.example/studios/sess-1/connect",
                        "workspaceRef": "org/staging",
                        "workspaceId": 14715071736572,
                        "autoStart": True,
                    }
                ],
                "view": [dto("starting"), dto("running")],
            }
        )
        record = self.launch(seqera)
        self.assertTrue(record.launchSuccess)
        self.assertEqual(record.sessionId, "sess-1")
        self.assertEqual(record.template, f"{JUPYTER}:4.6.0-0.12")
        self.assertEqual(record.startupStatus, "running")
        self.assertEqual(record.startupSeconds, 60)
        self.assertEqual(record.error, "")
        self.assertEqual(seqera.subcommands(), ["templates", "add", "view", "view"])
        add_args = seqera.calls[1]
        self.assertEqual(add_args[add_args.index("-t") + 1], f"{JUPYTER}:4.6.0-0.12")
        self.assertEqual(
            add_args[add_args.index("--labels") + 1], "automation=showcase"
        )
        self.assertIn("--auto-start", add_args)

    def test_records_create_failure_without_polling(self) -> None:
        seqera = FakeSeqera(
            {
                "templates": [{"templates": [{"repository": f"{JUPYTER}:4.6.0-0.12"}]}],
                "add": [
                    seqeraplatform.CommandError(
                        "Command failed: 'ERROR: Compute environment not found'"
                    )
                ],
            }
        )
        record = self.launch(seqera)
        self.assertFalse(record.launchSuccess)
        self.assertIsNone(record.sessionId)
        self.assertIn("Compute environment not found", record.error)
        self.assertNotIn("view", seqera.subcommands())

    def test_records_startup_failure(self) -> None:
        seqera = FakeSeqera(
            {
                "templates": [{"templates": [{"repository": f"{JUPYTER}:4.6.0-0.12"}]}],
                "add": [{"sessionId": "sess-1"}],
                "view": [dto("starting"), dto("errored", message="instance failed")],
            }
        )
        record = self.launch(seqera)
        self.assertTrue(record.launchSuccess)
        self.assertEqual(record.startupStatus, "errored")
        self.assertEqual(record.statusMessage, "instance failed")

    def test_records_missing_template(self) -> None:
        seqera = FakeSeqera({"templates": [{"templates": []}]})
        record = self.launch(seqera)
        self.assertFalse(record.launchSuccess)
        self.assertIn("No usable Studio template", record.error)
        self.assertNotIn("add", seqera.subcommands())

    def test_dryrun_only_prints_the_add_command(self) -> None:
        seqera = FakeSeqera({}, dryrun=True)
        record = self.launch(seqera)
        self.assertTrue(record.dryrun)
        self.assertEqual(record.startupStatus, sa.OUTCOME_DRYRUN)
        self.assertEqual(record.template, JUPYTER)
        self.assertEqual(seqera.subcommands(), ["add"])


class FinishStudioTest(unittest.TestCase):
    def finish(
        self,
        seqera: FakeSeqera,
        launch: sa.StudioLaunch,
        delete: bool = True,
        force: bool = False,
    ) -> sa.StudioResult:
        clock = FakeClock()
        return sa.finish_studio(
            seqera,
            launch,
            delete=delete,
            force=force,
            stop_timeout_seconds=100,
            poll_seconds=60,
            clock=clock,
            sleep=clock.sleep,
        )

    def test_passed_studio_is_stopped_and_deleted(self) -> None:
        seqera = FakeSeqera(
            {
                "view": [dto("running"), dto("stopping"), dto("stopped")],
                "stop": [{"sessionId": "sess-1", "jobSubmitted": True}],
                "delete": [{"userSuppliedStudioIdentifier": "sess-1"}],
            }
        )
        result = self.finish(seqera, launch_record())
        self.assertEqual(result.outcome, sa.OUTCOME_PASSED)
        self.assertEqual(result.checkStatus, "running")
        self.assertEqual(result.stopStatus, "stopped")
        self.assertTrue(result.deleted)
        self.assertEqual(result.outcomeDetail, "")
        self.assertEqual(
            seqera.subcommands(), ["view", "stop", "view", "view", "delete"]
        )

    def test_studio_stopped_during_soak_fails_but_is_deleted(self) -> None:
        seqera = FakeSeqera(
            {
                "view": [dto("stopped", stop_reason="LIFESPAN_EXPIRED")],
                "delete": [{}],
            }
        )
        result = self.finish(seqera, launch_record())
        self.assertEqual(result.outcome, sa.OUTCOME_FAILED_SOAK)
        self.assertIn("LIFESPAN_EXPIRED", result.outcomeDetail)
        self.assertTrue(result.deleted)
        self.assertNotIn("stop", seqera.subcommands())

    def test_studio_that_never_started_is_reported_and_deleted(self) -> None:
        seqera = FakeSeqera({"view": [dto("errored", message="boom")], "delete": [{}]})
        result = self.finish(
            seqera, launch_record(startupStatus="errored", statusMessage="boom")
        )
        self.assertEqual(result.outcome, sa.OUTCOME_FAILED_TO_START)
        self.assertIn("boom", result.outcomeDetail)
        self.assertTrue(result.deleted)

    def test_late_start_is_still_a_failure_but_gets_stopped(self) -> None:
        seqera = FakeSeqera(
            {
                "view": [dto("running"), dto("stopped")],
                "stop": [{"jobSubmitted": True}],
                "delete": [{}],
            }
        )
        result = self.finish(seqera, launch_record(startupStatus=sa.STATUS_TIMEOUT))
        self.assertEqual(result.outcome, sa.OUTCOME_FAILED_TO_START)
        self.assertEqual(result.stopStatus, "stopped")
        self.assertTrue(result.deleted)

    def test_stop_timeout_leaves_studio_unless_forced(self) -> None:
        responses = {
            "view": [dto("running"), dto("stopping")],
            "stop": [{"jobSubmitted": True}],
            "delete": [{}],
        }
        result = self.finish(FakeSeqera(responses), launch_record())
        self.assertEqual(result.outcome, sa.OUTCOME_FAILED_TO_STOP)
        self.assertEqual(result.stopStatus, sa.STATUS_TIMEOUT)
        self.assertFalse(result.deleted)
        self.assertIn("--force", result.outcomeDetail)

        forced = self.finish(FakeSeqera(responses), launch_record(), force=True)
        self.assertEqual(forced.outcome, sa.OUTCOME_FAILED_TO_STOP)
        self.assertTrue(forced.deleted)

    def test_delete_error_is_recorded_without_changing_outcome(self) -> None:
        seqera = FakeSeqera(
            {
                "view": [dto("running"), dto("stopped")],
                "stop": [{"jobSubmitted": True}],
                "delete": [seqeraplatform.CommandError("Command failed: 'ERROR: 502'")],
            }
        )
        result = self.finish(seqera, launch_record())
        self.assertEqual(result.outcome, sa.OUTCOME_PASSED)
        self.assertFalse(result.deleted)
        self.assertIn("delete failed", result.outcomeDetail)

    def test_no_delete_flag_keeps_studio(self) -> None:
        seqera = FakeSeqera(
            {"view": [dto("running"), dto("stopped")], "stop": [{"jobSubmitted": True}]}
        )
        result = self.finish(seqera, launch_record(), delete=False)
        self.assertEqual(result.outcome, sa.OUTCOME_PASSED)
        self.assertFalse(result.deleted)
        self.assertNotIn("delete", seqera.subcommands())

    def test_failed_to_create_and_dryrun_make_no_calls(self) -> None:
        seqera = FakeSeqera({})
        failed = self.finish(
            seqera, launch_record(sessionId=None, launchSuccess=False, error="no CE")
        )
        self.assertEqual(failed.outcome, sa.OUTCOME_FAILED_TO_CREATE)
        self.assertEqual(failed.outcomeDetail, "no CE")
        dryrun = self.finish(seqera, launch_record(sessionId=None, dryrun=True))
        self.assertEqual(dryrun.outcome, sa.OUTCOME_DRYRUN)
        self.assertEqual(seqera.calls, [])


class ReportTest(unittest.TestCase):
    def test_markdown_summary_lists_failures_first_and_links_kept_studios(self) -> None:
        passed = sa.StudioResult(
            **launch_record(studioName="a-passed").model_dump(), deleted=True
        )
        failed = sa.StudioResult(
            **launch_record(
                studioName="z-failed", startupStatus="errored"
            ).model_dump(),
            outcome=sa.OUTCOME_FAILED_TO_START,
            outcomeDetail="startup ended with status 'errored' | boom",
        )
        summary = sa.build_markdown_summary([passed, failed])
        rows = [
            line
            for line in summary.splitlines()
            if line.startswith("| ") and not line.startswith("| ---")
        ]
        self.assertEqual(len(rows), 3)  # header + two studios
        self.assertTrue(rows[1].startswith("| [z-failed](https://"))
        self.assertTrue(rows[2].startswith("| a-passed |"))
        self.assertIn("FAILED_TO_START", rows[1])
        self.assertIn("\\|", rows[1])  # pipes inside details are escaped
        self.assertIn("2 studios: 1 passed, 1 failed, 0 other", summary)
        self.assertEqual(
            sa.build_summary([passed, failed]),
            {"total": 2, "passed": 1, "failed": 1, "other": 0},
        )

    def test_step_summary_is_written_only_inside_github_actions(self) -> None:
        result = sa.StudioResult(**launch_record().model_dump())
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.md"
            with mock.patch.dict(
                os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}
            ):
                sa.write_step_summary([result])
            self.assertIn("| Studio |", summary_path.read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {}, clear=True):
            sa.write_step_summary([result])  # nothing to write to, no error


class CliTest(unittest.TestCase):
    def test_parse_launch_and_finish_arguments(self) -> None:
        launch = sa.parse_args(
            [
                "launch",
                "-l",
                "DEBUG",
                "-i",
                "studios/staging-aws-cloud.yaml",
                "-o",
                "out.json",
                "--dryrun",
            ]
        )
        self.assertEqual(launch.command, "launch")
        self.assertEqual(launch.log_level, "DEBUG")
        self.assertTrue(launch.dryrun)
        self.assertEqual(launch.startup_timeout, 30)

        finish = sa.parse_args(
            [
                "finish",
                "-i",
                "a.json",
                "b.json",
                "-o",
                "out.json",
                "--delete",
                "--fail-on-error",
            ]
        )
        self.assertEqual(finish.command, "finish")
        self.assertEqual(finish.input, ["a.json", "b.json"])
        self.assertTrue(finish.delete and finish.fail_on_error)
        self.assertFalse(finish.force)


class WorkflowTest(unittest.TestCase):
    def test_workflow_targets_staging_and_always_cleans_up(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow["name"], "seqera-showcase-autotest-studios-staging")
        self.assertIn(
            "STAGING_TOWER_ACCESS_ENDPOINT", workflow["env"]["TOWER_API_ENDPOINT"]
        )

        launch, finish = (
            workflow["jobs"]["launch"],
            workflow["jobs"]["soak-stop-and-delete"],
        )
        self.assertIn("STAGING_TOWER_ACCESS_TOKEN", launch["env"]["TOWER_ACCESS_TOKEN"])
        self.assertIn("STAGING_TOWER_ACCESS_TOKEN", finish["env"]["TOWER_ACCESS_TOKEN"])
        self.assertEqual(finish["needs"], ["launch"])
        self.assertTrue(finish["if"].startswith("always()"))
        self.assertIn("inputs.timer", finish["environment"])

        launch_run = next(
            s["run"] for s in launch["steps"] if s.get("name") == "launch_studios"
        )
        self.assertIn("studios_autotest.py launch", launch_run)
        self.assertIn("studios/staging*.yaml", launch_run)
        finish_run = next(
            s["run"] for s in finish["steps"] if s.get("name") == "finish studios"
        )
        self.assertIn("studios_autotest.py finish", finish_run)
        self.assertIn("--fail-on-error", finish_run)

        # Studios only accept resource labels, which must be key=value pairs.
        default_labels = re.search(r"\|\| '--labels \"([^\"]+)\"'", launch_run)
        self.assertIsNotNone(default_labels)
        self.assertIn("=", default_labels.group(1))

        # The dev/staging config test relies on this workflow not reusing compute-envs globs.
        self.assertNotIn("compute-envs/", launch_run)


if __name__ == "__main__":
    unittest.main()
