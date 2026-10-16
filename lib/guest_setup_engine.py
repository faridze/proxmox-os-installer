#!/usr/bin/env python3
"""Guest preparation for Debian/Ubuntu VMs (runs inside the VM, not on Proxmox)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from guest_netplan import (  # noqa: E402
    DEST_NAME,
    NetplanSafetyError,
    migrate_netplan_independence,
    network_diagnostics_readonly,
)
from guest_netplan import build_netplan_plan  # noqa: E402

VERSION = "0.2.0"

SUPPORTED_OS = frozenset(
    {("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"), ("ubuntu", "24.04")}
)


class GuestSetupError(Exception):
    """User-facing guest setup failure."""


@dataclass(frozen=True)
class OsInfo:
    id: str
    version_id: str
    pretty_name: str
    variant: str


@dataclass(frozen=True)
class PreflightResult:
    os_info: OsInfo
    network_backend: str
    missing_bins: list[str]


def _root() -> Path:
    return Path(os.environ.get("PVEOSI_GUEST_ROOT", "/"))


def _path(rel: str) -> Path:
    return _root() / rel.lstrip("/")


def state_dir() -> Path:
    return _path("var/lib/pveosi-guest-setup")


def state_file() -> Path:
    return state_dir() / "state.json"


def _read_os_release() -> dict[str, str]:
    path = _path("etc/os-release")
    if not path.is_file():
        raise GuestSetupError("Cannot read /etc/os-release.")
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, val = line.split("=", 1)
        data[key.strip()] = val.strip().strip('"')
    return data


def parse_os_info() -> OsInfo:
    data = _read_os_release()
    ident = data.get("ID", "").lower()
    ver = data.get("VERSION_ID", "")
    if ident == "debian":
        ver = ver.split(".")[0] if ver else ver
    pretty = data.get("PRETTY_NAME", f"{ident} {ver}")
    return OsInfo(id=ident, version_id=ver, pretty_name=pretty, variant="")


def os_supported(info: OsInfo) -> bool:
    if info.id == "debian" and info.version_id in ("12", "13"):
        return True
    if info.id == "ubuntu" and info.version_id in ("22.04", "24.04"):
        return True
    return (info.id, info.version_id) in SUPPORTED_OS


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _run(cmd: list[str]) -> Any:
    import subprocess

    if os.environ.get("PVEOSI_GUEST_MOCK_CMD") == "1":
        return _mock_run(cmd)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _mock_run(cmd: list[str]) -> Any:
    import subprocess

    script = os.environ.get("PVEOSI_GUEST_MOCK_SCRIPT", "")
    if script:
        log = Path(script)
        entries = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
        entries.append(cmd)
        log.write_text(json.dumps(entries), encoding="utf-8")
    if cmd[:3] == ["systemctl", "is-active", "NetworkManager"]:
        active = os.environ.get("PVEOSI_GUEST_MOCK_NM_ACTIVE", "inactive")
        return subprocess.CompletedProcess(cmd, 0 if active == "active" else 3, active, "")
    if cmd[:2] == ["hostnamectl", "set-hostname"]:
        _path("etc/hostname").write_text(cmd[-1] + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if cmd[:2] == ["cloud-init", "status"]:
        st = os.environ.get("PVEOSI_GUEST_MOCK_CI_STATUS", "done")
        return subprocess.CompletedProcess(cmd, 0, st, "")
    if cmd[:3] == ["systemctl", "is-active", "ssh"]:
        return subprocess.CompletedProcess(cmd, 0, "active", "")
    if cmd[:3] == ["systemctl", "is-active", "sshd"]:
        return subprocess.CompletedProcess(cmd, 0, "inactive", "")
    if cmd[:2] == ["ip", "-4"]:
        return subprocess.CompletedProcess(cmd, 0, os.environ.get("PVEOSI_GUEST_MOCK_IP4", ""), "")
    if cmd[:2] == ["ip", "route"]:
        return subprocess.CompletedProcess(cmd, 0, os.environ.get("PVEOSI_GUEST_MOCK_ROUTE", ""), "")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def detect_network_backend() -> str:
    if _path("etc/netplan").is_dir() and list(_path("etc/netplan").glob("*.yaml")):
        return "netplan"
    if _which("nmcli"):
        r = _run(["systemctl", "is-active", "NetworkManager"])
        if r.stdout.strip() == "active":
            return "networkmanager"
    if _path("etc/network/interfaces").is_file():
        return "ifupdown"
    if _path("etc/systemd/network").is_dir() and list(_path("etc/systemd/network").glob("*.network")):
        return "systemd-networkd"
    return "unknown"


def required_bins_for_backend(backend: str) -> list[str]:
    base = ["systemctl", "hostnamectl"]
    if backend == "netplan":
        base.extend(["netplan", "ip"])
    elif backend in ("ifupdown", "systemd-networkd"):
        base.append("ip")
    return base


def run_preflight() -> PreflightResult:
    if os.geteuid() != 0 and os.environ.get("PVEOSI_GUEST_ALLOW_NONROOT") != "1":
        raise GuestSetupError("Run as root inside the guest.")
    if not _path("run/systemd/system").exists() and os.environ.get("PVEOSI_GUEST_ALLOW_NONROOT") != "1":
        raise GuestSetupError("Systemd is required.")
    info = parse_os_info()
    if not os_supported(info):
        raise GuestSetupError(f"Unsupported OS: {info.pretty_name}.")
    backend = detect_network_backend()
    if backend == "unknown":
        raise GuestSetupError("Unsupported or unknown network configuration.")
    if backend == "networkmanager":
        raise GuestSetupError("NetworkManager-active guests are not supported for automated preparation.")
    missing = [b for b in required_bins_for_backend(backend) if not _which(b)]
    if missing and os.environ.get("PVEOSI_GUEST_MOCK_CMD") != "1":
        raise GuestSetupError(f"Missing commands: {', '.join(missing)}")
    return PreflightResult(os_info=info, network_backend=backend, missing_bins=missing)


def cloud_init_first_boot_complete() -> bool:
    sem = _path("var/lib/cloud/instance/sem/config_scripts_user")
    if sem.exists():
        return True
    if _which("cloud-init"):
        r = _run(["cloud-init", "status"])
        out = (r.stdout or "") + (r.stderr or "")
        if "status: done" in out.lower() or out.strip().lower() == "done":
            return True
        if "not run" in out.lower() or "error" in out.lower():
            return False
    return os.environ.get("PVEOSI_GUEST_MOCK_CI_STATUS", "").lower() == "done"


def boot_id() -> str:
    p = _path("proc/sys/kernel/random/boot_id")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return os.environ.get("PVEOSI_GUEST_MOCK_BOOT_ID", "unknown")


def _read_state() -> dict[str, Any]:
    sf = state_file()
    if not sf.is_file():
        return {}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_state(data: dict[str, Any]) -> None:
    if os.environ.get("PVEOSI_GUEST_DRY_RUN") == "1":
        return
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    tmp = sd / f".state.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_file())


def _network_snapshot() -> dict[str, str]:
    ip4 = (_run(["ip", "-4", "addr", "show"]).stdout or "").strip()
    routes = (_run(["ip", "route", "show", "default"]).stdout or "").strip()
    host = (_run(["hostnamectl", "--static"]).stdout or "").strip()
    if not host and _path("etc/hostname").is_file():
        host = _path("etc/hostname").read_text(encoding="utf-8").strip()
    return {"ipv4": ip4, "default_route": routes, "hostname": host}


def disable_cloud_init_marker(dry_run: bool) -> None:
    marker = _path("etc/cloud/cloud-init.disabled")
    if marker.exists():
        return
    if dry_run:
        print(f"DRY RUN: would create {marker}")
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    print("Created /etc/cloud/cloud-init.disabled")


def cmd_prepare(dry_run: bool = False) -> int:
    pf = run_preflight()
    if not cloud_init_first_boot_complete():
        raise GuestSetupError(
            "Cloud-init first boot does not appear complete; wait for cloud-init status done."
        )
    if dry_run:
        os.environ["PVEOSI_GUEST_DRY_RUN"] = "1"
    net_result: dict[str, str] = {}
    if pf.network_backend == "netplan":
        net_result = migrate_netplan_independence(dry_run=dry_run)
        print(f"Netplan: {net_result.get('status')} ({net_result.get('source', '')} -> {DEST_NAME})")
    else:
        print(network_diagnostics_readonly(pf.network_backend))
        print(
            "Non-netplan backend: no automatic migration; existing configuration preserved as-is."
        )
        net_result = {"status": "preserved_readonly", "backend": pf.network_backend}

    disable_cloud_init_marker(dry_run)
    snap = _network_snapshot()
    checkpoint = {
        "phase": "prepared",
        "version": VERSION,
        "os": f"{pf.os_info.id}:{pf.os_info.version_id}",
        "network_backend": pf.network_backend,
        "prepare_boot_id": boot_id(),
        "prepare_epoch": int(time.time()),
        "netplan": net_result,
        "network_snapshot": snap,
        "verified": False,
        "external_ssh_attested": False,
    }
    _write_state(checkpoint)
    print("\nPREPARE complete. Reboot the guest manually before running: pveosi-guest-setup verify")
    print("Do not purge cloud-init packages or /var/lib/cloud in this phase.")
    return 0


def _snapshots_match(before: dict[str, str], after: dict[str, str]) -> bool:
    return before.get("ipv4") == after.get("ipv4") and before.get("default_route") == after.get("default_route")


def cmd_verify(interactive: bool = True) -> int:
    st = _read_state()
    if st.get("phase") != "prepared":
        raise GuestSetupError("No successful PREPARE checkpoint found; run prepare first.")
    if st.get("verified"):
        print("VERIFY already recorded for this checkpoint.")
        return 0
    current_boot = boot_id()
    if current_boot == st.get("prepare_boot_id"):
        raise GuestSetupError(
            "Boot ID unchanged since PREPARE; reboot the guest before verify."
        )
    snap_before = st.get("network_snapshot", {})
    snap_after = _network_snapshot()
    if not _snapshots_match(snap_before, snap_after):
        raise GuestSetupError(
            "IPv4 addresses or default route changed after reboot; investigate before continuing."
        )
    pf = run_preflight()
    if pf.network_backend == "netplan":
        dest = _path(f"etc/netplan/{DEST_NAME}")
        if not dest.is_file():
            raise GuestSetupError(f"Missing independent netplan file {DEST_NAME}.")
    r = _run(["systemctl", "is-active", "ssh"])
    ssh_ok = r.stdout.strip() in ("active", "activating") or _run(
        ["systemctl", "is-active", "sshd"]
    ).stdout.strip() in ("active", "activating")
    if not ssh_ok:
        raise GuestSetupError("SSH service is not active.")
    host_now = snap_after.get("hostname", "")
    if not host_now:
        raise GuestSetupError("Hostname could not be determined.")
    if interactive:
        print("Local checks passed. External SSH login must be confirmed by the operator.")
        ans = input(
            "Confirm you logged in via SSH from another host/session (yes/no): "
        ).strip().lower()
        if ans not in ("yes", "y"):
            raise GuestSetupError("External SSH attestation missing; verify blocked.")
    st["verified"] = True
    st["verify_boot_id"] = current_boot
    st["external_ssh_attested"] = True
    st["phase"] = "verified"
    st["verify_epoch"] = int(time.time())
    _write_state(st)
    print("VERIFY complete. Host-side pveosi finalize may proceed after guest shutdown.")
    return 0


def cmd_status() -> int:
    pf = run_preflight()
    st = _read_state()
    print("pveosi-guest-setup status")
    print(f"  Tool version:     {VERSION}")
    print(f"  OS:               {pf.os_info.pretty_name}")
    print(f"  Network backend:  {pf.network_backend}")
    print(f"  Cloud-init boot:  {'done' if cloud_init_first_boot_complete() else 'incomplete'}")
    print(f"  CI disabled file: {_path('etc/cloud/cloud-init.disabled').exists()}")
    print(f"  Checkpoint phase: {st.get('phase', 'none')}")
    print(f"  Verified:         {st.get('verified', False)}")
    if st.get("prepare_boot_id"):
        print(f"  Prepare boot_id:  {st.get('prepare_boot_id')}")
        print(f"  Current boot_id:  {boot_id()}")
    return 0


def print_preflight_report(pf: PreflightResult) -> None:
    print("pveosi-guest-setup preflight")
    print(f"  OS:        {pf.os_info.pretty_name}")
    print(f"  Network:   {pf.network_backend}")
    if pf.missing_bins:
        print(f"  Missing:   {', '.join(pf.missing_bins)}")
    print()


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} (yes/no): ").strip().lower() in ("yes", "y")


def set_hostname_interactive() -> None:
    current = _path("etc/hostname").read_text(encoding="utf-8").strip() if _path("etc/hostname").is_file() else ""
    print(f"Current hostname: {current or '(unset)'}")
    new = input("New hostname (blank = keep): ").strip()
    if not new:
        return
    if not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9.-]{0,62}[a-zA-Z0-9])?", new):
        raise GuestSetupError("Invalid hostname.")
    if not _confirm(f"Set hostname to {new}"):
        return
    if os.environ.get("PVEOSI_GUEST_DRY_RUN") == "1":
        print(f"DRY RUN: would set hostname {new}")
        return
    if _run(["hostnamectl", "set-hostname", new]).returncode != 0:
        raise GuestSetupError("hostnamectl failed.")


def ssh_diagnostics() -> None:
    print("--- SSH diagnostics (read-only) ---")
    for cmd in (["systemctl", "is-active", "ssh"], ["systemctl", "is-active", "sshd"]):
        r = _run(cmd)
        print(f"{' '.join(cmd)}: {(r.stdout or r.stderr).strip()}")
    print("--- end ---")


def disk_diagnostics() -> None:
    print("--- Disk diagnostics (read-only) ---")
    for cmd in (["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"], ["df", "-hT"]):
        r = _run(cmd)
        print(r.stdout or r.stderr or "")
    print("--- end ---")


def guided_setup(pf: PreflightResult) -> None:
    print("Guided workflow: PREPARE -> manual reboot -> VERIFY\n")
    if _confirm("Run PREPARE phase"):
        cmd_prepare(dry_run=False)
    if _confirm("Run VERIFY phase (only after reboot)"):
        cmd_verify(interactive=True)
    print("\nOptional read-only checks:")
    if _confirm("SSH diagnostics"):
        ssh_diagnostics()
    if _confirm("Disk diagnostics"):
        disk_diagnostics()
    if pf.network_backend != "netplan":
        print(network_diagnostics_readonly(pf.network_backend))


def cmd_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pveosi-guest-setup")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--guided", action="store_true")
    parser.add_argument("command", nargs="?", choices=["prepare", "verify"])
    args = parser.parse_args(argv)

    if args.version:
        print(VERSION)
        return 0
    try:
        if args.status:
            return cmd_status()
        pf = run_preflight()
        if args.preflight:
            print_preflight_report(pf)
            return 0
        if args.command == "prepare":
            return cmd_prepare(dry_run=args.dry_run)
        if args.command == "verify":
            if args.dry_run:
                print("DRY RUN: verify performs checks only; no state mutation.")
                st = _read_state()
                if st.get("phase") != "prepared":
                    raise GuestSetupError("No PREPARE checkpoint.")
                if boot_id() == st.get("prepare_boot_id"):
                    raise GuestSetupError("Reboot required before verify.")
                print("Checkpoint and boot ID look ready for interactive verify.")
                return 0
            return cmd_verify(interactive=True)
        if args.dry_run and not args.command:
            print_preflight_report(pf)
            return cmd_prepare(dry_run=True)
        if args.guided:
            print_preflight_report(pf)
            guided_setup(pf)
            return 0
        print_preflight_report(pf)
        print("Usage: pveosi-guest-setup prepare|verify [--dry-run] | --status | --preflight | --guided")
    except (GuestSetupError, NetplanSafetyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    sys.exit(cmd_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
