#!/usr/bin/env python3
"""Safe self-update for proxmox-os-installer (HTTPS GitHub tarball, staged install, rollback)."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_REPO = "faridze/proxmox-os-installer"
DEFAULT_REPO_HTTPS = f"https://github.com/{DEFAULT_REPO}"
TRUSTED_REPO_SLUGS = frozenset({DEFAULT_REPO})
METADATA_FILENAME = "install-metadata.json"
LOCK_PATH = "/var/lock/proxmox-os-installer-update.lock"
SUFFIX_PREVIOUS = ".previous"
SUFFIX_BROKEN = ".broken"
SUFFIX_STAGING = ".staging"

REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,200}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

REQUIRED_SOURCE = (
    "install.sh",
    "VERSION",
    "bin/proxmox-os-installer",
    "catalog/os-images.tsv",
    "lib/image_catalog.py",
    "lib/preflight.py",
    "lib/cache_ops.py",
    "lib/ssh_access.py",
    "lib/update_engine.py",
    "lib/finalize_engine.py",
    "lib/console_engine.py",
    "lib/snippet_cleanup.py",
    "lib/common.sh",
    "lib/interactive.sh",
    "lib/install_aliases.sh",
    "lib/snippets_guidance.sh",
)

REQUIRED_INSTALLED = (
    "VERSION",
    "bin/proxmox-os-installer",
    "catalog/os-images.tsv",
    "lib/image_catalog.py",
    "lib/preflight.py",
    "lib/cache_ops.py",
    "lib/ssh_access.py",
    "lib/update_engine.py",
    "lib/finalize_engine.py",
    "lib/console_engine.py",
    "lib/snippet_cleanup.py",
    "lib/common.sh",
    "lib/interactive.sh",
    "lib/install_aliases.sh",
    "lib/snippets_guidance.sh",
)

PY_LIBS = (
    "image_catalog.py",
    "preflight.py",
    "cache_ops.py",
    "ssh_access.py",
    "update_engine.py",
    "finalize_engine.py",
    "console_engine.py",
    "snippet_cleanup.py",
)
SH_LIBS = ("common.sh", "interactive.sh", "install_aliases.sh", "snippets_guidance.sh")


class UpdateError(Exception):
    """User-facing update failure."""


class NetworkError(UpdateError):
    """Network or GitHub API failure."""


@dataclass
class ResolvedTarget:
    ref: str
    commit: str
    version: str
    source: str


@dataclass
class InstallPaths:
    install_root: Path
    metadata_path: Path
    entry_link: Path
    alias_bindir: Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def sibling_path(install_root: Path, suffix: str) -> Path:
    """Recovery path adjacent to install_root; must not follow unsafe symlinks."""
    parent = install_root.parent.resolve()
    name = install_root.name
    if not name or name in (".", "..") or "/" in name:
        raise UpdateError("Unsafe install root path.")
    candidate = parent / f"{name}{suffix}"
    resolved_parent = candidate.parent.resolve()
    if resolved_parent != parent:
        raise UpdateError("Refusing recovery path outside install parent directory.")
    if candidate.is_symlink():
        raise UpdateError(f"Refusing unsafe recovery symlink: {candidate}")
    return candidate


def previous_path(paths: InstallPaths) -> Path:
    return sibling_path(paths.install_root, SUFFIX_PREVIOUS)


def broken_path(paths: InstallPaths) -> Path:
    return sibling_path(paths.install_root, SUFFIX_BROKEN)


def staging_path(paths: InstallPaths) -> Path:
    return sibling_path(paths.install_root, SUFFIX_STAGING)


def assert_safe_install_root(paths: InstallPaths) -> Path:
    root = paths.install_root
    if root.is_symlink():
        raise UpdateError("Install root must not be a symlink.")
    return root


def trusted_repo_slug(meta: dict[str, Any]) -> str:
    """Return the only allowed repository slug; reject deceptive metadata."""
    raw = meta.get("repository")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_REPO
    if not isinstance(raw, str):
        raise UpdateError("Invalid installation repository metadata.")
    value = raw.strip()
    slug: Optional[str] = None
    if value in TRUSTED_REPO_SLUGS:
        slug = value
    elif value.rstrip("/") in (DEFAULT_REPO_HTTPS, f"{DEFAULT_REPO_HTTPS}/"):
        slug = DEFAULT_REPO
    elif value.startswith("https://github.com/"):
        tail = value[len("https://github.com/") :].strip("/")
        if "@" in tail or "?" in tail or "#" in tail or "://" in tail:
            raise UpdateError("Refusing malformed repository URL in installation metadata.")
        parts = tail.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise UpdateError("Refusing malformed repository URL in installation metadata.")
        slug = f"{parts[0]}/{parts[1]}"
    elif value.startswith("http://"):
        raise UpdateError("Refusing non-HTTPS repository URL in installation metadata.")
    else:
        raise UpdateError("Refusing update from an unexpected repository; use bootstrap with official repo.")
    if slug not in TRUSTED_REPO_SLUGS:
        raise UpdateError("Refusing untrusted repository in installation metadata.")
    return slug


def default_paths() -> InstallPaths:
    root = _env_path("PVEOSI_INSTALL_ROOT", "/usr/local/lib/proxmox-os-installer")
    entry = _env_path("PVEOSI_INSTALL_ENTRY", "/usr/local/sbin/proxmox-os-installer")
    bindir = _env_path("PVEOSI_ALIAS_BINDIR", "/usr/local/sbin")
    return InstallPaths(
        install_root=root,
        metadata_path=root / METADATA_FILENAME,
        entry_link=entry,
        alias_bindir=bindir,
    )


def read_version(tree: Path) -> str:
    vf = tree / "VERSION"
    if vf.is_file():
        return vf.read_text(encoding="utf-8").strip()
    bin_path = tree / "bin/proxmox-os-installer"
    if bin_path.is_file():
        text = bin_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^VERSION="([^"]+)"', text, re.M)
        if m:
            return m.group(1)
    return "unknown"


def load_metadata(paths: InstallPaths) -> dict[str, Any]:
    if not paths.metadata_path.is_file():
        return {}
    try:
        data = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_metadata(paths: InstallPaths, meta: dict[str, Any]) -> None:
    paths.install_root.mkdir(parents=True, exist_ok=True)
    paths.metadata_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(paths.metadata_path, 0o644)


def validate_ref(ref: str) -> str:
    ref = ref.strip()
    if not ref or not REF_RE.match(ref):
        raise UpdateError(f"Invalid ref: {ref!r}")
    if ".." in ref or ref.startswith("/"):
        raise UpdateError(f"Unsafe ref: {ref!r}")
    return ref


def _github_request(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "proxmox-os-installer-update",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(f"Ref not found on GitHub: {url}") from exc
        raise NetworkError(f"GitHub API error ({exc.code})") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"Network error: {exc.reason}") from exc


def resolve_commit(ref: str, repo: str = DEFAULT_REPO) -> str:
    ref = validate_ref(ref)
    if FULL_SHA_RE.match(ref):
        return ref
    api = f"https://api.github.com/repos/{repo}/commits/{ref}"
    body = _github_request(api)
    data = json.loads(body.decode("utf-8"))
    sha = data.get("sha")
    if not isinstance(sha, str) or not FULL_SHA_RE.match(sha):
        raise UpdateError("Could not resolve commit SHA from GitHub.")
    return sha


def tarball_url(commit: str, repo: str = DEFAULT_REPO) -> str:
    if not FULL_SHA_RE.match(commit):
        raise UpdateError("Internal error: commit must be full SHA for download.")
    return f"https://api.github.com/repos/{repo}/tarball/{commit}"


def download_tarball(commit: str, dest: Path, repo: str = DEFAULT_REPO) -> None:
    url = tarball_url(commit, repo)
    req = urllib.request.Request(url, headers={"User-Agent": "proxmox-os-installer-update"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"Download failed (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"Download failed: {exc.reason}") from exc


def safe_extract_tar(archive: Path, dest: Path) -> Path:
    """Extract GitHub tarball; return path to package root inside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        if not members:
            raise UpdateError("Archive is empty.")
        roots: set[str] = set()
        safe_members: list[tarfile.TarInfo] = []
        for m in members:
            name = m.name
            if name.startswith("/") or name.startswith("\\"):
                raise UpdateError(f"Unsafe archive path: {name}")
            parts = Path(name).parts
            if ".." in parts:
                raise UpdateError(f"Archive traversal attempt: {name}")
            if m.issym() or m.islnk():
                raise UpdateError(f"Refusing symlink/hardlink in archive: {name}")
            if m.isdev() or m.isfifo() or m.ischr() or m.isblk():
                raise UpdateError(f"Refusing special file in archive: {name}")
            safe_members.append(m)
            if parts:
                roots.add(parts[0])
        if len(roots) != 1:
            raise UpdateError("Unexpected archive layout (multiple top-level directories).")
        tf.extractall(path=dest, members=safe_members, filter="data")
        root_name = next(iter(roots))
        return dest / root_name


