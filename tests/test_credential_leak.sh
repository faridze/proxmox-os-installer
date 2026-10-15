#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP credential leak test (requires root)' >&2; exit 0; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
SECRET='PVEOSI_TEST_SECRET_NEVER_LOG_9281'
TMP="$(mktemp -d)"
CACHE="$TMP/cache"
LOG="$TMP/combined.log"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$CACHE"
IMG="$CACHE/debian-12-debian-12-genericcloud-amd64.qcow2"
printf 'qcow2-placeholder' > "$IMG"
HASH512="$(sha512sum "$IMG" | awk '{print $1}')"

cat > "$TMP/bin/qm" <<EOF
#!/usr/bin/env bash
CFG="\${QM_CFG}"
case "\$1" in
  config)
    if [[ -f "\$CFG" ]]; then cat "\$CFG"; exit 0; fi
    printf 'Configuration file does not exist\n' >&2
    exit 2
    ;;
  create) : > "\$CFG"; printf 'name: mock\n' >> "\$CFG"; exit 0;;
  set)
    if [[ "\$*" == *--cipassword* ]]; then
      printf 'simulated failure for cipassword\n' >&2
      exit 5
    fi
    if [[ "\$*" == *import-from* ]]; then printf 'scsi0: local-lvm:disk\n' >> "\$CFG"; fi
    if [[ "\$*" == *cloudinit* ]]; then printf 'ide2: local-lvm:cloudinit\n' >> "\$CFG"; fi
    exit 0;;
  resize|start) exit 0;;
esac
EOF
cp "$REPO/tests/lib/mock_pvesm.sh" "$TMP/bin/pvesm"
cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
printf '"501"\n'
EOF
cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
cat > "$TMP/bin/curl" <<EOF
#!/usr/bin/env bash
out=""; while [[ \$# -gt 0 ]]; do case "\$1" in -o) out="\$2"; shift 2;; *) shift;; esac; done
printf '%s  debian-12-genericcloud-amd64.qcow2\n' "$HASH512" > "\$out"
EOF
cat > "$TMP/bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == info ]] && printf '{"format":"qcow2","virtual-size":3221225472,"backing-filename":null}\n'
EOF
chmod +x "$TMP/bin/"*

export QM_LOG="$TMP/qm.log" QM_CFG="$TMP/qm.cfg"
export PROXMOX_OS_INSTALLER_CACHE="$CACHE" PATH="$TMP/bin:$PATH"

{
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf '%s\n%s\n' "$SECRET" "$SECRET"
  printf 'y\n'
} | "$REPO/bin/proxmox-os-installer" >"$LOG" 2>&1 || true

leaked=0
grep -q "$SECRET" "$LOG" 2>/dev/null && leaked=1
[[ -f "$TMP/qm.log" ]] && grep -q "$SECRET" "$TMP/qm.log" 2>/dev/null && leaked=1
if (( leaked )); then
  echo "SECRET LEAKED in installer output" >&2
  exit 1
fi
if grep -q -- '--cipassword' "$LOG" 2>/dev/null; then
  echo "cipassword argument leaked in output" >&2
  exit 1
fi
grep -q 'cloud-init login password configuration' "$LOG" || {
  echo "expected password failure message missing:" >&2
  cat "$LOG" >&2
  exit 1
}
echo 'OK: credential non-disclosure regression'
