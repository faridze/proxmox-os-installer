import importlib.util
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_catalog", BASE / "lib/image_catalog.py")
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def test_eight_entries(self):
        actual = catalog.load_catalog(BASE / "catalog/os-images.tsv")
        self.assertEqual(len(actual), 8)
        self.assertEqual(len({e[0] for e in actual}), 8)

    def test_gnu_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'sums'
            f.write_text('f'*64 + ' *rocky.qcow2\n')
            self.assertEqual(catalog.find_digest(f, 'rocky.qcow2', 'sha256'), 'f'*64)

    def test_bsd_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'sums'
            f.write_text('SHA256 (rocky.qcow2) = ' + 'a'*64 + '\n')
            self.assertEqual(catalog.find_digest(f, 'rocky.qcow2', 'sha256'), 'a'*64)

    def test_conflicting_hashes_fail(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'sums'
            f.write_text('a'*64 + '  linux.img\n' + 'b'*64 + '  linux.img\n')
            with self.assertRaises(ValueError):
                catalog.find_digest(f, 'linux.img', 'sha256')

    def test_missing_hash_fails(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'sums'
            f.write_text('a'*64 + '  wrong.img\n')
            with self.assertRaises(ValueError):
                catalog.find_digest(f, 'linux.img', 'sha256')

    def test_invalid_url_fails(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'catalog'
            f.write_text('bad|Bad OS|debian|http://unsafe/|img.qcow2|SUMS|sha256\n')
            with self.assertRaises(ValueError):
                catalog.load_catalog(f)


if __name__ == '__main__':
    unittest.main()
