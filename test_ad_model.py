import unittest
from unittest.mock import patch

from ad_model import OPTIONAL_SNAPSHOT_FILES, local_ad_model_snapshot


class LocalAdModelSnapshotTest(unittest.TestCase):
    def test_offline_snapshot_ignores_hub_metadata(self):
        with patch("ad_model.snapshot_download", return_value="/models/qwen") as download:
            path = local_ad_model_snapshot()

        self.assertEqual(path, "/models/qwen")
        self.assertEqual(download.call_args.args, ("mlx-community/Qwen2.5-7B-Instruct-4bit",))
        self.assertTrue(download.call_args.kwargs["local_files_only"])
        ignored = download.call_args.kwargs["ignore_patterns"]
        self.assertIn("README.md", ignored)
        self.assertIn(".gitattributes", ignored)
        self.assertEqual(ignored, OPTIONAL_SNAPSHOT_FILES)