def validate_package_tree(tree: Path, *, installed: bool = False) -> None:
    required = REQUIRED_INSTALLED if installed else REQUIRED_SOURCE
    for rel in required:
        p = tree / rel
        if not p.is_file():
            raise UpdateError(f"Missing required file in package: {rel}")
    if not installed and not os.access(tree / "install.sh", os.R_OK):
        raise UpdateError("install.sh is not readable.")


def run_validation(tree: Path, *, installed: bool = False) -> None:
    validate_package_tree(tree, installed=installed)
    catalog = tree / "catalog/os-images.tsv"
    subprocess.run(
        [sys.executable, str(tree / "lib/image_catalog.py"), "catalog-check", str(catalog)],
        check=True,
        capture_output=True,
    )
    sh_files = ["bin/proxmox-os-installer"] + [f"lib/{s}" for s in SH_LIBS]
    if not installed:
        sh_files = ["install.sh"] + sh_files
    for sh in sh_files:
        subprocess.run(["bash", "-n", str(tree / sh)], check=True, capture_output=True)
    for py in PY_LIBS:
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(tree / "lib" / py)],
            check=True,
            capture_output=True,
        )


def stage_install(
    tree: Path,
    stage_root: Path,
    meta: dict[str, Any],
    paths: Optional[InstallPaths] = None,
) -> None:
    if stage_root.exists():
        shutil.rmtree(stage_root)
    env = os.environ.copy()
    env["PVEOSI_INSTALL_ROOT"] = str(stage_root)
    if paths is not None:
        env["PVEOSI_INSTALL_ENTRY"] = str(paths.entry_link)
        env["PVEOSI_ALIAS_BINDIR"] = str(paths.alias_bindir)
    env["PVEOSI_SKIP_ALIASES"] = "1"
    env["PVEOSI_SKIP_MAIN_ENTRY"] = "1"
    env["PVEOSI_QUIET_INSTALL"] = "1"
    env["PVEOSI_INSTALL_REF"] = str(meta.get("install_ref", ""))
    env["PVEOSI_INSTALL_COMMIT"] = str(meta.get("commit_sha", ""))
    env["PVEOSI_INSTALL_REPO"] = str(meta.get("repository", DEFAULT_REPO_HTTPS))
    env["PVEOSI_INSTALL_VERSION"] = str(meta.get("version", read_version(tree)))
    subprocess.run(["bash", str(tree / "install.sh")], check=True, env=env, cwd=str(tree))


