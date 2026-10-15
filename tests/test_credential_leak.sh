#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP credential leak test (requires root)' >&2; exit 0; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
SECRET='PVEOSI_TEST_SECRET_NEVER_LOG_9281'

run_case() {
  local case_id="$1" stderr_payload="$2"
  local TMP CACHE LOG
  TMP="$(mktemp -d)"
  CACHE="$TMP/cache"
  LOG="$TMP/combined.log"
  mkdir -p "$TMP/bin" "$CACHE"
  local IMG="$CACHE/debian-12-debian-12-genericcloud-amd64.qcow2"
  printf 'qcow2-placeholder' > "$IMG"
  local HASH512
  HASH512="$(sha512sum "$IMG" | awk '{print $1}')"

  cat > "$TMP/bin/qm" <<EOF
#!/usr/bin/env bash
CFG="\${QM_CFG}"
case "\$1" in
  config)
    if [[ -f "\$CFG" ]]; then cat "\$CFG"; exit 0; fi
    printf 'Configuration file '\''nodes/pve/qemu-server/501.conf'\'' does not exist\n' >&2
    exit 2
    ;;
  create) : > "\$CFG"; printf 'name: mock\n' >> "\$CFG"; exit 0;;
  set)
    if [[ "\$*" == *--cipassword* ]]; then
      printf '%s\n' "$stderr_payload" >&2
      exit 5
    fi
    if [[ "\$*" == *import-from* ]]; then printf 'scsi0: local-lvm:disk\n' >> "\$CFG"; fi
    if [[ "\$*" == *cloudinit* ]]; then printf 'ide2: local-lvm:cloudinit\n' >> "\$CFG"; fi
    exit 0;;
  resize|start) exit 0;;
esac
EOF
  cp "$REPO/tests/lib/mock_pvesm.sh" "$TMP/bin/pvesm"
  chmod +x "$TMP/bin/pvesm"
  cat > "$TMP/bin/pvesh" <<'EOF'
#!/bin/bash
case "$1" in get)
  case "$2" in
    /cluster/resources*) printf '[]\n'; exit 0 ;;
    /cluster/nextid*) printf '"501"\n'; exit 0 ;;
  esac ;;
esac
printf '"501"\n'
EOF
  printf '#!/bin/bash\n[[ "$*" == "link show dev vmbr0" ]]\n' > "$TMP/bin/ip"
  cat > "$TMP/bin/curl" <<CEOF
#!/bin/bash
out=""; while [[ \$# -gt 0 ]]; do case "\$1" in -o) out="\$2"; shift 2;; *) shift;; esac; done
printf '%s  debian-12-genericcloud-amd64.qcow2\n' "$HASH512" > "\$out"
CEOF
  cat > "$TMP/bin/qemu-img" <<'EOF'
#!/bin/bash
if [[ "$1" == info ]]; then
  printf '{"format":"qcow2","virtual-size":3221225472,"backing-filename":null}\n'
fi
EOF
  chmod +x "$TMP/bin/"*

  export QM_CFG="$TMP/qm.cfg" PROXMOX_OS_INSTALLER_CACHE="$CACHE" PATH="$TMP/bin:$PATH"
  {
    printf '1\n1\n'
    for _ in {1..5}; do printf '\n'; done
    for _ in {1..4}; do printf '\n'; done
    printf '\n\n'
    printf 'n\n'
    printf '%s\n%s\n' "$SECRET" "$SECRET"
    printf 'y\n'
  } | "$REPO/bin/proxmox-os-installer" >"$LOG" 2>&1 || true

  local leaked=0
  grep -q "$SECRET" "$LOG" 2>/dev/null && leaked=1
  grep -q -- '--cipassword' "$LOG" 2>/dev/null && leaked=1
  if (( leaked )); then
    echo "SECRET LEAKED in case $case_id" >&2
    cat "$LOG" >&2
    rm -rf "$TMP"
    return 1
  fi
  grep -q 'cloud-init login password configuration' "$LOG" || {
    echo "missing safe failure message in case $case_id" >&2
    cat "$LOG" >&2
    rm -rf "$TMP"
    return 1
  }
  grep -q 'Command output suppressed' "$LOG" || {
    echo "missing suppression notice in case $case_id" >&2
    rm -rf "$TMP"
    return 1
  }
  rm -rf "$TMP"
  return 0
}

run_case plain "simulated failure for cipassword"
run_case echo_secret "failed: password was ${SECRET} on stderr"
run_case argv_style "qm set 501 --cipassword ${SECRET} failed"
run_case jsonish "{\"cipassword\":\"${SECRET}\"}"

echo 'OK: credential non-disclosure regression (multiple stderr formats)'
