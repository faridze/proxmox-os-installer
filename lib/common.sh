# Shared shell helpers for proxmox-os-installer
error() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> [%s] %s\n' "$STAGE_LABEL" "$*"; }
stage() {
  STAGE_NUM=$((STAGE_NUM + 1))
  STAGE_LABEL="Stage ${STAGE_NUM}/8"
  info "$1"
}

provisioning_failed() {
  local op="$1" detail="${2:-}"
  printf '\nERROR: Provisioning failed during: %s\n' "$op" >&2
  [[ -n "$detail" ]] && printf '%s\n' "$detail" >&2
  if [[ -n "$VMID" ]]; then
    printf 'Affected VM ID: %s\n' "$VMID" >&2
    if (( CREATED == 1 )); then
      printf 'The VM may be partially configured. It was NOT automatically deleted.\n' >&2
      printf 'Inspect: qm config %s\n' "$VMID" >&2
      printf 'Start only after review: qm start %s\n' "$VMID" >&2
      printf 'To remove manually (only if you intend to destroy this VM): qm destroy %s --purge\n' "$VMID" >&2
    fi
  fi
  exit 1
}

run_qm() {
  local out
  if ! out="$(qm "$@" 2>&1)"; then
    provisioning_failed "qm $*" "$out"
  fi
}

assert_vmid_free() {
  python3 "$PREFLIGHT" vmid-free "$VMID" || provisioning_failed "VM ID $VMID is already in use"
}