def refresh_aliases(install_root: Path, paths: InstallPaths) -> None:
    env = os.environ.copy()
    env["PVEOSI_INSTALL_ROOT"] = str(install_root)
    env["PVEOSI_INSTALL_ENTRY"] = str(paths.entry_link)
    env["PVEOSI_ALIAS_BINDIR"] = str(paths.alias_bindir)
    env["PVEOSI_SKIP_FILE_COPY"] = "1"
    env["PVEOSI_WRITE_METADATA"] = "0"
    env["PVEOSI_QUIET_INSTALL"] = "1"
    subprocess.run(["bash", str(install_root / "install.sh")], check=True, env=env, cwd=str(install_root))


def installed_commit(paths: InstallPaths, meta: dict[str, Any]) -> Optional[str]:
    sha = meta.get("commit_sha")
    if isinstance(sha, str) and FULL_SHA_RE.match(sha):
        return sha
    return None


def channel_default_ref(meta: dict[str, Any]) -> Optional[str]:
    ref = meta.get("install_ref") or meta.get("channel")
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    if ref == "main":
        return "main"
    if FULL_SHA_RE.match(ref) or SHORT_SHA_RE.match(ref):
        return ref
    # Experimental / feature branch: stay on same branch name
    if ref not in ("main", "master"):
        return ref
    return ref


