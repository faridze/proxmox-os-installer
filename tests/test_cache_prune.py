import importlib.util
import os
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
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
time.sleep(60)
"""


class CachePruneTests(unittest.TestCase):
    def test_symlink_not_pruned(self):
        catalog = BASE / "catalog/os-images.tsv"
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            target = cache / "target.img"
            target.write_bytes(b"x")
            link = cache / "evil-link.img"
            link.symlink_to(target)
            cache_ops.prune_cache(cache, catalog, apply=True)
            self.assertTrue(link.is_symlink())
            self.assertFalse(target.exists())

    def test_obsolete_skipped_when_other_lock_busy(self):
        catalog = BASE / "catalog/os-images.tsv"
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            stale = cache / "stale.img"
            stale.write_bytes(b"data")
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
                cache_ops.prune_cache(cache, catalog, apply=True)
                self.assertTrue(stale.exists())
            finally:
                holder.kill()
                holder.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
