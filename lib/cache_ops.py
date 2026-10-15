#!/usr/bin/env python3
"""Checksum-aware image cache listing and pruning."""
import fcntl
import os
import sys
from contextlib import contextmanager
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


def resolve_cache_path(cache_dir: Path, name: str) -> Path:
    root = cache_dir.resolve()
    candidate = (root / name).resolve()
    if not str(candidate).startswith(str(root) + os.sep) and candidate != root:
        raise ValueError(f"refusing path outside cache: {name}")
    return candidate


class CacheLockBusy(OSError):
    """Another process holds the cache lock."""


@contextmanager
def hold_cache_lock(lock_path: Path, blocking: bool = True):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            os.close(fd)
            raise CacheLockBusy(f"lock busy: {lock_path.name}") from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
    by_file = {expected_cache_name(e[0], e[4]): e for e in entries}
    by_os = {e[0]: e for e in entries}
    return by_file, by_os


def prune_reason(path: Path, expected_map: dict) -> str | None:
    if is_temp_in_progress(path) or path.name.endswith(LOCK_SUFFIX):
        return None
    if path.name not in expected_map:
        return "not listed in current catalog"
    if path.stat().st_size == 0:
        return "empty cache file"
    return None


def os_id_for_cache_file(filename: str, expected_map: dict) -> str | None:
    entry = expected_map.get(filename)
    return entry[0] if entry else None


def list_cache(cache_dir, catalog_path):
    expected_map, _ = catalog_expected(catalog_path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total = 0
    for p in sorted(cache_dir.iterdir()):
        if not p.is_file() or p.name.endswith(LOCK_SUFFIX) or is_temp_in_progress(p):
            continue
        size = p.stat().st_size
        total += size
        status = "current" if p.name in expected_map else "obsolete"
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


def collect_prune_candidates(cache_dir: Path, expected_map: dict) -> list[tuple[Path, str]]:
    out = []
    for p in sorted(cache_dir.iterdir()):
        if not p.is_file():
            continue
        reason = prune_reason(p, expected_map)
        if reason:
            out.append((p, reason))
    return out


def delete_under_lock(cache_dir: Path, path: Path, expected_map: dict, reason: str) -> bool:
    """Delete one cache file while holding the relevant provisioning lock."""
    path = resolve_cache_path(cache_dir, path.name)
    os_id = os_id_for_cache_file(path.name, expected_map)
    lock_path = cache_dir / f"{os_id}.lock" if os_id else cache_dir / ".prune.lock"
    try:
        with hold_cache_lock(lock_path, blocking=False):
            if not path.is_file():
                return False
            current_reason = prune_reason(path, expected_map)
            if current_reason != reason:
                return False
            path.unlink()
            return True
    except CacheLockBusy:
        return False
    except OSError as exc:
        print(f"SKIP {path.name}: {exc}", file=sys.stderr)
        return False


def prune_cache(cache_dir, catalog_path, apply=False):
    expected_map, _ = catalog_expected(catalog_path)
    cache_dir = Path(cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    candidates = collect_prune_candidates(cache_dir, expected_map)

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

    removed = 0
    for p, reason in candidates:
        if delete_under_lock(cache_dir, p, expected_map, reason):
            removed += 1
        else:
            print(f"SKIP (busy or changed): {p.name}", file=sys.stderr)
    print(f"Prune complete ({removed} file(s) removed).")
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