def refuse_implicit_main_downgrade(
    meta: dict[str, Any],
    target_ref: str,
    target_commit: str,
    *,
    explicit_ref: Optional[str],
) -> None:
    """Pinned or non-main installs must not silently move to main."""
    if explicit_ref is not None:
        return
    install_ref = meta.get("install_ref") or meta.get("channel") or ""
    if not isinstance(install_ref, str):
        install_ref = ""
    install_ref = install_ref.strip()
    cur_sha = installed_commit(
        default_paths(),
        meta,
    )
    if target_ref == "main" and install_ref and install_ref != "main":
        if FULL_SHA_RE.match(install_ref) or SHORT_SHA_RE.match(install_ref):
            raise UpdateError(
                "This installation is pinned to a commit; use --ref <commit-or-tag> to change version."
            )
        raise UpdateError(
            f"This installation tracks ref '{install_ref}'; use --ref to switch to main explicitly."
        )
    if cur_sha and target_commit != cur_sha and target_ref == "main":
        if install_ref and install_ref != "main" and not FULL_SHA_RE.match(install_ref):
            raise UpdateError(
                f"Refusing implicit channel switch from '{install_ref}' to main without --ref main."
            )


def pick_update_ref(
    meta: dict[str, Any],
    explicit_ref: Optional[str],
) -> str:
    if explicit_ref:
        return validate_ref(explicit_ref)
    default = channel_default_ref(meta)
    if default:
        return validate_ref(default)
    raise UpdateError(
        "Installation provenance is unknown. Specify --ref <commit-or-tag> for this update."
    )


def acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise UpdateError("Refusing unsafe update lock path.")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise UpdateError("Another update is already in progress.")
    return fd


def release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def recover_interrupted_paths(paths: InstallPaths) -> None:
    """Normalize leftover directories from a failed or partial update."""
    live = assert_safe_install_root(paths)
    staging = staging_path(paths)
    broken = broken_path(paths)
    if staging.exists():
        if live.exists():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            try:
                staging.rename(live)
            except OSError as exc:
                raise UpdateError(
                    f"Interrupted update left staging directory; manual recovery may be required: {staging}"
                ) from exc
    if broken.exists() and not live.exists():
        prev = previous_path(paths)
        try:
            broken.rename(live)
        except OSError:
            if not prev.exists():
                raise UpdateError(
                    f"Failed install at {broken}; inspect manually before retrying."
                ) from None


def activate_stage(stage_root: Path, paths: InstallPaths) -> Path:
    """Swap stage into install_root; return path to the replaced live tree (pending backup)."""
    live = assert_safe_install_root(paths)
    pending = staging_path(paths)
    if pending.exists():
        shutil.rmtree(pending)
    if live.exists():
        try:
            live.rename(pending)
        except OSError as exc:
            raise UpdateError(f"Could not move current installation aside: {exc}") from exc
    try:
        stage_root.rename(live)
    except OSError as exc:
        if pending.exists() and not live.exists():
            try:
                pending.rename(live)
            except OSError:
                pass
        raise UpdateError(f"Could not activate staged installation: {exc}") from exc
    return pending


