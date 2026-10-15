#!/usr/bin/env bash
# Isolated alias installation tests (no real /usr/local changes).
set -Eeuo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# shellcheck source=../lib/install_aliases.sh
source "$REPO/lib/install_aliases.sh"

setup_env() {
  rm -rf "$TMP/sbin" "$TMP/otherbin" "$TMP/lib"
  export PVEOSI_PROJECT_BIN="$TMP/lib/proxmox-os-installer/bin/proxmox-os-installer"
  export PVEOSI_ALIAS_BINDIR="$TMP/sbin"
  mkdir -p "$(dirname "$PVEOSI_PROJECT_BIN")" "$PVEOSI_ALIAS_BINDIR" "$TMP/otherbin"
  printf '#!/bin/sh\nexit 0\n' > "$PVEOSI_PROJECT_BIN"
  chmod 0755 "$PVEOSI_PROJECT_BIN"
  export PATH="$TMP/otherbin:$PVEOSI_ALIAS_BINDIR:/bin"
}

assert_eq() {
  [[ "$1" == "$2" ]] || { printf 'ASSERT FAIL: expected [%s] got [%s]\n' "$2" "$1" >&2; return 1; }
}

assert_file_exists() { [[ -e "$1" || -L "$1" ]] || { printf 'missing %s\n' "$1" >&2; return 1; }; }
assert_not_exists() { [[ ! -e "$1" && ! -L "$1" ]] || { printf 'should not exist: %s\n' "$1" >&2; return 1; }; }

test_both_aliases_available() {
  setup_env
  local sel
  sel="$(pveosi_install_short_aliases)"
  assert_eq "$sel" "pveosi"
  assert_file_exists "$PVEOSI_ALIAS_BINDIR/pveosi"
  assert_not_exists "$PVEOSI_ALIAS_BINDIR/pve-osi"
}

test_preferred_owned_by_project_idempotent() {
  setup_env
  pveosi_install_short_aliases >/dev/null
  ln -sf "$(pveosi_real_bin)" "$PVEOSI_ALIAS_BINDIR/pveosi"
  local sel
  sel="$(pveosi_install_short_aliases)"
  assert_eq "$sel" "pveosi"
  pveosi_is_our_symlink "$PVEOSI_ALIAS_BINDIR/pveosi"
}

test_preferred_occupied_foreign() {
  setup_env
  printf '#!/bin/sh\n' > "$TMP/otherbin/pveosi"
  chmod 0755 "$TMP/otherbin/pveosi"
  local sel
  sel="$(pveosi_install_short_aliases 2>"$TMP/warn.txt")"
  assert_eq "$sel" "pve-osi"
  assert_not_exists "$PVEOSI_ALIAS_BINDIR/pveosi"
  assert_file_exists "$PVEOSI_ALIAS_BINDIR/pve-osi"
  grep -q 'skipping that alias' "$TMP/warn.txt"
}

test_both_occupied() {
  setup_env
  printf '#!/bin/sh\n' > "$TMP/otherbin/pveosi"
  printf '#!/bin/sh\n' > "$TMP/otherbin/pve-osi"
  chmod 0755 "$TMP/otherbin/pveosi" "$TMP/otherbin/pve-osi"
  local sel
  sel="$(pveosi_install_short_aliases 2>"$TMP/warn2.txt")"
  assert_eq "$sel" ""
  assert_not_exists "$PVEOSI_ALIAS_BINDIR/pveosi"
  assert_not_exists "$PVEOSI_ALIAS_BINDIR/pve-osi"
  grep -q 'no short alias installed' "$TMP/warn2.txt"
}

test_foreign_symlink_at_bindir() {
  setup_env
  ln -s /bin/false "$PVEOSI_ALIAS_BINDIR/pveosi"
  local sel
  sel="$(pveosi_install_short_aliases)"
  assert_eq "$sel" "pve-osi"
  [[ "$(readlink "$PVEOSI_ALIAS_BINDIR/pveosi")" == "/bin/false" ]]
}

test_foreign_regular_file() {
  setup_env
  printf 'not a symlink\n' > "$PVEOSI_ALIAS_BINDIR/pveosi"
  local sel
  sel="$(pveosi_install_short_aliases)"
  assert_eq "$sel" "pve-osi"
  [[ -f "$PVEOSI_ALIAS_BINDIR/pveosi" ]]
}

test_foreign_directory() {
  setup_env
  mkdir "$PVEOSI_ALIAS_BINDIR/pveosi"
  local sel
  sel="$(pveosi_install_short_aliases)"
  assert_eq "$sel" "pve-osi"
  [[ -d "$PVEOSI_ALIAS_BINDIR/pveosi" ]]
}

test_repeated_install() {
  setup_env
  pveosi_install_short_aliases >/dev/null
  pveosi_install_short_aliases >/dev/null
  assert_file_exists "$PVEOSI_ALIAS_BINDIR/pveosi"
  pveosi_is_our_symlink "$PVEOSI_ALIAS_BINDIR/pveosi"
}

test_new_conflict_retires_preferred() {
  setup_env
  pveosi_install_short_aliases >/dev/null
  assert_file_exists "$PVEOSI_ALIAS_BINDIR/pveosi"
  printf '#!/bin/sh\n' > "$TMP/otherbin/pveosi"
  chmod 0755 "$TMP/otherbin/pveosi"
  local sel
  sel="$(pveosi_install_short_aliases 2>"$TMP/warn3.txt")"
  assert_eq "$sel" "pve-osi"
  assert_not_exists "$PVEOSI_ALIAS_BINDIR/pveosi"
  assert_file_exists "$PVEOSI_ALIAS_BINDIR/pve-osi"
  [[ -x "$TMP/otherbin/pveosi" ]]
}

test_main_entry_foreign_refused() {
  setup_env
  local entry="$PVEOSI_ALIAS_BINDIR/proxmox-os-installer"
  printf 'foreign\n' > "$entry"
  if pveosi_safe_install_main_entry "$entry"; then
    printf 'expected main entry install to fail\n' >&2
    return 1
  fi
  [[ -f "$entry" ]]
}

test_main_entry_ours_refreshed() {
  setup_env
  local entry="$PVEOSI_ALIAS_BINDIR/proxmox-os-installer"
  ln -s "$PVEOSI_PROJECT_BIN" "$entry"
  pveosi_safe_install_main_entry "$entry"
  pveosi_is_our_symlink "$entry"
}

failures=0
for t in test_both_aliases_available test_preferred_owned_by_project_idempotent \
  test_preferred_occupied_foreign test_both_occupied test_foreign_symlink_at_bindir \
  test_foreign_regular_file test_foreign_directory test_repeated_install \
  test_new_conflict_retires_preferred test_main_entry_foreign_refused test_main_entry_ours_refreshed; do
  if "$t"; then
    printf 'OK: %s\n' "$t"
  else
    printf 'FAIL: %s\n' "$t" >&2
    failures=$((failures + 1))
  fi
done

if (( failures > 0 )); then
  exit 1
fi
echo 'OK: all install alias collision tests'
