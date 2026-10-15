import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_catalog", BASE / "lib/image_catalog.py")
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class UpstreamTests(unittest.TestCase):
    def test_network_error_distinct(self):
        with mock.patch.object(
            catalog,
            "_download_manifest",
            side_effect=catalog.UpstreamNetworkError("timeout"),
        ):
            with self.assertRaises(ValueError):
                catalog.check_upstream(BASE / "catalog/os-images.tsv")

    def test_manifest_digest_required(self):
        def fake_download(url):
            return 200, b"not a valid manifest\n"

        with mock.patch.object(catalog, "_download_manifest", side_effect=fake_download):
            with mock.patch.object(catalog, "_head_url", return_value=200):
                with self.assertRaises(ValueError):
                    catalog.check_upstream(BASE / "catalog/os-images.tsv")

    def test_ok_with_manifest_and_head(self):
        digest = "a" * 128
        body = f"{digest}  debian-12-genericcloud-amd64.qcow2\n".encode()

        def fake_download(url):
            return 200, body

        with mock.patch.object(catalog, "_download_manifest", side_effect=fake_download):
            with mock.patch.object(catalog, "_head_url", return_value=200):
                # Only first catalog entry needs to pass; mock all with same body works for debian-12
                # but other images will fail - patch load to one entry
                with mock.patch.object(
                    catalog,
                    "load_catalog",
                    return_value=[
                        [
                            "debian-12",
                            "Debian 12",
                            "debian",
                            "https://example.invalid",
                            "debian-12-genericcloud-amd64.qcow2",
                            "SHA512SUMS",
                            "sha512",
                        ]
                    ],
                ):
                    count = catalog.check_upstream(BASE / "catalog/os-images.tsv")
                    self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
