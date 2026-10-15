#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'SKIP failure mocks (requires root)' >&2; exit 0; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d)"
CACHE="$TMP/cache"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$CACHE"

setup_base() {
  cat > "$TMP/bin/qm" <<'EOF'
#!/usr/bin/env bash
CFG="${QM_CFG}"
case "$1" in
  config)
    if [[ -f "$CFG" ]]; then exit 0; fi
    printf 'Configuration file does not exist\n' >&2
    exit 2
    ;;
  create) exit 0;;
  set) exit "${QM_SET_RC:-0}";;
  resize) exit 0;;
  start) exit "${QM_START_RC:-0}";;
esac
EOF
  cp "$REPO/tests/lib/mock_pvesm.sh" "$TMP/bin/pvesm"
  cat > "$TMP/bin/pvesh" <<'EOF'
#!/usr/bin/env bash
case "$1" in get)
  case "$2" in
    /cluster/resources*) printf '[]\n'; exit 0 ;;
    /cluster/nextid*) printf '"401"\n'; exit 0 ;;
  esac ;;
esac
printf '"401"\n'
EOF
  cat > "$TMP/bin/ip" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == 'link show dev vmbr0' ]]
EOF
  cat > "$TMP/bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == info ]]; then
  printf '{"format":"qcow2","virtual-size":3221225472,"backing-filename":null}\n'
fi
EOF
  chmod +x "$TMP/bin/"*
}

inputs() {
  printf '1\n1\n'
  for _ in {1..5}; do printf '\n'; done
  for _ in {1..4}; do printf '\n'; done
  printf '\n\nn\npw\npw\ny\n'
}

# Checksum mismatch
setup_base
HASH=$(sha512sum "$REPO/catalog/os-images.tsv" | awk '{print $1}') # wrong content on purpose
cat > "$TMP/bin/curl" <<EOF
#!/usr/bin/env bash
out=""; while [[ \$# -gt 0 ]]; do case "\$1" in -o) out="\$2"; shift 2;; *) shift;; esac; done
printf '%s  debian-12-genericcloud-amd64.qcow2\n' "$HASH" > "\$out"
EOF
chmod +x "$TMP/bin/curl"
export PATH="$TMP/bin:$PATH" PROXMOX_OS_INSTALLER_CACHE="$CACHE" QM_CFG="$TMP/cfg"
if inputs | "$REPO/bin/proxmox-os-installer" 2>"$TMP/err.txt"; then
  echo 'expected checksum failure' >&2; exit 1
fi
grep -qi 'checksum\|SHA' "$TMP/err.txt"

# Failed import
setup_base
IMG="$CACHE/debian-12-debian-12-genericcloud-amd64.qcow2"
printf x > "$IMG"
HASH512=$(sha512sum "$IMG" | awk '{print $1}')
cat > "$TMP/bin/curl" <<EOF
#!/usr/bin/env bash
out=""; while [[ \$# -gt 0 ]]; do case "\$1" in -o) out="\$2"; shift 2;; *) shift;; esac; done
printf '%s  debian-12-genericcloud-amd64.qcow2\n' "$HASH512" > "\$out"
EOF
chmod +x "$TMP/bin/curl"
export QM_SET_RC=12
if inputs | "$REPO/bin/proxmox-os-installer" 2>"$TMP/import.err"; then
  echo 'expected import failure' >&2; exit 1
fi
grep -q 'Affected VM ID: 401' "$TMP/import.err"

echo 'OK: failure-path mocks'
