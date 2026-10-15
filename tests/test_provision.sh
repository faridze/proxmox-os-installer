#!/usr/bin/env bash
# Mocked Proxmox provisioning scenarios (no real VM creation).
set -Eeuo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

write_qm_mock() {
  local script="$TMP/bin/qm"
  cat > "$script" <<'EOF'
#!/usr/bin/env bash
LOG="${QM_LOG:-/tmp/qm.log}"
printf '%s\n' "$*" >> "$LOG"
case "$1" in
  config)
    if [[ "${QM_CONFIG_RC:-}" == 0 ]]; then exit 0; fi
    printf 'Configuration file does not exist\n' >&2
    exit 2
    ;;
  create) exit "${QM_CREATE_RC:-0}";;
  set)
    if [[ "${QM_FAIL_SET:-}" == "scsi0" && "$*" == *import-from* ]]; then exit 12; fi
    if [[ "${QM_FAIL_SET:-}" == "cloudinit" && "$*" == *cloudinit* ]]; then exit 13; fi
    exit "${QM_SET_RC:-0}";;
  resize) exit "${QM_RESIZE_RC:-0}";;
  start) exit "${QM_START_RC:-0}";;
  *) exit 0;;
esac
EOF
  chmod +x "$script"
}

base_mocks() {
  mkdir -p "$TMP/bin"
  write_qm_mock
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
  export PATH="$TMP/bin:$PATH"
}

inputs_default() {
  printf '1\n1\n' # Debian 12
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf 'n\n'
  printf 'pw\npw\n'
}

run_dry() {
  QM_LOG="$TMP/qm-$1.log"
  inputs_default | "$REPO/bin/proxmox-os-installer" --dry-run >"$TMP/out-$1.txt" 2>&1
}

base_mocks
run_dry basic
grep -q 'Stage 1/8' "$TMP/out-basic.txt"
grep -q 'DRY RUN' "$TMP/out-basic.txt"

# Existing VM ID rejection (cluster resources reports vmid 200 in use)
cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
case "$1" in get)
  case "$2" in
    /cluster/resources*) printf '[{"vmid":200}]\n'; exit 0 ;;
    /cluster/nextid*) printf '"200"\n'; exit 0 ;;
  esac ;;
esac
printf '"200"\n'
EOF
chmod +x "$TMP/bin/pvesh"
export PATH="$TMP/bin:$PATH"
if inputs_default | "$REPO/bin/proxmox-os-installer" --dry-run 2>"$TMP/exists.err"; then
  echo 'expected failure for existing VMID' >&2; exit 1
fi
grep -q 'already in use' "$TMP/exists.err"
cp "$REPO/tests/lib/mock_pvesh.sh" "$TMP/bin/pvesh"
chmod +x "$TMP/bin/pvesh"

# Storage validation failure
cat > "$TMP/bin/pvesm" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  status)
    if [[ "${2:-}" == --content ]]; then
      printf 'Name Type Status\nlocal-lvm dir active\n'; exit 0
    fi
    printf 'Name Type Status Total Used Available %\nlocal-lvm dir active 10000000 9999999 1000 99%\n'
    exit 0
    ;;
esac
exit 98
EOF
chmod +x "$TMP/bin/pvesm"
export PATH="$TMP/bin:$PATH"
if inputs_default | "$REPO/bin/proxmox-os-installer" --dry-run 2>"$TMP/storage.err"; then
  echo 'expected storage preflight failure' >&2; exit 1
fi
grep -qi 'storage' "$TMP/storage.err"

echo 'OK: provision mock tests'
