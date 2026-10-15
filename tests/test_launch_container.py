from pathlib import Path
import unittest

import yaml

from launch_pipelines import ComputeEnvironment

ROOT = Path(__file__).resolve().parents[1]


def compute_env(dind: bool = False) -> ComputeEnvironment:
    return ComputeEnvironment(
        ref="aws", name="ce", workdir="s3://bucket", workspace="1", dind=dind
    )


class LaunchContainerTest(unittest.TestCase):
    def test_dind_defaults_to_false(self) -> None:
        ce = ComputeEnvironment(ref="aws", name="ce", workdir="s3://b", workspace="1")
        self.assertFalse(ce.dind)

    def test_plain_compute_env_uses_plain_launcher(self) -> None:
        self.assertEqual(compute_env().get_launch_container("plain", "dind"), "plain")

    def test_dind_compute_env_uses_dind_launcher(self) -> None:
        self.assertEqual(
            compute_env(dind=True).get_launch_container("plain", "dind"), "dind"
        )

    def test_dind_compute_env_falls_back_to_plain_launcher(self) -> None:
        self.assertEqual(
            compute_env(dind=True).get_launch_container("plain", None), "plain"
        )

    def test_no_launcher_given(self) -> None:
        self.assertIsNone(compute_env(dind=True).get_launch_container(None, None))
        self.assertIsNone(compute_env().get_launch_container(None, "dind"))

    def test_cloud_and_sched_compute_envs_are_dind(self) -> None:
        paths = sorted((ROOT / "compute-envs").glob("*.yaml"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                envs = yaml.safe_load(path.read_text(encoding="utf-8"))["compute-envs"]
                expected = path.stem.endswith("-cloud") or path.stem.startswith("sched-")
                for env in envs:
                    self.assertEqual(env.get("dind", False), expected, env["ref"])


if __name__ == "__main__":
    unittest.main()
