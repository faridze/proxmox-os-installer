#!/usr/bin/env python3
"""Interactive guest preparation for Debian/Ubuntu VMs (runs inside the VM, not on Proxmox)."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

VERSION = "0.1.0"

SUPPORTED_OS = frozenset(
    {
        ("debian", "12"),
        ("debian", "13"),
        ("ubuntu", "22.04"),
        ("ubuntu", "24.04"),
    }
)

NETWORK_BACKENDS = ("netplan", "ifupdown", "networkmanager", "systemd-networkd")


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


def _read_os_release() -> dict[str, str]:
    path = _path("etc/os-release")
    if not path.is_file():
        raise GuestSetupError("Cannot read /etc/os-release — not a supported Linux guest.")
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, val = line.split("=", 1)
        data[key.strip()] = val.strip().strip('"')
    return data


def parse_os_info() -> OsInfo:
    data = _read_os_release()
    ident = data.get("ID", "").lower()
    ver = data.get("VERSION_ID", "")
    if ident == "ubuntu" and ver.endswith(".04"):
        pass
    elif ident == "debian":
        ver = ver.split(".")[0] if ver else ver
    pretty = data.get("PRETTY_NAME", f"{ident} {ver}")
    variant = data.get("VARIANT_ID", data.get("ID_LIKE", ""))
    return OsInfo(id=ident, version_id=ver, pretty_name=pretty, variant=variant.lower())


def os_supported(info: OsInfo) -> bool:
    key = (info.id, info.version_id)
    if info.id == "debian" and info.version_id in ("12", "13"):
        return True
    if info.id == "ubuntu" and info.version_id in ("22.04", "24.04"):
        return True
    return key in SUPPORTED_OS


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    if os.environ.get("PVEOSI_GUEST_MOCK_CMD") == "1":
        return _mock_run(cmd)
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=False,
    )


def _mock_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    script = os.environ.get("PVEOSI_GUEST_MOCK_SCRIPT", "")
    if script:
        import json

        log = Path(script)
        entries = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
        entries.append(cmd)
        log.write_text(json.dumps(entries), encoding="utf-8")
    if cmd[:3] == ["systemctl", "is-active", "NetworkManager"]:
        active = os.environ.get("PVEOSI_GUEST_MOCK_NM_ACTIVE", "inactive")
        return subprocess.CompletedProcess(cmd, 0 if active == "active" else 3, active, "")
    if cmd[:2] == ["apt-get", "install"]:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if cmd[:2] == ["hostnamectl", "set-hostname"]:
        name = cmd[-1]
        _path("etc/hostname").write_text(name + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def detect_network_backend() -> str:
    if _path("etc/netplan").is_dir() and list(_path("etc/netplan").glob("*.yaml")):
        return "netplan"
    if _path("run/systemd/system/NetworkManager.service").exists() or _which("nmcli"):
        r = _run(["systemctl", "is-active", "NetworkManager"], check=False)
        if r.stdout.strip() == "active":
            return "networkmanager"
    if _path("etc/network/interfaces").is_file():
        return "ifupdown"
    if _path("etc/systemd/network").is_dir() and list(_path("etc/systemd/network").glob("*.network")):
        return "systemd-networkd"
    return "unknown"


def required_bins_for_backend(backend: str) -> list[str]:
    base = ["systemctl", "hostnamectl", "ss", "df", "lsblk"]
    if backend == "netplan":
        base.append("netplan")
    if backend in ("ifupdown", "netplan"):
        base.extend(["ip", "grep"])
    if backend == "networkmanager":
        base.append("nmcli")
    return base


def run_preflight() -> PreflightResult:
    if os.geteuid() != 0 and os.environ.get("PVEOSI_GUEST_ALLOW_NONROOT") != "1":
        raise GuestSetupError("Run as root inside the guest (sudo -i or root login).")
    if not _path("run/systemd/system").exists() and os.environ.get("PVEOSI_GUEST_ALLOW_NONROOT") != "1":
        raise GuestSetupError("Systemd is required; this utility targets Debian/Ubuntu cloud images with systemd.")
    info = parse_os_info()
    if not os_supported(info):
        raise GuestSetupError(
            f"Unsupported OS: {info.pretty_name}. "
            "Supported: Debian 12/13, Ubuntu Server 22.04/24.04 LTS."
        )
    backend = detect_network_backend()
    if backend == "unknown":
        raise GuestSetupError(
            "Could not identify a supported guest network stack "
            "(netplan, ifupdown, NetworkManager, or systemd-networkd). "
            "Configure networking manually before using this tool."
        )
    if backend == "networkmanager":
        raise GuestSetupError(
            "NetworkManager is active. This release does not automate NM profiles; "
            "use nmcli or convert to netplan/ifupdown before running automated network steps."
        )
    missing = [b for b in required_bins_for_backend(backend) if not _which(b)]
    if missing and os.environ.get("PVEOSI_GUEST_MOCK_CMD") != "1":
        raise GuestSetupError(f"Missing required commands: {', '.join(missing)}")
    return PreflightResult(os_info=info, network_backend=backend, missing_bins=missing)


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} (yes/no): ").strip().lower()
    return ans in ("yes", "y")


def set_hostname_interactive() -> None:
    current = _path("etc/hostname").read_text(encoding="utf-8").strip() if _path("etc/hostname").is_file() else ""
    print(f"Current hostname: {current or '(unset)'}")
    new = input("New hostname (blank = keep current): ").strip()
    if not new:
        print("Hostname unchanged.")
        return
    if not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9.-]{0,62}[a-zA-Z0-9])?", new):
        raise GuestSetupError("Invalid hostname.")
    if not _confirm(f"Set hostname to {new}"):
        print("Skipped.")
        return
    r = _run(["hostnamectl", "set-hostname", new])
    if r.returncode != 0:
        raise GuestSetupError("hostnamectl failed.")
    print(f"Hostname set to {new}.")


def _backup_file(path: Path) -> None:
    if path.is_file():
        bak = path.with_suffix(path.suffix + ".pveosi-bak")
        shutil.copy2(path, bak)
        print(f"Backup: {bak}")


def configure_network_interactive(backend: str) -> None:
    print(f"Network backend: {backend}")
    print("This tool does not replace complex multi-interface layouts.")
    mode = input("Configure IPv4 as [d]hcp or [s]tatic? (blank = skip): ").strip().lower()
    if not mode:
        print("Network unchanged.")
        return
    if mode.startswith("d"):
        print("DHCP is usually already configured by cloud-init; no change applied.")
        return
    if not mode.startswith("s"):
        raise GuestSetupError("Enter d, s, or leave blank.")
    addr = input("IPv4 address/CIDR (e.g. 192.0.2.10/24): ").strip()
    gw = input("Default gateway: ").strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", addr) or not re.match(r"^\d+\.\d+\.\d+\.\d+$", gw):
        raise GuestSetupError("Invalid IPv4 or gateway.")
    if not _confirm(f"Apply static IPv4 {addr} via {backend}"):
        print("Skipped.")
        return
    if backend == "netplan":
        _apply_netplan_static(addr, gw)
    elif backend == "ifupdown":
        _apply_ifupdown_static(addr, gw)
    elif backend == "systemd-networkd":
        _apply_networkd_static(addr, gw)
    else:
        raise GuestSetupError(f"Unsupported backend for automated static IP: {backend}")
    print("Network configuration written. Reboot or apply backend reload manually if needed.")


def _apply_netplan_static(addr: str, gw: str) -> None:
    np_dir = _path("etc/netplan")
    files = sorted(np_dir.glob("*.yaml"))
    target = files[0] if files else np_dir / "01-pveosi.yaml"
    _backup_file(target)
    ip = addr.split("/")[0]
    prefix = addr.split("/")[1]
    text = (
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        "    eth0:\n"
        "      dhcp4: false\n"
        f"      addresses: [{addr}]\n"
        "      routes:\n"
        f"        - to: default\n          via: {gw}\n"
        "      nameservers:\n"
        "        addresses: [8.8.8.8, 1.1.1.1]\n"
    )
    print("NOTE: interface name 'eth0' is assumed; edit netplan if your NIC differs.")
    target.write_text(text, encoding="utf-8")


def _apply_ifupdown_static(addr: str, gw: str) -> None:
    iface = _path("etc/network/interfaces")
    _backup_file(iface)
    ip = addr.split("/")[0]
    prefix = addr.split("/")[1]
    text = (
        "# Managed by pveosi-guest-setup — review before reboot\n"
        "auto lo\n"
        "iface lo inet loopback\n\n"
        "auto eth0\n"
        f"iface eth0 inet static\n"
        f"  address {ip}\n"
        f"  netmask {_cidr_to_netmask(int(prefix))}\n"
        f"  gateway {gw}\n"
        "  dns-nameservers 8.8.8.8 1.1.1.1\n"
    )
    print("NOTE: interface name 'eth0' is assumed; edit /etc/network/interfaces if needed.")
    iface.write_text(text, encoding="utf-8")


def _cidr_to_netmask(prefix: int) -> str:
    mask = (0xffffffff >> (32 - prefix)) << (32 - prefix)
    return ".".join(str((mask >> (8 * i)) & 0xff) for i in (3, 2, 1, 0))


def _apply_networkd_static(addr: str, gw: str) -> None:
    dropin = _path("etc/systemd/network/10-pveosi-static.network")
    _backup_file(dropin)
    text = (
        "[Match]\nName=eth0\n\n[Network]\n"
        f"Address={addr}\nGateway={gw}\nDNS=8.8.8.8\nDNS=1.1.1.1\n"
    )
    print("NOTE: interface name 'eth0' is assumed.")
    dropin.write_text(text, encoding="utf-8")


def disable_cloud_init_interactive() -> None:
    marker = _path("etc/cloud/cloud-init.disabled")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        print("cloud-init already disabled via /etc/cloud/cloud-init.disabled")
        return
    if not _confirm("Create /etc/cloud/cloud-init.disabled (recommended before host finalize)"):
        print("Skipped.")
        return
    marker.write_text("", encoding="utf-8")
    print("cloud-init disabled for future boots.")
    if _confirm("Also offer to purge /var/lib/cloud (irreversible; only if host Cloud-Init is finalized)"):
        lib = _path("var/lib/cloud")
        if lib.exists():
            shutil.rmtree(lib)
            print("Removed /var/lib/cloud")
        else:
            print("/var/lib/cloud not present.")


def install_guest_agent_interactive(info: OsInfo) -> None:
    if not _confirm("Install and enable qemu-guest-agent package"):
        print("Skipped.")
        return
    r = _run(["apt-get", "update", "-qq"])
    if r.returncode != 0:
        raise GuestSetupError("apt-get update failed.")
    r = _run(["apt-get", "install", "-y", "qemu-guest-agent"])
    if r.returncode != 0:
        raise GuestSetupError("qemu-guest-agent installation failed.")
    _run(["systemctl", "enable", "--now", "qemu-guest-agent"])
    print("qemu-guest-agent installed and started.")


def run_package_updates_interactive() -> None:
    if not _confirm("Run apt-get update and upgrade (may take several minutes)"):
        print("Skipped.")
        return
    r = _run(["apt-get", "update", "-qq"])
    if r.returncode != 0:
        raise GuestSetupError("apt-get update failed.")
    r = _run(["apt-get", "upgrade", "-y"])
    if r.returncode != 0:
        raise GuestSetupError("apt-get upgrade failed.")
    print("Package upgrade completed.")


def ensure_time_sync_interactive() -> None:
    if _path("lib/systemd/system/systemd-timesyncd.service").exists():
        _run(["systemctl", "enable", "--now", "systemd-timesyncd"])
        print("systemd-timesyncd enabled.")
        return
    if _which("chronyc"):
        _run(["systemctl", "enable", "--now", "chrony"])
        print("chrony enabled.")
        return
    print("No timesyncd/chrony detected; configure NTP manually.")


def ssh_diagnostics() -> None:
    print("--- SSH diagnostics (read-only) ---")
    for cmd in (
        ["systemctl", "is-active", "ssh"],
        ["systemctl", "is-active", "sshd"],
        ["ss", "-tlnp"],
    ):
        r = _run(cmd, check=False)
        label = " ".join(cmd)
        out = (r.stdout or r.stderr or "").strip()
        print(f"{label}: {out or '(no output)'}")
    sshd_cfg = _path("etc/ssh/sshd_config")
    if sshd_cfg.is_file():
        for line in sshd_cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or not s:
                continue
            if re.match(r"^(PermitRootLogin|PasswordAuthentication|PubkeyAuthentication)\b", s):
                print(f"  {s}")
    print("--- end SSH diagnostics ---")


def disk_diagnostics() -> None:
    print("--- Disk / filesystem diagnostics (read-only) ---")
    for cmd in (["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT"], ["df", "-hT"]):
        r = _run(cmd, check=False)
        print(r.stdout or r.stderr or "")
    print("--- end disk diagnostics ---")


def print_preflight_report(pf: PreflightResult) -> None:
    print("pveosi-guest-setup preflight")
    print(f"  OS:        {pf.os_info.pretty_name}")
    print(f"  ID:        {pf.os_info.id} {pf.os_info.version_id}")
    print(f"  Network:   {pf.network_backend}")
    if pf.missing_bins:
        print(f"  Missing:   {', '.join(pf.missing_bins)}")
    print()


def guided_setup(pf: PreflightResult) -> None:
    print("Guided guest preparation (you may skip any step).")
    print("Host-side pveosi finalize remains separate; shut down cleanly before host finalize.\n")
    steps: list[tuple[str, Callable[[], None]]] = [
        ("Hostname", lambda: set_hostname_interactive()),
        ("Network", lambda: configure_network_interactive(pf.network_backend)),
        ("Disable cloud-init", disable_cloud_init_interactive),
        ("QEMU guest agent", lambda: install_guest_agent_interactive(pf.os_info)),
        ("Package updates", run_package_updates_interactive),
        ("Time synchronization", ensure_time_sync_interactive),
        ("SSH diagnostics", ssh_diagnostics),
        ("Disk diagnostics", disk_diagnostics),
    ]
    for title, fn in steps:
        print(f"\n== {title} ==")
        if not _confirm(f"Run step: {title}"):
            print("Skipped.")
            continue
        try:
            fn()
        except GuestSetupError as exc:
            print(f"ERROR: {exc}")


def cmd_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pveosi-guest-setup",
        description="Prepare a Debian/Ubuntu cloud VM inside the guest (not on Proxmox).",
    )
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Run checks and exit.")
    parser.add_argument("--guided", action="store_true", help="Run interactive guided setup.")
    args = parser.parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    try:
        pf = run_preflight()
        print_preflight_report(pf)
        if args.preflight:
            return 0
        if args.guided:
            guided_setup(pf)
            return 0
        print("Interactive menu:")
        print("  1) Guided setup (recommended)")
        print("  2) Hostname only")
        print("  3) Network only")
        print("  4) Disable cloud-init")
        print("  5) Install QEMU guest agent")
        print("  6) Package updates")
        print("  7) Time sync")
        print("  8) SSH diagnostics")
        print("  9) Disk diagnostics")
        print("  0) Exit")
        choice = input("Select option: ").strip()
        actions: dict[str, Callable[[], None]] = {
            "1": lambda: guided_setup(pf),
            "2": set_hostname_interactive,
            "3": lambda: configure_network_interactive(pf.network_backend),
            "4": disable_cloud_init_interactive,
            "5": lambda: install_guest_agent_interactive(pf.os_info),
            "6": run_package_updates_interactive,
            "7": ensure_time_sync_interactive,
            "8": ssh_diagnostics,
            "9": disk_diagnostics,
        }
        if choice == "0":
            return 0
        action = actions.get(choice)
        if not action:
            print("Invalid option.")
            return 1
        action()
    except GuestSetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    sys.exit(cmd_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
