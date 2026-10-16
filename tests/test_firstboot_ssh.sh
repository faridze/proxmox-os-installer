#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP first-boot SSH integration mock (requires root)' >&2; exit 0; }
REPO="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/cache" "$TMP/snippets"
IMG="$TMP/cache/debian-12-debian-12-genericcloud-amd64.qcow2"
printf 'cloudimage-stub' > "$IMG"
HASH512="$(sha512sum "$IMG" | awk '{print $1}')"
cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
case "$2" in
  /cluster/nextid*) printf '"603"\n';;
  /cluster/resources*) printf '[]\n';;
  *) exit 91;;
esac
EOF
cat > "$TMP/bin/pvesm" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  status)
    if [[ "${2:-}" == --content && "${3:-}" == snippets ]]; then
      echo 'Name Type Status'; echo 'local dir active'; exit 0
    fi
    if [[ "${2:-}" == --content ]]; then
      echo 'Name Type Status'; echo 'local-lvm lvmthin active'; exit 0
    fi
    echo 'Name Type Status Total Used Available %'
    echo 'local-lvm lvmthin active 10000000 1000 9000000 1%'
    echo 'local dir active 10000000 1000 9000000 1%'
    ;;
  path)
    [[ "$2" == 'local:snippets/pveosi-603-user.yaml' ]] || exit 92
    printf '%s/snippets/pveosi-603-user.yaml\n' "$TEST_ROOT";;
  *) exit 93;;
esac
EOF
cat > "$TMP/bin/qm" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  config)
    if [[ -f "$TEST_ROOT/qm.cfg" ]]; then cat "$TEST_ROOT/qm.cfg"; exit 0; fi
    printf "Configuration file '/etc/pve/qemu-server/603.conf' does not exist\n" >&2
    exit 2;;
  create)
    echo 'name: test' > "$TEST_ROOT/qm.cfg";;
  set)
    case "$3" in
      --scsi0) echo 'scsi0: local-lvm:vm-603-disk-0' >> "$TEST_ROOT/qm.cfg";;
      --ide2) echo 'ide2: local-lvm:cloudinit' >> "$TEST_ROOT/qm.cfg";;
      --ciuser) printf '%s' "$4" > "$TEST_ROOT/user";;
      --cicustom)
        echo "cicustom: $4" >> "$TEST_ROOT/qm.cfg";;
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
[[ "$1" == info ]] || exit 96
printf '{"format":"qcow2","virtual-size":3221225472,"backing-filename":null}\n'
EOF
cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
chmod +x "$TMP/bin/"*
export PATH="$TMP/bin:$PATH"
export TEST_ROOT="$TMP"
export PROXMOX_OS_INSTALLER_CACHE="$TMP/cache"
inputs() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\n'  # default user; no public SSH key
  printf 'y\ny\ny\n' # SSH password login; switch to root; allow root SSH
  printf '\n' # snippets storage: local
  printf 'LOCAL_TEST_PASSWORD\nLOCAL_TEST_PASSWORD\n'
  printf '\n'
  printf 'y\nn\n' # create VM; do not start
}
inputs | "$REPO/bin/proxmox-os-installer" >"$TMP/console.log" 2>&1
SNIP="$TMP/snippets/pveosi-603-user.yaml"
[[ -f "$SNIP" ]] || { cat "$TMP/console.log"; exit 1; }
[[ "$(stat -c '%a' "$SNIP")" == 600 ]]
grep -q '^user: root$' "$SNIP"
grep -q '^ssh_pwauth: true$' "$SNIP"
grep -q '^disable_root: false$' "$SNIP"
grep -q 'PermitRootLogin yes' "$SNIP"
grep -q 'cicustom: user=local:snippets/pveosi-603-user.yaml' "$TMP/qm.cfg"
if grep -q LOCAL_TEST_PASSWORD "$TMP/console.log" "$SNIP"; then
  echo 'ERROR: password appeared in output or snippet' >&2
  exit 1
fi
echo 'OK: first-boot root/password SSH flow (mock only; no real VM)'
