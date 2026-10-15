# Interactive prompts (two-level OS menu, storage/bridge discovery)

select_os_entry() {
  local -a families=() family_line family_key family_label count
  mapfile -t family_lines < <(python3 "$CHECKER" distro-groups "$CATALOG")
  for family_line in "${family_lines[@]}"; do
    IFS='|' read -r family_key family_label count <<< "$family_line"
    families+=("$family_key")
    printf '%2d) %s\n' "${#families[@]}" "$family_label"
  done
  local distro_pick
  distro_pick="$(ask 'Select distribution number' '1')"
  int_range "$distro_pick" 'Distribution' 1 "${#families[@]}"
  family_key="${families[$((10#$distro_pick - 1))]}"
  mapfile -t version_entries < <(grep -vE '^(#|[[:space:]]*$)' "$CATALOG" | grep "^${family_key}-")
  local i
  for i in "${!version_entries[@]}"; do
    IFS='|' read -r _ ver_label _ _ _ _ _ <<< "${version_entries[$i]}"
    printf '%2d) %s\n' "$((i + 1))" "$ver_label"
  done
  local ver_pick
  ver_pick="$(ask 'Select version number' '1')"
  int_range "$ver_pick" 'Version' 1 "${#version_entries[@]}"
  IFS='|' read -r OS_ID OS_LABEL DEFAULT_USER BASE_URL IMAGE_FILE SUMS_FILE ALGO <<< "${version_entries[$((10#$ver_pick - 1))]}"
}

pick_storage() {
  printf '\nActive storages supporting VM images:\n'
  local line default
  default="$(python3 "$PREFLIGHT" default-storage 2>/dev/null || printf 'local-lvm')"
  while IFS='|' read -r name typ avail; do
    printf '  - %s (%s, ~%s GiB free)\n' "$name" "$typ" "$avail"
  done < <(python3 "$PREFLIGHT" list-storages 2>/dev/null || true)
  STORAGE="$(ask 'Target storage (images + cloud-init)' "$default")"
  [[ "$STORAGE" =~ ^[a-zA-Z][a-zA-Z0-9_.-]*$ ]] || error 'Invalid storage ID.'
  if ! python3 "$PREFLIGHT" storage-validate "$STORAGE" "$DISK_GIB" 2> >(cat >&2); then
    error "Storage $STORAGE failed preflight validation."
  fi
}

pick_bridge() {
  local default
  default="$(python3 "$PREFLIGHT" default-bridge 2>/dev/null || printf 'vmbr0')"
  BRIDGE="$(ask 'Network bridge' "${default:-vmbr0}")"
  [[ "$BRIDGE" =~ ^[a-zA-Z0-9_.:-]+$ ]] || error 'Invalid network bridge.'
  python3 "$PREFLIGHT" bridge-exists "$BRIDGE" || error "Bridge $BRIDGE not found."
}

collect_auth() {
  USERNAME="$(ask 'Cloud-init username' "$DEFAULT_USER")"
  [[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || error 'Invalid username.'
  SSH_PUBKEY="$(ask 'SSH public key file or inline key (blank = no key)' '')"
  if [[ -n "$SSH_PUBKEY" ]]; then
    if [[ -f "$SSH_PUBKEY" && -r "$SSH_PUBKEY" ]]; then
      python3 "$PREFLIGHT" ssh-validate-file "$SSH_PUBKEY" >/dev/null || error 'Invalid SSH public key file.'
    else
      if (( DRY_RUN == 1 )); then
        python3 "$PREFLIGHT" ssh-validate-line "$SSH_PUBKEY" >/dev/null || error 'Invalid inline SSH public key.'
        SSH_INLINE_VALIDATED=1
        SSH_PUBKEY=""
      else
        TMP_SSH="$(mktemp)"
        chmod 0600 "$TMP_SSH"
        python3 "$PREFLIGHT" ssh-validate-line "$SSH_PUBKEY" > "$TMP_SSH" || error 'Invalid inline SSH public key.'
        SSH_PUBKEY="$TMP_SSH"
      fi
    fi
  fi
  PASSWORD_SET=0
  if [[ -z "$SSH_PUBKEY" ]] || ask_yes 'Also set console login password' n; then
    PASSWORD_SET=1
    read -rsp 'Login password: ' PASSWORD || error 'Input cancelled.'
    printf '\n'
    [[ -n "$PASSWORD" ]] || error 'Password cannot be empty.'
    read -rsp 'Confirm password: ' PASSWORD_CONFIRM || error 'Input cancelled.'
    printf '\n'
    [[ "$PASSWORD" == "$PASSWORD_CONFIRM" ]] || error 'Passwords do not match.'
    unset PASSWORD_CONFIRM
    printf 'Note: password may appear briefly in process listings while being applied; SSH keys are preferred.\n'
    printf 'Note: many cloud images disable password-based SSH; console/VNC may still work.\n'
  fi
}

print_summary() {
  printf '\n--------- VM SUMMARY ---------\n'
  printf 'OS: %s\nVM: %s (%s)\nCPU: %s\nRAM: %s GiB\nDisk: %s GiB @ %s\nNIC: %s\nIP: %s\nUser: %s\n' \
    "$OS_LABEL" "$VMID" "$NAME" "$CORES" "$RAM_GIB" "$DISK_GIB" "$STORAGE" "$NIC" "$IP_CONFIG" "$USERNAME"
  if [[ -n "$SSH_PUBKEY" || "${SSH_INLINE_VALIDATED:-0}" == 1 ]]; then
    printf 'SSH key: configured\n'
  else
    printf 'SSH key: none\n'
  fi
  printf 'Password set: %s\n' "$PASSWORD_SET"
  printf '%s\n' '------------------------------'
}
