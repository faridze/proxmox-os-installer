import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cache_ops", BASE / "lib/cache_ops.py")
cache_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache_ops)

HOLDER = """
import fcntl, os, sys, time
lock, ready = sys.argv[1], sys.argv[2]
fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(ready, "w").close()
time.sleep(60)
"""


class CacheConcurrencyTests(unittest.TestCase):
    def test_prune_skips_when_lock_held(self):
        catalog = BASE / "catalog/os-images.tsv"
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            name = cache_ops.expected_cache_name("debian-12", "debian-12-genericcloud-amd64.qcow2")
            target = cache / name
            target.write_bytes(b"")
            lock = cache / "debian-12.lock"
            ready = cache / ".holder-ready"
            holder = subprocess.Popen(
                [sys.executable, "-c", HOLDER, str(lock), str(ready)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if ready.exists():
                        break
                    time.sleep(0.05)
                else:
                    self.fail("lock holder did not start")
                cache_ops.prune_cache(cache, catalog, apply=True)
                self.assertTrue(target.exists())
            finally:
                holder.kill()
                holder.wait(timeout=5)

    def test_prune_deletes_when_lock_available(self):
        catalog = BASE / "catalog/os-images.tsv"
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            obsolete = cache / "stale.img"
            obsolete.write_bytes(b"data")
            cache_ops.prune_cache(cache, catalog, apply=True)
            self.assertFalse(obsolete.exists())


if __name__ == "__main__":
    unittest.main()
