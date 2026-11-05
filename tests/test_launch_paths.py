import unittest
from unittest import mock

import launch_pipelines
from launch_pipelines import ComputeEnvironment, LaunchConfig, Pipeline, date


class FakeSeqera:
    dryrun = True

    def launch(self, *args, to_json: bool = False) -> dict:
        return {}


def launch_args(ref: str) -> dict:
    config = LaunchConfig(
        pipeline=Pipeline(
            name="viralrecon", url="nf-core/viralrecon", latest=False, profiles=[]
        ),
        compute_environment=ComputeEnvironment(
            ref=ref, name=ref, workdir="gs://shared-bucket", workspace="1"
        ),
    )
    with mock.patch.object(
        launch_pipelines,
        "parse_launch_block",
        wraps=launch_pipelines.parse_launch_block,
    ) as parse:
        config.launch_pipeline(FakeSeqera())
    return parse.call_args.args[0]


class LaunchPathsTest(unittest.TestCase):
    def test_workdir_and_outdir_include_compute_env_ref(self) -> None:
        args = launch_args("gcp-wif")
        self.assertEqual(
            args["work-dir"], f"gs://shared-bucket/viralrecon/gcp-wif/work-{date}"
        )
        self.assertEqual(
            args["params"]["outdir"],
            f"gs://shared-bucket/viralrecon/gcp-wif/results-test-{date}/",
        )

    def test_compute_envs_sharing_a_bucket_get_distinct_paths(self) -> None:
        wif, cloud = launch_args("gcp-wif"), launch_args("gcp-cloud")
        self.assertNotEqual(wif["work-dir"], cloud["work-dir"])
        self.assertNotEqual(wif["params"]["outdir"], cloud["params"]["outdir"])


if __name__ == "__main__":
    unittest.main()
