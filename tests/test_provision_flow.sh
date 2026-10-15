#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP provision flow (requires root)' >&2; exit 0; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
CACHE="$TMP/cache"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$CACHE"
IMG="$CACHE/debian-12-debian-12-genericcloud-amd64.qcow2"
printf 'qcow2-placeholder' > "$IMG"
HASH512="$(sha512sum "$IMG" | awk '{print $1}')"

cat > "$TMP/bin/qm" <<'EOF'
#!/usr/bin/env bash
LOG="${QM_LOG}"
CFG="${QM_CFG:-/tmp/qm-mock.cfg}"
printf '%s\n' "$*" >> "$LOG"
case "$1" in
  config)
    if [[ ! -f "$CFG" ]]; then
      printf 'Configuration file does not exist\n' >&2
      exit 2
    fi
    cat "$CFG"
    exit 0
    ;;
  create)
    : > "$CFG"
    printf 'name: mock\n' >> "$CFG"
    exit 0
    ;;
  set)
    if [[ "$*" == *import-from* ]]; then printf 'scsi0: local-lvm:vm-301-disk-0\n' >> "$CFG"; fi
    if [[ "$*" == *cloudinit* ]]; then printf 'ide2: local-lvm:cloudinit\n' >> "$CFG"; fi
    exit 0
    ;;
  resize|start) exit 0;;
  *) exit 0;;
esac
EOF
cp "$REPO/tests/lib/mock_pvesm.sh" "$TMP/bin/pvesm"
cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
printf '"301"\n'
EOF
cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
cat > "$TMP/bin/curl" <<EOF
#!/usr/bin/env bash
out=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    -o) out="\$2"; shift 2;;
    *) shift;;
  esac
done
printf '%s  debian-12-genericcloud-amd64.qcow2\n' "$HASH512" > "\$out"
EOF
cat > "$TMP/bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == info ]]; then
  printf '{"format":"qcow2","virtual-size":3221225472,"backing-filename":null}\n'
fi
EOF
chmod +x "$TMP/bin/"*

export QM_LOG="$TMP/qm.log"
export QM_CFG="$TMP/qm.cfg"
export PROXMOX_OS_INSTALLER_CACHE="$CACHE"
export PATH="$TMP/bin:$PATH"

{
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf 'pw\npw\n'
  printf 'y\ny\n'
} | "$REPO/bin/proxmox-os-installer" >"$TMP/out.txt" 2>&1

grep -q 'SUCCESS: VM 301' "$TMP/out.txt"
grep -q '^create ' "$TMP/qm.log"
grep -q 'import-from' "$TMP/qm.log"
grep -q 'cloudinit' "$TMP/qm.log"
grep -q '^start ' "$TMP/qm.log"
echo 'OK: full mocked provision command order'
