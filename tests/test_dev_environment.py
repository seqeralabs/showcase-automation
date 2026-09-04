from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative_path: str) -> object:
    with (ROOT / relative_path).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class DevEnvironmentConfigTest(unittest.TestCase):
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
