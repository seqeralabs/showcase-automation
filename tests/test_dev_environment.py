from pathlib import Path
from typing import cast
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative_path: str) -> dict[str, object]:
    with (ROOT / relative_path).open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"Expected a YAML mapping in {relative_path}")
    return cast(dict[str, object], value)


class DevEnvironmentConfigTest(unittest.TestCase):
    def assert_dev_workspace_copy(
        self, dev_path: str, staging_path: str
    ) -> None:
        expected = load_yaml(staging_path)
        compute_environments = expected["compute-envs"]
        if not isinstance(compute_environments, list) or not all(
            isinstance(compute_environment, dict)
            for compute_environment in compute_environments
        ):
            raise TypeError(f"Expected compute-envs list in {staging_path}")
        for compute_environment in compute_environments:
            compute_environment["workspace"] = "86340901303136"

        self.assertTrue((ROOT / dev_path).is_file())
        self.assertEqual(load_yaml(dev_path), expected)
        expected_text = (ROOT / staging_path).read_text(encoding="utf-8").replace(
            'workspace: "14715071736572"',
            'workspace: "86340901303136"',
        )
        self.assertEqual(
            (ROOT / dev_path).read_text(encoding="utf-8"), expected_text
        )

    def test_compute_environment_configs_match_staging_with_dev_workspace(
        self,
    ) -> None:
        compute_env_dir = ROOT / "compute-envs"
        staging_paths = sorted(compute_env_dir.glob("staging-*.yaml"))
        self.assertEqual(len(staging_paths), 11)
        expected_dev_names = {
            staging_path.name.replace("staging-", "dev-", 1)
            for staging_path in staging_paths
        }
        self.assertEqual(
            {dev_path.name for dev_path in compute_env_dir.glob("dev-*.yaml")},
            expected_dev_names,
        )

        for staging_path in staging_paths:
            dev_name = staging_path.name.replace("staging-", "dev-", 1)
            with self.subTest(dev_name=dev_name):
                self.assert_dev_workspace_copy(
                    f"compute-envs/{dev_name}",
                    f"compute-envs/{staging_path.name}",
                )

        self.assert_dev_workspace_copy(
            "compute-envs/sched-dev.yaml",
            "compute-envs/sched-staging.yaml",
        )

    def test_pipeline_configs_match_staging(self) -> None:
        path_pairs = {
            "pipelines/dev-hello.yaml": "pipelines/staging-hello.yaml",
            "pipelines/dev-nf-core-pipelines.yaml": "pipelines/staging-nf-core-pipelines.yaml",
        }
        for dev_path, staging_path in path_pairs.items():
            with self.subTest(dev_path=dev_path):
                self.assertTrue((ROOT / dev_path).is_file())
                self.assertEqual(load_yaml(dev_path), load_yaml(staging_path))


if __name__ == "__main__":
    unittest.main()
