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
[[ "$REF" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$ ]] || { printf 'ERROR: invalid ref.\n' >&2; exit 1; }
TMP_DIR="$(mktemp -d /tmp/proxmox-os-installer.XXXXXXXX)"
trap 'rm -rf -- "$TMP_DIR"' EXIT
# Main is selected for the simple first-time install; tags/commits may be supplied via env.
ARCHIVE="https://api.github.com/repos/faridze/proxmox-os-installer/tarball/${REF}"
printf 'Downloading proxmox-os-installer (%s) from GitHub...\n' "$REF"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 \
  --proto '=https' --proto-redir '=https' --output "$TMP_DIR/source.tar.gz" "$ARCHIVE"
mkdir -m 0700 "$TMP_DIR/source"
tar -xzf "$TMP_DIR/source.tar.gz" --strip-components=1 -C "$TMP_DIR/source"
[[ -f # Execute with Bash even if the Git archive omits +x or /tmp uses noexec.
bash "$TMP_DIR/source/install.sh" && -f "$TMP_DIR/source/VERSION" ]] || {
  printf 'ERROR: downloaded archive is incomplete.\n' >&2; exit 1;
}
"$TMP_DIR/source/install.sh"
printf '\nNext: proxmox-os-installer\n'
