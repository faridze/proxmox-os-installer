# shellcheck shell=bash
# Collision-safe short command aliases for proxmox-os-installer (sourced by install.sh).

PVEOSI_ALIAS_PRIMARY="${PVEOSI_ALIAS_PRIMARY:-pveosi}"
PVEOSI_ALIAS_FALLBACK="${PVEOSI_ALIAS_FALLBACK:-pve-osi}"
PVEOSI_ALIAS_BINDIR="${PVEOSI_ALIAS_BINDIR:-/usr/local/sbin}"

# Directories scanned for name collisions (deduplicated).
pveosi_path_dirs() {
  local -a dirs=()
  local d
  for d in /usr/local/sbin /usr/local/bin /usr/sbin /usr/bin /sbin /bin; do
    dirs+=("$d")
  done
  if [[ -n "${PATH:-}" ]]; then
    IFS=':' read -ra _path_parts <<< "$PATH"
    for d in "${_path_parts[@]}"; do
      dirs+=("$d")
    done
  fi
  printf '%s\n' "${dirs[@]}" | awk 'NF && !seen[$0]++'
}

pveosi_real_bin() {
  readlink -f -- "${PVEOSI_PROJECT_BIN:?PVEOSI_PROJECT_BIN not set}"
}

pveosi_is_our_symlink() {
  local link="$1"
  [[ -L "$link" ]] || return 1
  [[ "$(readlink -f -- "$link")" == "$(pveosi_real_bin)" ]]
}

# Occupancy at an exact path: free | ours | foreign_file | foreign_symlink | foreign_dir
pveosi_path_state() {
  local path="$1"
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    printf 'free'
    return 0
  fi
  if [[ -d "$path" && ! -L "$path" ]]; then
    printf 'foreign_dir'
    return 0
  fi
  if [[ -L "$path" ]]; then
    if pveosi_is_our_symlink "$path"; then
      printf 'ours'
    else
      printf 'foreign_symlink'
    fi
    return 0
  fi
  printf 'foreign_file'
}

pveosi_foreign_name_in_path() {
  local name="$1"
  local skip_dir="${2:-}"
  local dir full state
  while IFS= read -r dir; do
    [[ -n "$dir" && -d "$dir" ]] || continue
    if [[ -n "$skip_dir" && "$(readlink -f -- "$dir" 2>/dev/null || printf '%s' "$dir")" == "$(readlink -f -- "$skip_dir" 2>/dev/null || printf '%s' "$skip_dir")" ]]; then
      continue
    fi
    full="${dir%/}/$name"
    state="$(pveosi_path_state "$full")"
    case "$state" in
      foreign_file|foreign_symlink|foreign_dir) return 0 ;;
    esac
  done < <(pveosi_path_dirs)
  return 1
}

pveosi_retire_our_symlink() {
  local path="$1"
  if pveosi_is_our_symlink "$path"; then
    rm -f -- "$path"
    return 0
  fi
  return 1
}

# Install or refresh our symlink at a single path. Never touches foreign paths.
pveosi_install_symlink_at() {
  local path="$1"
  local target
  target="$(pveosi_real_bin)"
  local state
  state="$(pveosi_path_state "$path")"
  case "$state" in
    free)
      ln -s -- "$target" "$path"
      return 0
      ;;
    ours)
      ln -sf -- "$target" "$path"
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

pveosi_safe_install_main_entry() {
  local entry="$1"
  local target
  target="$(pveosi_real_bin)"
  local state
  state="$(pveosi_path_state "$entry")"
  case "$state" in
    free|ours)
      ln -sf -- "$target" "$entry"
      return 0
      ;;
    *)
      printf 'ERROR: refusing to modify foreign path: %s (%s)\n' "$entry" "$state" >&2
      return 1
      ;;
  esac
}

# Returns selected short command name via stdout; warnings on stderr.
pveosi_install_short_aliases() {
  local primary="$PVEOSI_ALIAS_PRIMARY"
  local fallback="$PVEOSI_ALIAS_FALLBACK"
  local bindir="$PVEOSI_ALIAS_BINDIR"
  local primary_path="${bindir%/}/$primary"
  local fallback_path="${bindir%/}/$fallback"
  local selected="" tried_fallback=0

  install -d -m 0755 "$bindir"

  if pveosi_foreign_name_in_path "$primary" "$bindir"; then
    pveosi_retire_our_symlink "$primary_path" || true
    printf 'WARNING: %s is already provided elsewhere in PATH; skipping that alias.\n' "$primary" >&2
    tried_fallback=1
  elif pveosi_install_symlink_at "$primary_path"; then
    selected="$primary"
  else
    printf 'WARNING: cannot install %s at %s (path blocked).\n' "$primary" "$primary_path" >&2
    tried_fallback=1
  fi

  if [[ -z "$selected" ]]; then
    if pveosi_foreign_name_in_path "$fallback" "$bindir"; then
      pveosi_retire_our_symlink "$fallback_path" || true
      printf 'WARNING: %s is already provided elsewhere in PATH; skipping fallback alias.\n' "$fallback" >&2
    elif pveosi_install_symlink_at "$fallback_path"; then
      selected="$fallback"
    else
      printf 'WARNING: cannot install %s at %s (path blocked).\n' "$fallback" "$fallback_path" >&2
    fi
  else
    # Preferred alias succeeded; remove stale fallback only if we own it.
    pveosi_retire_our_symlink "$fallback_path" || true
  fi

  if [[ -z "$selected" ]]; then
    printf 'WARNING: no short alias installed; use proxmox-os-installer.\n' >&2
  elif (( tried_fallback == 1 )) && [[ "$selected" == "$fallback" ]]; then
    printf 'NOTE: using fallback command %s.\n' "$fallback" >&2
  fi
  printf '%s' "$selected"
}