def commit_previous_backup(paths: InstallPaths, pending_backup: Path) -> None:
    """Retain one generation: replace .previous only after the new live tree is active."""
    if not pending_backup.exists():
        return
    prev = previous_path(paths)
    if prev.exists():
        shutil.rmtree(prev)
    pending_backup.rename(prev)


def rollback(paths: InstallPaths, pending_backup: Path) -> None:
    """Restore the installation tree replaced during activate_stage."""
    live = assert_safe_install_root(paths)
    broken = broken_path(paths)
    if live.exists():
        if broken.exists():
            shutil.rmtree(broken)
        try:
            live.rename(broken)
        except OSError as exc:
            raise UpdateError(
                f"Automatic rollback incomplete; inspect {broken} and {live} manually."
            ) from exc
    if pending_backup.exists():
        try:
            pending_backup.rename(live)
        except OSError as exc:
            raise UpdateError(
                f"Automatic rollback incomplete; prior tree remains at {pending_backup}."
            ) from exc


def assert_install_unchanged_for_update(
    paths: InstallPaths,
    baseline_commit: Optional[str],
    target: ResolvedTarget,
) -> None:
    current = installed_commit(paths, load_metadata(paths))
    if current and current == target.commit:
        raise UpdateError("Installation is already at the target commit.")
    if baseline_commit and current and current != baseline_commit:
        raise UpdateError(
            "Installation changed during update; another update may have completed. Re-run update --check."
        )


def plan_update(
    paths: InstallPaths,
    explicit_ref: Optional[str],
) -> ResolvedTarget:
    meta = load_metadata(paths)
    repo_slug = trusted_repo_slug(meta)

    ref = pick_update_ref(meta, explicit_ref)
    commit = resolve_commit(ref, repo_slug)
    refuse_implicit_main_downgrade(meta, ref, commit, explicit_ref=explicit_ref)

    # Resolve version from remote tree without full install: use commit API + VERSION in tarball is heavy;
    # we read VERSION after download in perform path. For plan, placeholder until extract.
    version = meta.get("version") if isinstance(meta.get("version"), str) else "unknown"
    return ResolvedTarget(ref=ref, commit=commit, version=version, source=repo_slug)


