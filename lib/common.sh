# shellcheck shell=bash
# Shared shell helpers for proxmox-os-installer
# shellcheck source=lib/snippets_guidance.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/snippets_guidance.sh"
error() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> [%s] %s\n' "$STAGE_LABEL" "$*"; }
stage() {
  STAGE_NUM=$((STAGE_NUM + 1))
  STAGE_LABEL="Stage ${STAGE_NUM}/8"
  info "$1"
}

# Read Proxmox stderr for user-facing errors without echoing secrets from argv.
_sanitize_proxmox_stderr() {
  local f="$1"
  [[ -f "$f" && -s "$f" ]] || return 0
  sed -E \
    -e 's/--cipassword[[:space:]]+[^[:space:]]+/--cipassword [REDACTED]/g' \
    -e 's/(password[[:space:]]*[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED]/gi' \
    "$f" | head -n 40
}

provisioning_failed() {
  local op="$1" detail="${2:-}"
  printf '\nERROR: Provisioning failed during: %s\n' "$op" >&2
  if [[ -n "$detail" ]]; then
    printf 'Detail:\n%s\n' "$detail" >&2
  fi
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

# Run qm with a fixed operation label (never include secret arguments in labels).
qm_run() {
  local operation="$1"
  shift
  local stderr_file rc=0
  stderr_file="$(mktemp)"
  chmod 0600 "$stderr_file"
  qm "$@" >/dev/null 2>"$stderr_file" || rc=$?
  if (( rc != 0 )); then
    local detail
    detail="$(_sanitize_proxmox_stderr "$stderr_file")"
    rm -f -- "$stderr_file"
    provisioning_failed "$operation" "$detail"
  fi
  rm -f -- "$stderr_file"
}

# Documented Proxmox mechanism; password still visible in process list while qm runs.
qm_set_cloud_init_password() {
  local vmid="$1" stderr_file rc=0
  stderr_file="$(mktemp)"
  chmod 0600 "$stderr_file"
  qm set "$vmid" --cipassword "$PASSWORD" >/dev/null 2>"$stderr_file" || rc=$?
  rm -f -- "$stderr_file"
  if (( rc != 0 )); then
    provisioning_failed "cloud-init login password configuration for VM ${vmid}" \
      "Proxmox qm exited with status ${rc}. Command output suppressed to avoid leaking credentials."
  fi
}

assert_vmid_free() {
  local rc=0
  python3 "$PREFLIGHT" vmid-free "$VMID" || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) provisioning_failed "VM ID ${VMID} is already in use" ;;
    *) provisioning_failed "unable to verify VM ID ${VMID} availability (fail closed)" ;;
  esac
}
