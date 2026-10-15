#!/usr/bin/env bash
# Install/update from a reviewed working tree. No apt changes, no remote execution.
set -Eeuo pipefail
(( EUID == 0 )) || { printf 'ERROR: run as root.\n' >&2; exit 1; }
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET=/usr/local/lib/proxmox-os-installer
ENTRY=/usr/local/sbin/proxmox-os-installer
python3 "$REPO/lib/image_catalog.py" catalog-check "$REPO/catalog/os-images.tsv"
install -d -m 0755 "$TARGET"/{bin,catalog,lib} /usr/local/sbin
install -m 0755 "$REPO/bin/proxmox-os-installer" "$TARGET/bin/proxmox-os-installer"
install -m 0644 "$REPO/catalog/os-images.tsv" "$TARGET/catalog/os-images.tsv"
for py in image_catalog.py preflight.py cache_ops.py; do
  install -m 0644 "$REPO/lib/$py" "$TARGET/lib/$py"
done
for sh in common.sh interactive.sh; do
  install -m 0644 "$REPO/lib/$sh" "$TARGET/lib/$sh"
done
install -m 0644 "$REPO/VERSION" "$TARGET/VERSION"
ln -sfn "$TARGET/bin/proxmox-os-installer" "$ENTRY"
"$ENTRY" --check-catalog
printf 'Installed %s. Run: proxmox-os-installer\n' "$("$ENTRY" --version)"