def perform_update(
    paths: InstallPaths,
    target: ResolvedTarget,
    check_only: bool = False,
    explicit_ref: Optional[str] = None,
) -> ResolvedTarget:
    assert_safe_install_root(paths)
    recover_interrupted_paths(paths)

    lock_path = Path(os.environ.get("PVEOSI_UPDATE_LOCK", LOCK_PATH))
    lock_fd = acquire_lock(lock_path)
    try:
        meta = load_metadata(paths)
        baseline_commit = installed_commit(paths, meta)

        # Re-plan under lock so metadata/channel rules match current installation.
        target = plan_update(paths, explicit_ref)
        if baseline_commit and baseline_commit == target.commit:
            return target
        assert_install_unchanged_for_update(paths, baseline_commit, target)

        work_parent = paths.install_root.parent
        work_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pveosi-update.", dir=str(work_parent)) as tmp:
            tmp_path = Path(tmp)
            try:
                archive = tmp_path / "source.tar.gz"
                download_tarball(target.commit, archive, target.source)
                extract_root = tmp_path / "extract"
                tree = safe_extract_tar(archive, extract_root)
                run_validation(tree)
                target.version = read_version(tree)

                if baseline_commit and baseline_commit == target.commit:
                    return target

                if check_only:
                    return target

                assert_install_unchanged_for_update(paths, baseline_commit, target)

                stage_root = tmp_path / "stage"
                new_meta = {
                    "version": target.version,
                    "commit_sha": target.commit,
                    "repository": DEFAULT_REPO_HTTPS,
                    "install_ref": target.ref if target.ref != target.commit else target.commit,
                    "channel": target.ref,
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                try:
                    stage_install(tree, stage_root, new_meta, paths)
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise UpdateError("Staging installation failed.") from exc
                try:
                    run_validation(stage_root, installed=True)
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise UpdateError("Staged installation failed validation.") from exc

                assert_install_unchanged_for_update(paths, baseline_commit, target)

                pending_backup = Path()
                print("Installing...")
                try:
                    pending_backup = activate_stage(stage_root, paths)
                except UpdateError:
                    raise
                except OSError as exc:
                    raise UpdateError(f"Activation failed: {exc}") from exc

                try:
                    refresh_aliases(paths.install_root, paths)
                    write_metadata(paths, new_meta)
                    run_validation(paths.install_root, installed=True)
                except Exception as exc:
                    try:
                        rollback(paths, pending_backup)
                    except UpdateError as rollback_exc:
                        raise UpdateError(
                            f"Update failed ({exc}); automatic rollback also failed: {rollback_exc}"
                        ) from exc
                    raise UpdateError("Update failed during activation; previous version restored.") from exc

                commit_previous_backup(paths, pending_backup)
            except tarfile.TarError as exc:
                raise UpdateError("Downloaded archive is corrupt or unreadable.") from exc
    finally:
        release_lock(lock_fd)

    return target


def print_status_header(
    paths: InstallPaths,
    target: ResolvedTarget,
    *,
    update_available: bool,
) -> None:
    meta = load_metadata(paths)
    installed_ver = read_version(paths.install_root) if paths.install_root.is_dir() else str(meta.get("version", "unknown"))
    cur_commit = installed_commit(paths, meta) or "(unknown)"
    print("Proxmox OS Installer")
    print()
    print(f"Installed: v{installed_ver}")
    print(f"Current commit: {cur_commit}")
    print(f"Target: v{target.version}")
    print(f"Target commit: {target.commit}")
    print(f"Source: {target.source}")
    print()
    if update_available:
        print("Update available.")
    else:
        print("Already up to date.")


def cmd_update(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pveosi update",
        description=(
            "Download and install a newer proxmox-os-installer from the official GitHub repository "
            "over HTTPS. Does not modify VMs, image cache, or Proxmox cluster settings. "
            "Trust is limited to TLS until signed releases exist."
        ),
    )
    parser.add_argument("--check", action="store_true", help="Check for updates without installing.")
    parser.add_argument("--ref", metavar="REF", help="Install a specific Git tag or commit (required for some pinned installs).")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive confirmation.")
    args = parser.parse_args(argv)

    if os.geteuid() != 0 and os.environ.get("PVEOSI_UPDATE_ALLOW_NONROOT") != "1":
        print("ERROR: run as root.", file=sys.stderr)
        return 1

    paths = default_paths()
    try:
        target = plan_update(paths, args.ref)
    except NetworkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    meta = load_metadata(paths)
    cur_commit = installed_commit(paths, meta)
    # Version comparison alone is insufficient; compare commits when known.
    try:
        work_parent = paths.install_root.parent
        work_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pveosi-check.", dir=str(work_parent)) as tmp:
            archive = Path(tmp) / "source.tar.gz"
            download_tarball(target.commit, archive, target.source)
            tree = safe_extract_tar(archive, Path(tmp) / "extract")
            target.version = read_version(tree)
    except NetworkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    update_available = not (cur_commit and cur_commit == target.commit)
    print_status_header(paths, target, update_available=update_available)

    if args.check:
        return 0

    if not update_available:
        return 0

    if not args.yes:
        try:
            ans = input("Proceed with update? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Cancelled.")
            return 0

    print("Updating...")
    print("Verifying package...")
    try:
        perform_update(paths, target, check_only=False, explicit_ref=args.ref)
    except NetworkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print("ERROR: validation or install failed.", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Update successful.")
    print("Run: pveosi")
    return 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "update":
        print("Usage: update_engine.py update [--check] [--ref REF] [--yes]", file=sys.stderr)
        sys.exit(2)
    sys.exit(cmd_update(sys.argv[2:]))


if __name__ == "__main__":
    main()
