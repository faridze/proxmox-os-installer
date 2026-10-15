#!/usr/bin/env bash
# Install/update from a reviewed working tree. No apt changes, no remote execution.
set -Eeuo pipefail
if (( EUID != 0 )) && [[ "${PVEOSI_INSTALL_ALLOW_NONROOT:-0}" != 1 ]]; then
  printf 'ERROR: run as root.\n' >&2
  exit 1
fi
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${PVEOSI_INSTALL_ROOT:-/usr/local/lib/proxmox-os-installer}"
ENTRY="${PVEOSI_INSTALL_ENTRY:-/usr/local/sbin/proxmox-os-installer}"
PVEOSI_PROJECT_BIN="$TARGET/bin/proxmox-os-installer"
PVEOSI_ALIAS_BINDIR="${PVEOSI_ALIAS_BINDIR:-/usr/local/sbin}"
METADATA="$TARGET/install-metadata.json"

# shellcheck source=lib/install_aliases.sh
source "$REPO/lib/install_aliases.sh"

if [[ "${PVEOSI_SKIP_FILE_COPY:-0}" != 1 ]]; then
  python3 "$REPO/lib/image_catalog.py" catalog-check "$REPO/catalog/os-images.tsv"
  install -d -m 0755 "$TARGET"/{bin,catalog,lib} /usr/local/sbin
  install -m 0755 "$REPO/bin/proxmox-os-installer" "$PVEOSI_PROJECT_BIN"
  install -m 0644 "$REPO/catalog/os-images.tsv" "$TARGET/catalog/os-images.tsv"
  for py in image_catalog.py preflight.py cache_ops.py ssh_access.py update_engine.py; do
    install -m 0644 "$REPO/lib/$py" "$TARGET/lib/$py"
  done
  for sh in common.sh interactive.sh install_aliases.sh snippets_guidance.sh; do
    install -m 0644 "$REPO/lib/$sh" "$TARGET/lib/$sh"
  done
  install -m 0644 "$REPO/VERSION" "$TARGET/VERSION"
  install -m 0755 "$REPO/install.sh" "$TARGET/install.sh"
fi

if [[ "${PVEOSI_SKIP_MAIN_ENTRY:-0}" != 1 ]]; then
  pveosi_safe_install_main_entry "$ENTRY" || exit 1
fi

if [[ "${PVEOSI_SKIP_ALIASES:-0}" != 1 ]]; then
  SELECTED_CMD="$(pveosi_install_short_aliases)"
else
  SELECTED_CMD=""
fi

if [[ "${PVEOSI_WRITE_METADATA:-1}" == 1 ]]; then
  INSTALL_VER="${PVEOSI_INSTALL_VERSION:-$(tr -d '[:space:]' < "$REPO/VERSION" 2>/dev/null || true)}"
  INSTALL_COMMIT="${PVEOSI_INSTALL_COMMIT:-}"
  INSTALL_REF="${PVEOSI_INSTALL_REF:-${PROXMOX_OS_INSTALLER_REF:-main}}"
  INSTALL_REPO="${PVEOSI_INSTALL_REPO:-https://github.com/faridze/proxmox-os-installer}"
  INSTALL_TS="${PVEOSI_INSTALL_TIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  python3 - "$METADATA" "$INSTALL_VER" "$INSTALL_COMMIT" "$INSTALL_REPO" "$INSTALL_REF" "$INSTALL_TS" <<'PY'
import json, os, sys
path, ver, commit, repo, ref, ts = sys.argv[1:7]
data = {
    "version": ver or "unknown",
    "commit_sha": commit or None,
    "repository": repo,
    "install_ref": ref,
    "channel": ref,
    "installed_at": ts,
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, sort_keys=True)
    f.write("\n")
os.chmod(path, 0o644)
PY
fi

if [[ "${PVEOSI_SKIP_FILE_COPY:-0}" != 1 ]]; then
  "$PVEOSI_PROJECT_BIN" --check-catalog
fi
VERSION_OUT="$("$PVEOSI_PROJECT_BIN" --version)"
if [[ "${PVEOSI_QUIET_INSTALL:-0}" == 1 ]]; then
  exit 0
fi
if [[ -n "$SELECTED_CMD" ]]; then
  printf 'Installed %s. Run: %s (also: proxmox-os-installer)\n' "$VERSION_OUT" "$SELECTED_CMD"
else
  printf 'Installed %s. Run: proxmox-os-installer\n' "$VERSION_OUT"
fi
