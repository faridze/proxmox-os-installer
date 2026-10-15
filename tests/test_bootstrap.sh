#!/usr/bin/env bash
# Regression: Bootstrap must execute a non-executable install.sh from an archive.
set -Eeuo pipefail
[[ "$EUID" == 0 ]] || { echo 'SKIP bootstrap test (requires root)' >&2; exit 0; }
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/source-tree/example-archive"
cat > "$TMP/source-tree/example-archive/install.sh" <<'INSTALL'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'INSTALLED\n' > "$TEST_MARKER"
INSTALL
printf '0.0.0-test\n' > "$TMP/source-tree/example-archive/VERSION"
chmod 0644 "$TMP/source-tree/example-archive/install.sh"
tar -czf "$TMP/fixture.tar.gz" -C "$TMP/source-tree" example-archive
cat > "$TMP/bin/qm" <<'MOCK_QM'
#!/usr/bin/env bash
exit 0
MOCK_QM
cat > "$TMP/bin/curl" <<'MOCK_CURL'
#!/usr/bin/env bash
set -Eeuo pipefail
out=''
while (( $# )); do
  case "$1" in
    --output|-o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$out" ]] || exit 41
cp -- "$TEST_ARCHIVE" "$out"
MOCK_CURL
chmod +x "$TMP/bin/qm" "$TMP/bin/curl"
export TEST_ARCHIVE="$TMP/fixture.tar.gz" TEST_MARKER="$TMP/marker" PATH="$TMP/bin:$PATH"
PROXMOX_OS_INSTALLER_REF=bootstrap-nonexec-test bash "$ROOT/bootstrap.sh" > "$TMP/output" 2>&1
grep -qx INSTALLED "$TEST_MARKER"
echo 'OK: bootstrap runs non-executable archive install.sh via Bash'
