import importlib.util
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_catalog", BASE / "lib/image_catalog.py")
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class UpstreamTests(unittest.TestCase):
    def test_network_error_distinct(self):
        with mock.patch.object(catalog, "_probe_url", side_effect=catalog.UpstreamNetworkError("timeout")):
            with self.assertRaises(ValueError):
                catalog.check_upstream(BASE / "catalog/os-images.tsv")

    def test_probe_ok(self):
        with mock.patch.object(catalog, "_probe_url", return_value=200):
            count = catalog.check_upstream(BASE / "catalog/os-images.tsv")
            self.assertEqual(count, 8)


if __name__ == "__main__":
    unittest.main()
