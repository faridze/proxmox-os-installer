#!/usr/bin/env bash
# Install/update from a reviewed working tree. No apt changes, no remote execution.
set -Eeuo pipefail
(( EUID == 0 )) || { printf 'ERROR: run as root.\n' >&2; exit 1; }
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET=/usr/local/lib/proxmox-os-installer
ENTRY=/usr/local/sbin/proxmox-os-installer
PVEOSI_PROJECT_BIN="$TARGET/bin/proxmox-os-installer"
PVEOSI_ALIAS_BINDIR=/usr/local/sbin

# shellcheck source=lib/install_aliases.sh
source "$REPO/lib/install_aliases.sh"

python3 "$REPO/lib/image_catalog.py" catalog-check "$REPO/catalog/os-images.tsv"
install -d -m 0755 "$TARGET"/{bin,catalog,lib} /usr/local/sbin
install -m 0755 "$REPO/bin/proxmox-os-installer" "$PVEOSI_PROJECT_BIN"
install -m 0644 "$REPO/catalog/os-images.tsv" "$TARGET/catalog/os-images.tsv"
for py in image_catalog.py preflight.py cache_ops.py; do
  install -m 0644 "$REPO/lib/$py" "$TARGET/lib/$py"
done
for sh in common.sh interactive.sh install_aliases.sh; do
  install -m 0644 "$REPO/lib/$sh" "$TARGET/lib/$sh"
done
install -m 0644 "$REPO/VERSION" "$TARGET/VERSION"

pveosi_safe_install_main_entry "$ENTRY" || exit 1
SELECTED_CMD="$(pveosi_install_short_aliases)"
"$ENTRY" --check-catalog
VERSION_OUT="$("$ENTRY" --version)"
if [[ -n "$SELECTED_CMD" ]]; then
  printf 'Installed %s. Run: %s (also: proxmox-os-installer)\n' "$VERSION_OUT" "$SELECTED_CMD"
else
  printf 'Installed %s. Run: proxmox-os-installer\n' "$VERSION_OUT"
fi
