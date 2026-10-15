import importlib.util
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cache_ops", BASE / "lib/cache_ops.py")
cache_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache_ops)


class CacheTests(unittest.TestCase):
    def test_list_and_prune_obsolete(self):
        catalog = BASE / "catalog/os-images.tsv"
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            (cache / "obsolete-old.img").write_bytes(b"x" * 10)
            current = cache_ops.expected_cache_name("debian-12", "debian-12-genericcloud-amd64.qcow2")
            (cache / current).write_bytes(b"y" * 20)
            cache_ops.prune_cache(cache, catalog, apply=True)
            self.assertFalse((cache / "obsolete-old.img").exists())
            self.assertTrue((cache / current).exists())


if __name__ == "__main__":
    unittest.main()
