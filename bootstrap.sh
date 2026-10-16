#!/usr/bin/env bash
# HTTPS-only bootstrap from this repository's main branch.
# Review this file before running as root; use a pinned reviewed ref in production.
set -Eeuo pipefail
umask 077
(( EUID == 0 )) || { printf 'ERROR: run as root on a Proxmox VE node.\n' >&2; exit 1; }
command -v qm >/dev/null 2>&1 || { printf 'ERROR: this is not a Proxmox node (qm missing).\n' >&2; exit 1; }
for cmd in curl tar mktemp python3; do
  command -v "$cmd" >/dev/null 2>&1 || { printf 'ERROR: missing tool: %s\n' "$cmd" >&2; exit 1; }
done
REF="${PROXMOX_OS_INSTALLER_REF:-main}"
[[ "$REF" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,200}$ ]] || { printf 'ERROR: invalid ref.\n' >&2; exit 1; }
[[ "$REF" != *..* ]] || { printf 'ERROR: invalid ref.\n' >&2; exit 1; }
TMP_BASE="${TMPDIR:-/var/tmp}"
TMP_DIR="$(mktemp -d "${TMP_BASE}/proxmox-os-installer.XXXXXXXX")"
trap 'rm -rf -- "$TMP_DIR"' EXIT
REPO_SLUG='faridze/proxmox-os-installer'
RESOLVED_REF="$REF"
if [[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  COMMIT_JSON="$(curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 \
    --proto '=https' --proto-redir '=https' \
    -H 'Accept: application/vnd.github+json' -H 'User-Agent: proxmox-os-installer-bootstrap' \
    "https://api.github.com/repos/${REPO_SLUG}/commits/${REF}")" || {
    printf 'ERROR: could not resolve ref %s on GitHub.\n' "$REF" >&2; exit 1;
  }
  RESOLVED_REF="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])' <<<"$COMMIT_JSON")" || {
    printf 'ERROR: could not parse commit SHA.\n' >&2; exit 1;
  }
fi
ARCHIVE="https://api.github.com/repos/${REPO_SLUG}/tarball/${RESOLVED_REF}"
printf 'Downloading proxmox-os-installer (%s) from GitHub...\n' "$REF"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 \
  --proto '=https' --proto-redir '=https' --output "$TMP_DIR/source.tar.gz" "$ARCHIVE"
mkdir -m 0700 "$TMP_DIR/source"
python3 - "$TMP_DIR/source.tar.gz" "$TMP_DIR/source" <<'PY'
import sys, tarfile
from pathlib import Path
archive, dest = Path(sys.argv[1]), Path(sys.argv[2])
dest.mkdir(parents=True, exist_ok=True)
with tarfile.open(archive, "r:gz") as tf:
    roots = set()
    members = []
    for m in tf.getmembers():
        if m.name.startswith("/") or ".." in Path(m.name).parts:
            raise SystemExit("ERROR: unsafe archive path")
        if m.issym() or m.islnk():
            raise SystemExit("ERROR: refusing symlink in archive")
        members.append(m)
        parts = Path(m.name).parts
        if parts:
            roots.add(parts[0])
    if len(roots) != 1:
        raise SystemExit("ERROR: unexpected archive layout")
    tf.extractall(path=dest, members=members, filter="data")
    root = dest / next(iter(roots))
    for child in root.iterdir():
        target = dest / child.name
        if target.exists():
            if target.is_dir():
                import shutil
                shutil.rmtree(target)
            else:
                target.unlink()
        child.rename(target)
    root.rmdir()
PY
[[ -f "$TMP_DIR/source/install.sh" && -f "$TMP_DIR/source/VERSION" ]] || {
  printf 'ERROR: downloaded archive is incomplete.\n' >&2; exit 1;
}
export PVEOSI_INSTALL_REF="$REF" PVEOSI_INSTALL_COMMIT="$RESOLVED_REF" \
  PVEOSI_INSTALL_REPO="https://github.com/${REPO_SLUG}"
# Bash execution works even when install.sh lacks executable mode or /tmp is noexec.
bash "$TMP_DIR/source/install.sh"
printf '\nNext: proxmox-os-installer\n'
