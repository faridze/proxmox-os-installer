#!/usr/bin/env bash
# Install standalone pveosi-guest-setup into a VM (run as root inside the guest).
set -Eeuo pipefail
(( EUID == 0 )) || { echo "Run as root." >&2; exit 1; }
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PREFIX="${PVEOSI_GUEST_PREFIX:-/usr/local}"
install -d -m 0755 "${PREFIX}/sbin" "${PREFIX}/lib/pveosi-guest"
install -m 0644 "${SRC}/lib/guest_setup_engine.py" "${PREFIX}/lib/pveosi-guest/guest_setup_engine.py"
install -m 0644 "${SRC}/lib/guest_netplan.py" "${PREFIX}/lib/pveosi-guest/guest_netplan.py"
install -m 0755 "${SRC}/bin/pveosi-guest-setup" "${PREFIX}/sbin/pveosi-guest-setup"
echo "Installed: ${PREFIX}/sbin/pveosi-guest-setup"
echo "Engine:    ${PREFIX}/lib/pveosi-guest/"
