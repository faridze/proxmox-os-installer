#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP dry-run mock (requires root)' >&2; exit 0; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/qm" <<'EOF'
#!/usr/bin/env bash
case "$1" in config) printf 'does not exist\n' >&2; exit 2;; *) echo 'Unexpected mutation via qm' >&2; exit 98;; esac
EOF
cp "$REPO/tests/lib/mock_pvesm.sh" "$TMP/bin/pvesm"
cp "$REPO/tests/lib/mock_pvesh.sh" "$TMP/bin/pvesh"
cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
cat > "$TMP/bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$TMP/bin/"*
{
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf 'test-password\ntest-password\nn\n'
} | PATH="$TMP/bin:$PATH" "$REPO/bin/proxmox-os-installer" --dry-run > "$TMP/log" 2>&1
grep -q 'Debian 12 (Bookworm)' "$TMP/log"
grep -q 'DRY RUN: no downloads' "$TMP/log"
grep -q 'Disk: 150 GiB @ local-lvm' "$TMP/log"
echo 'OK: interactive dry-run mock (no provisioning performed)'
