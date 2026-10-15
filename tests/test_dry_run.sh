#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP dry-run mock (requires root)' >&2; exit 0; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/qm" <<'EOF'
#!/usr/bin/env bash
case "$1" in config) exit 1;; *) echo 'Unexpected mutation via qm' >&2; exit 98;; esac
EOF
cat > "$TMP/bin/pvesm" <<'EOF'
#!/usr/bin/env bash
case "$1" in status) printf '%s\n' 'Name Type Status Total Used Available %' 'local-lvm lvmthin active 1000000 100 999900 1%' ;; *) exit 98;; esac
EOF
cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
printf '"152"\n'
EOF
cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
cat > "$TMP/bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$TMP/bin/"*
# OS Debian12, all others default, no VLAN, DHCP, console password.
# 1 OS, 2 VMID, 3 NAME, 4 CORES, 5 RAM, 6 DISK, 7 STORAGE, 8 BRIDGE,
# 9 VLAN, 10 MODE, 11 USERNAME, 12 PUBKEY, 13 PASSWORD, 14 CONFIRM.
{
  printf '1\n'; for _ in {1..11}; do printf '\n'; done
  printf 'test-password\ntest-password\n'
} | PATH="$TMP/bin:$PATH" "$REPO/bin/proxmox-os-installer" --dry-run > "$TMP/log" 2>&1
cat "$TMP/log" | grep -q 'Debian 12 (Bookworm)'
cat "$TMP/log" | grep -q 'DRY RUN: no downloads'
cat "$TMP/log" | grep -q 'Disk: 150 GiB @ local-lvm'
echo 'OK: interactive dry-run mock (no provisioning performed)'
