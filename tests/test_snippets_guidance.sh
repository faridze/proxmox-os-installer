#!/usr/bin/env bash
# Snippets storage guidance, early validation, and secret-handling regressions (mocked).
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP snippets guidance tests (requires root)' >&2; exit 0; }
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/cache" "$TMP/snippets" "$TMP/alt/snippets"
STORAGE_CFG="$TMP/storage.cfg"
: >"$STORAGE_CFG"
IMG="$TMP/cache/debian-12-debian-12-genericcloud-amd64.qcow2"
printf 'cloudimage-stub' > "$IMG"
HASH512="$(sha512sum "$IMG" | awk '{print $1}')"

write_mocks() {
  local snippet_mode="$1" # none|local|nfs|local_only_lvm
  cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
case "$2" in
  /cluster/nextid*) printf '"701"\n';;
  /cluster/resources*) printf '[]\n';;
  *) exit 91;;
esac
EOF
  cat > "$TMP/bin/pvesm" <<EOF
#!/usr/bin/env bash
case "\$1" in
  status)
    if [[ "\${2:-}" == --content && "\${3:-}" == snippets ]]; then
      echo 'Name Type Status'
      case "$snippet_mode" in
        local) echo 'local dir active';;
        nfs) echo 'nfs-store nfs active';;
        none) ;;
      esac
      exit 0
    fi
    echo 'Name Type Status Total Used Available %'
    echo 'local-lvm lvmthin active 10000000 1000 9000000 1%'
    case "$snippet_mode" in
      local|nfs) echo 'local dir active 10000000 1000 9000000 1%';;
      local_only_lvm) ;;
    esac
    if [[ "$snippet_mode" == nfs ]]; then
      echo 'nfs-store nfs active 10000000 1000 9000000 1%'
    fi
    ;;
  path)
    case "\$2" in
      local:snippets/*) printf '%s/snippets/%s\n' "\$TEST_ROOT" "\${2#local:snippets/}";;
      nfs-store:snippets/*) printf '%s/alt/snippets/%s\n' "\$TEST_ROOT" "\${2#nfs-store:snippets/}";;
      *) exit 92;;
    esac
    ;;
  *) exit 93;;
esac
EOF
  cat > "$TMP/bin/qm" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  config)
    if [[ -f "$TEST_ROOT/qm.cfg" ]]; then cat "$TEST_ROOT/qm.cfg"; exit 0; fi
    printf "Configuration file '/etc/pve/qemu-server/701.conf' does not exist\n" >&2
    exit 2;;
  create) echo 'name: test' > "$TEST_ROOT/qm.cfg";;
  set)
    case "$3" in
      --scsi0) echo 'scsi0: local-lvm:vm-701-disk-0' >> "$TEST_ROOT/qm.cfg";;
      --ide2) echo 'ide2: local-lvm:cloudinit' >> "$TEST_ROOT/qm.cfg";;
      --ciuser) printf '%s' "$4" > "$TEST_ROOT/user";;
      --cicustom) echo "cicustom: $4" >> "$TEST_ROOT/qm.cfg";;
    esac;;
  cloudinit)
    [[ "$2" == dump && "$4" == user ]] || exit 94
    echo '#cloud-config'
    echo 'hostname: test'
    printf 'user: %s\n' "$(cat "$TEST_ROOT/user")"
    echo 'password: $6$TEST_HASH_ONLY'
    echo 'chpasswd:'
    echo '  expire: false'
    echo 'users:'
    echo '  - default';;
  resize|start) :;;
  *) exit 95;;
esac
EOF
  cat > "$TMP/bin/curl" <<EOC
#!/usr/bin/env bash
out=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    -o) out="\$2"; shift 2;;
    *) shift;;
  esac
done
printf '%s  debian-12-genericcloud-amd64.qcow2\n' "$HASH512" > "\$out"
EOC
  cat > "$TMP/bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == info ]] || exit 96
printf '{"format":"qcow2","virtual-size":3221225472,"backing-filename":null}\n'
EOF
  cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
  chmod +x "$TMP/bin/"*
}

inputs_missing_snippets() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n' # default user; no SSH public key
  printf 'y\nn\n' # SSH password login; keep debian user (snippet check runs before password)
}

inputs_ssh_key_only() {
  printf '1\n1\n702\nkeyvm\n\n\n\n\n\n\n'
  printf 'debian\nssh-ed25519 AAAAcomment\n' # user + inline key (dry path uses validate only in dry-run)
  printf 'n\nn\n' # no console password, no create - actually need dry-run
}

export PATH="$TMP/bin:$PATH"
export TEST_ROOT="$TMP"
export PROXMOX_OS_INSTALLER_CACHE="$TMP/cache"

# 1) Missing snippets: actionable guidance before VM creation
write_mocks none
inputs_missing_snippets | "$REPO/bin/proxmox-os-installer" >"$TMP/miss.log" 2>&1 || true
grep -q 'Cloud-Init Snippets support' "$TMP/miss.log" || { cat "$TMP/miss.log"; exit 1; }
grep -q 'pvesm status --content snippets' "$TMP/miss.log" || exit 1
grep -q 'No VM was created by this failed attempt' "$TMP/miss.log" || exit 1
grep -q 'Datacenter > Storage' "$TMP/miss.log" || exit 1
! grep -q 'SECRET_PW_A' "$TMP/miss.log" || exit 1
echo 'OK: missing snippets guidance'

# 2) local with snippets succeeds (minimal path)
write_mocks local
inputs_local() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf 'y\ny\ny\n\n'
  printf 'PW_LOCAL\nPW_LOCAL\n'
  printf 'y\nn\n'
}
inputs_local | "$REPO/bin/proxmox-os-installer" >"$TMP/local.log" 2>&1 || true
SNIP="$TMP/snippets/pveosi-701-user.yaml"
[[ -f "$SNIP" ]] || { cat "$TMP/local.log"; exit 1; }
[[ "$(stat -c '%a' "$SNIP")" == 600 ]]
! grep -q 'PW_LOCAL' "$TMP/local.log" "$SNIP" || exit 1
grep -q 'password: \$6' "$SNIP" || exit 1
echo 'OK: local snippets install'

# 3) Alternative NFS snippets storage
write_mocks nfs
inputs_nfs() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf 'y\ny\ny\n'
  printf 'nfs-store\n'
  printf 'PW_NFS\nPW_NFS\n'
  printf 'y\nn\n'
}
inputs_nfs | "$REPO/bin/proxmox-os-installer" >"$TMP/nfs.log" 2>&1 || true
[[ -f "$TMP/alt/snippets/pveosi-701-user.yaml" ]] || { cat "$TMP/nfs.log"; exit 1; }
echo 'OK: nfs snippets storage'

# 4) local-lvm alone cannot satisfy snippets (error before VM)
write_mocks local_only_lvm
inputs_snip_fail() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'
  printf 'y\nn\n'
}
inputs_snip_fail | "$REPO/bin/proxmox-os-installer" >"$TMP/lvm.log" 2>&1 || true
grep -q 'local-lvm' "$TMP/lvm.log" || grep -q 'Snippets support' "$TMP/lvm.log" || exit 1
! grep -q 'SUCCESS: VM' "$TMP/lvm.log" || exit 1
echo 'OK: local-lvm not used for snippets'

# 5) SSH-key-only dry-run does not require snippets
write_mocks none
export DRY_RUN=1
KEYFILE="$TMP/test.pub"
printf 'ssh-ed25519 eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg= t@h\n' >"$KEYFILE"
inputs_key_dry() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf 'debian\n%s\n' "$KEYFILE"
  printf 'n\n'
}
inputs_key_dry | "$REPO/bin/proxmox-os-installer" --dry-run >"$TMP/dry.log" 2>&1 || true
unset DRY_RUN
! grep -q 'Cloud-Init Snippets support' "$TMP/dry.log" || exit 1
echo 'OK: ssh-key dry-run skips snippets'

# 8) installer cleanup trap removes temporary cloudinit dump paths (no stray tmp.* in install tmp)
grep -q 'TMP_USER_DUMP' "$REPO/bin/proxmox-os-installer" || exit 1
grep -q 'rm -f -- "\$TMP_USER_DUMP"' "$REPO/bin/proxmox-os-installer" || exit 1
echo 'OK: temp dump lifecycle wired in installer trap'

# 9) installer does not delete persistent snippets
! grep -qE 'rm .*pveosi-.*user\.yaml' "$REPO/bin/proxmox-os-installer" "$REPO/lib/ssh_access.py" || exit 1

# 10) storage.cfg untouched
[[ "$(cat "$STORAGE_CFG")" == "" ]] || exit 1
! grep -q '/etc/pve/storage.cfg' "$REPO/bin/proxmox-os-installer" "$REPO/lib/"*.sh || exit 1

echo 'OK: snippets guidance and secret-handling regressions'
