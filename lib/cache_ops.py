#!/usr/bin/env python3
"""Checksum-aware image cache listing and pruning."""
import fcntl
import os
import sys
from pathlib import Path

import importlib.util

_BASE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("image_catalog", _BASE / "image_catalog.py")
_catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_catalog)

LOCK_SUFFIX = ".lock"
TEMP_PREFIXES = (".image.", ".checksums.")


def expected_cache_name(os_id, image_file):
    return f"{os_id}-{image_file}"


def is_temp_in_progress(path: Path) -> bool:
    return any(path.name.startswith(prefix) for prefix in TEMP_PREFIXES)


def lock_is_busy(lock_path: Path) -> bool:
    try:
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
    except OSError:
        return True


def human_size(num):
    n = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def catalog_expected(catalog_path):
    entries = _catalog.load_catalog(catalog_path)
    return {expected_cache_name(e[0], e[4]): e for e in entries}


def list_cache(cache_dir, catalog_path):
    expected = set(catalog_expected(catalog_path))
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total = 0
    for p in sorted(cache_dir.iterdir()):
        if not p.is_file() or p.name.endswith(LOCK_SUFFIX) or is_temp_in_progress(p):
            continue
        size = p.stat().st_size
        total += size
        status = "current" if p.name in expected else "obsolete"
        rows.append((p.name, size, status))
    print(f"Cache directory: {cache_dir}")
    print(f"Total cached image data: {human_size(total)}")
    if not rows:
        print("(no cached images)")
        return 0
    print(f"{'FILE':<60} {'SIZE':>12}  STATUS")
    for name, size, status in rows:
        print(f"{name:<60} {human_size(size):>12}  {status}")
    return 0


def prune_cache(cache_dir, catalog_path, apply=False):
    expected_map = catalog_expected(catalog_path)
    cache_dir = Path(cache_dir)
    candidates = []
    for p in sorted(cache_dir.iterdir()):
        if not p.is_file() or p.name.endswith(LOCK_SUFFIX) or is_temp_in_progress(p):
            continue
        os_id = None
        for name, entry in expected_map.items():
            if name == p.name:
                os_id = entry[0]
                break
        if os_id:
            lock_path = cache_dir / f"{os_id}.lock"
            if lock_path.exists() and lock_is_busy(lock_path):
                print(f"SKIP (active lock): {p.name}", file=sys.stderr)
                continue
        reason = None
        if p.name not in expected_map:
            reason = "not listed in current catalog"
        elif p.stat().st_size == 0:
            reason = "empty cache file"
        if reason:
            candidates.append((p, reason))

    if not candidates:
        print("Nothing to prune.")
        return 0
    print("The following cache files may be removed:")
    freed = 0
    for p, reason in candidates:
        freed += p.stat().st_size
        print(f"  {p.name} ({human_size(p.stat().st_size)}) — {reason}")
    print(f"Total reclaimable: {human_size(freed)}")
    if not apply:
        print("Re-run with --yes to delete these files after explicit confirmation.")
        return 0
    for p, _ in candidates:
        p.unlink(missing_ok=True)
    print("Prune complete.")
    return 0


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: cache_ops.py list CACHE_DIR CATALOG | prune CACHE_DIR CATALOG [--yes]",
            file=sys.stderr,
        )
        return 2
    cmd, cache_dir, catalog = sys.argv[1], sys.argv[2], sys.argv[3]
    apply = "--yes" in sys.argv[4:]
    try:
        if cmd == "list":
            return list_cache(cache_dir, catalog)
        if cmd == "prune":
            return prune_cache(cache_dir, catalog, apply=apply)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
