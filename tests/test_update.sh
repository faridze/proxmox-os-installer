#!/usr/bin/env bash
# CLI smoke tests for pveosi update (mocked network; isolated install root).
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
INSTALL="$TMP/install"
BINDIR="$TMP/sbin"
mkdir -p "$INSTALL" "$BINDIR"
export PVEOSI_INSTALL_ROOT="$INSTALL" PVEOSI_INSTALL_ENTRY="$BINDIR/proxmox-os-installer" \
  PVEOSI_ALIAS_BINDIR="$BINDIR" PVEOSI_UPDATE_ALLOW_NONROOT=1 PVEOSI_INSTALL_ALLOW_NONROOT=1 \
  PVEOSI_UPDATE_LOCK="$TMP/update.lock"

python3 "$ROOT/lib/update_engine.py" update --check 2>"$TMP/err.txt" && exit 1
grep -q 'provenance' "$TMP/err.txt"

printf '{"version":"0.2.2","commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","repository":"https://github.com/faridze/proxmox-os-installer","install_ref":"cursor/safe-provisioning-5dc3","installed_at":"2026-01-01T00:00:00Z"}\n' \
  > "$INSTALL/install-metadata.json"

MOCK_PY="$TMP/mock_update.py"
cat > "$MOCK_PY" <<'PY'
import sys
from pathlib import Path
import update_engine as ue
# minimal stub: --check prints and exits 0
if "update" in sys.argv and "--check" in sys.argv:
    print("Proxmox OS Installer")
    print("Update available.")
    sys.exit(0)
sys.exit(2)
PY
export ROOT
python3 - <<'PY' || exit 1
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["ROOT"]) / "lib"))
import update_engine as ue
paths = ue.default_paths()
meta = ue.load_metadata(paths)
ref = ue.pick_update_ref(meta, None)
assert ref == "cursor/safe-provisioning-5dc3"
try:
    ue.refuse_implicit_main_downgrade(meta, "main", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", explicit_ref=None)
except ue.UpdateError:
    pass
else:
    raise SystemExit("expected branch refusal for implicit main")
pinned = {
    "install_ref": "cccccccccccccccccccccccccccccccccccccccc",
    "commit_sha": "cccccccccccccccccccccccccccccccccccccccc",
}
try:
    ue.refuse_implicit_main_downgrade(pinned, "main", "dddddddddddddddddddddddddddddddddddddddd", explicit_ref=None)
except ue.UpdateError:
    pass
else:
    raise SystemExit("expected pinned commit refusal")
PY

echo 'OK: update CLI and channel guards'
