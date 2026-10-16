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
from typing import Any, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from guest_netplan import (  # noqa: E402
    DEST_NAME,
    NetplanSafetyError,
    independent_netplan_verified,
    migrate_netplan_independence,
    network_diagnostics_readonly,
    provenance_fingerprint,
)
from guest_network import (  # noqa: E402
    NetworkCompareError,
    capture_network_facts,
    check_dns_resolution,
    compare_network_facts,
    facts_from_checkpoint,
    facts_to_checkpoint,
    resolv_conf_usable,
)

VERSION = "0.3.1"

SUPPORTED_OS = frozenset(
    {("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"), ("ubuntu", "24.04")}
)

BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
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
    if cmd[:2] == ["hostnamectl", "--static"]:
        host = _path("etc/hostname").read_text(encoding="utf-8").strip() if _path("etc/hostname").is_file() else ""
        return subprocess.CompletedProcess(cmd, 0, host, "")
    if cmd[:2] == ["cloud-init", "status"]:
        st = os.environ.get("PVEOSI_GUEST_MOCK_CI_STATUS", "done")
        return subprocess.CompletedProcess(cmd, 0, st, "")
    if cmd[:3] == ["systemctl", "is-active", "ssh"]:
        st = os.environ.get("PVEOSI_GUEST_MOCK_SSH_ACTIVE", "active")
        return subprocess.CompletedProcess(cmd, 0, st, "")
    if cmd[:3] == ["systemctl", "is-active", "sshd"]:
        st = os.environ.get("PVEOSI_GUEST_MOCK_SSHD_ACTIVE", "inactive")
        return subprocess.CompletedProcess(cmd, 0, st, "")
    if cmd[:3] == ["ip", "-j", "addr"]:
        raw = os.environ.get(
            "PVEOSI_GUEST_MOCK_IPJ_ADDR",
            '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.2","prefixlen":24}],'
            '"flags":["UP"]}]',
        )
        return subprocess.CompletedProcess(cmd, 0, raw, "")
    if cmd[:3] == ["ip", "-j", "route"]:
        raw = os.environ.get(
            "PVEOSI_GUEST_MOCK_IPJ_ROUTE",
            '[{"dst":"default","gateway":"10.0.0.1","dev":"eth0"}]',
        )
        return subprocess.CompletedProcess(cmd, 0, raw, "")
    if cmd[:2] == ["sshd", "-t"] or cmd[:2] == ["/usr/sbin/sshd", "-t"]:
        if os.environ.get("PVEOSI_GUEST_MOCK_SSHD_T_FAIL") == "1":
            return subprocess.CompletedProcess(cmd, 1, "", "mock invalid")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def networkmanager_is_active() -> bool:
    r = _run(["systemctl", "is-active", "NetworkManager"])
    return (r.stdout or "").strip() == "active"


def detect_network_backend() -> str:
    if networkmanager_is_active():
        return "networkmanager"
    if _path("etc/netplan").is_dir() and list(_path("etc/netplan").glob("*.yaml")):
        return "netplan"
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


def _require_pyyaml_for_netplan(backend: str) -> None:
    if backend != "netplan":
        return
    try:
        import yaml  # noqa: F401
    except ImportError:
        raise GuestSetupError("python3-yaml is required before netplan migration.")


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
        raise GuestSetupError(
            "NetworkManager is active; automated netplan migration is not supported."
        )
    _require_pyyaml_for_netplan(backend)
    missing = [b for b in required_bins_for_backend(backend) if not _which(b)]
    if missing and os.environ.get("PVEOSI_GUEST_MOCK_CMD") != "1":
        raise GuestSetupError(f"Missing commands: {', '.join(missing)}")
    return PreflightResult(os_info=info, network_backend=backend, missing_bins=missing)


def cloud_init_first_boot_complete() -> bool:
    if _which("cloud-init"):
        r = _run(["cloud-init", "status"])
        out = (r.stdout or "") + (r.stderr or "")
        low = out.lower()
        if "error" in low or "status: error" in low:
            return False
        if "status: done" in low or out.strip().lower() == "done":
            return True
        if "not run" in low:
            return False
    sem = _path("var/lib/cloud/instance/sem/config_scripts_user")
    if sem.exists():
        if _which("cloud-init"):
            r = _run(["cloud-init", "status"])
            out = (r.stdout or "") + (r.stderr or "")
            if "error" in out.lower():
                return False
        return True
    return os.environ.get("PVEOSI_GUEST_MOCK_CI_STATUS", "").lower() == "done"


def boot_id() -> str:
    p = _path("proc/sys/kernel/random/boot_id")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return os.environ.get("PVEOSI_GUEST_MOCK_BOOT_ID", "")


def _valid_boot_id(value: str) -> bool:
    return bool(value) and bool(BOOT_ID_RE.match(value))


def _read_state() -> dict[str, Any]:
    sf = state_file()
    if not sf.is_file():
        return {}
    if sf.is_symlink():
        raise GuestSetupError("Checkpoint is a symlink; refusing.")
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise GuestSetupError("Corrupt checkpoint; cannot verify.")
    if not isinstance(data, dict):
        raise GuestSetupError("Corrupt checkpoint; cannot verify.")
    phase = data.get("phase")
    if phase and phase not in ("prepared", "verified"):
        raise GuestSetupError("Corrupt checkpoint phase.")
    pb = data.get("prepare_boot_id")
    if pb is not None and not _valid_boot_id(pb):
        raise GuestSetupError("Invalid prepare boot ID in checkpoint.")
    vb = data.get("verify_boot_id")
    if vb is not None and not _valid_boot_id(vb):
        raise GuestSetupError("Invalid verify boot ID in checkpoint.")
    return data


def _write_state(data: dict[str, Any]) -> None:
    if os.environ.get("PVEOSI_GUEST_DRY_RUN") == "1":
        return
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    tmp = sd / f".state.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_file())
    os.chmod(state_file(), 0o600)


def _network_snapshot() -> dict[str, Any]:
    facts = capture_network_facts()
    host = (_run(["hostnamectl", "--static"]).stdout or "").strip()
    if not host and _path("etc/hostname").is_file():
        host = _path("etc/hostname").read_text(encoding="utf-8").strip()
    snap = facts_to_checkpoint(facts)
    snap["hostname"] = host
    return snap


def _snapshot_canonical(snap: dict[str, Any]) -> str:
    return json.dumps(snap, sort_keys=True)


def _baseline_unchanged(prev: dict[str, Any], pf: PreflightResult) -> bool:
    stored = prev.get("network_snapshot") or {}
    current = _network_snapshot()
    if _snapshot_canonical(stored) != _snapshot_canonical(current):
        return False
    if pf.network_backend == "netplan":
        if prev.get("provenance_fingerprint") != provenance_fingerprint():
            return False
    return True


def _network_independence_established(backend: str, net_result: dict[str, str]) -> bool:
    if backend != "netplan":
        return False
    return net_result.get("status") in ("migrated", "already_migrated")


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


def _ssh_service_active() -> bool:
    for unit in ("ssh", "sshd"):
        r = _run(["systemctl", "is-active", unit])
        if (r.stdout or "").strip() == "active":
            return True
    return False


def _ssh_config_valid() -> bool:
    if os.environ.get("PVEOSI_GUEST_MOCK_CMD") == "1":
        return os.environ.get("PVEOSI_GUEST_MOCK_SSHD_T_FAIL") != "1"
    for cmd in (["sshd", "-t"], ["/usr/sbin/sshd", "-t"]):
        if cmd[0].startswith("/") or shutil.which(cmd[0]):
            r = _run(cmd)
            if r.returncode == 0:
                return True
            if r.returncode != 0 and shutil.which(cmd[0]):
                return False
    return True


def cmd_prepare(dry_run: bool = False) -> int:
    pf = run_preflight()
    if not cloud_init_first_boot_complete():
        raise GuestSetupError(
            "Cloud-init first boot does not appear complete; wait for cloud-init status done."
        )
    prev = _read_state()
    current_boot = boot_id()
    if not _valid_boot_id(current_boot):
        raise GuestSetupError("Cannot read a valid kernel boot ID.")
    if prev.get("phase") == "prepared" and not prev.get("machine_verified"):
        if current_boot != prev.get("prepare_boot_id"):
            raise GuestSetupError(
                "Guest rebooted after PREPARE; run verify instead of prepare."
            )
    if dry_run:
        os.environ["PVEOSI_GUEST_DRY_RUN"] = "1"
    net_result: dict[str, str] = {}
    if pf.network_backend == "netplan":
        net_result = migrate_netplan_independence(dry_run=dry_run)
        print(
            f"Netplan: {net_result.get('status')} "
            f"({net_result.get('source', '')} -> {DEST_NAME})".rstrip()
        )
    else:
        print(network_diagnostics_readonly(pf.network_backend))
        print(
            "Non-netplan backend: manual-only workflow; cloud-init.disabled is not created "
            "until network independence is machine-verified (not available on this backend)."
        )
        net_result = {"status": "preserved_readonly", "backend": pf.network_backend}

    if _network_independence_established(pf.network_backend, net_result):
        disable_cloud_init_marker(dry_run)

    had_checkpoint = bool(prev.get("phase")) and prev.get("network_snapshot")
    if had_checkpoint:
        if prev.get("phase") == "prepared" and current_boot == prev.get("prepare_boot_id"):
            checkpoint = dict(prev)
            checkpoint["version"] = VERSION
            checkpoint["netplan"] = net_result
            if not dry_run:
                _write_state(checkpoint)
            print("\nPREPARE idempotent (baseline unchanged). Reboot before verify if not done yet.")
            return 0
        if prev.get("machine_verified") or prev.get("phase") in ("verified", "manual_acknowledged"):
            if not _baseline_unchanged(prev, pf):
                raise GuestSetupError(
                    "Material change since verified checkpoint; start a new preparation cycle."
                )
            checkpoint = dict(prev)
            checkpoint["version"] = VERSION
            checkpoint["netplan"] = net_result
            if not dry_run:
                _write_state(checkpoint)
            print("\nPREPARE idempotent; verified readiness unchanged.")
            return 0

    snap = _network_snapshot()
    checkpoint = {
        "phase": "prepared",
        "version": VERSION,
        "os": f"{pf.os_info.id}:{pf.os_info.version_id}",
        "network_backend": pf.network_backend,
        "prepare_boot_id": current_boot,
        "prepare_epoch": int(time.time()),
        "netplan": net_result,
        "network_snapshot": snap,
        "provenance_fingerprint": provenance_fingerprint()
        if pf.network_backend == "netplan"
        else None,
        "verified": False,
        "machine_verified": False,
        "external_ssh_attested": False,
        "manual_only": pf.network_backend in ("ifupdown", "systemd-networkd"),
    }
    if not dry_run:
        _write_state(checkpoint)
    print("\nPREPARE complete. Reboot the guest manually before running: pveosi-guest-setup verify")
    print("Do not purge cloud-init packages or /var/lib/cloud in this phase.")
    return 0


def cmd_verify(interactive: bool = True, manual_only: bool = False) -> int:
    if not interactive:
        raise GuestSetupError("External SSH attestation requires interactive verify.")
    st = _read_state()
    if st.get("phase") not in ("prepared", "verified"):
        raise GuestSetupError("No successful PREPARE checkpoint found; run prepare first.")
    if st.get("machine_verified") and st.get("phase") == "verified":
        if not _baseline_unchanged(st, run_preflight()):
            raise GuestSetupError(
                "Verified checkpoint no longer matches current network; re-prepare required."
            )
        print("VERIFY already recorded for this checkpoint.")
        return 0
    if st.get("phase") == "manual_acknowledged" and st.get("manual_only"):
        print("Manual-only acknowledgment already recorded.")
        return 0
    current_boot = boot_id()
    if not _valid_boot_id(current_boot):
        raise GuestSetupError("Cannot read a valid kernel boot ID.")
    if current_boot == st.get("prepare_boot_id"):
        raise GuestSetupError("Boot ID unchanged since PREPARE; reboot the guest before verify.")
    snap_before = st.get("network_snapshot", {})
    snap_after = _network_snapshot()
    try:
        before_facts = facts_from_checkpoint(snap_before)
        after_facts = capture_network_facts()
        cmp_result = compare_network_facts(before_facts, after_facts)
    except (KeyError, TypeError, NetworkCompareError) as exc:
        raise GuestSetupError(f"Network comparison failed: {exc}") from exc
    if not cmp_result.ok:
        raise GuestSetupError("; ".join(cmp_result.messages) or "Network changed after reboot.")
    if cmp_result.needs_operator_confirm:
        print("DHCP-related address change detected; confirm this is expected.")
        ans = input("Continue verify despite DHCP address change (yes/no): ").strip().lower()
        if ans not in ("yes", "y"):
            raise GuestSetupError("Operator declined DHCP address change.")
    expected_host = snap_before.get("hostname", "")
    actual_host = snap_after.get("hostname", "")
    if expected_host and actual_host != expected_host:
        raise GuestSetupError("Hostname changed since PREPARE.")
    if not resolv_conf_usable():
        raise GuestSetupError("Resolver configuration appears missing or unusable.")
    try:
        check_dns_resolution()
    except NetworkCompareError as exc:
        raise GuestSetupError(str(exc)) from exc
    pf = run_preflight()
    if pf.network_backend == "netplan":
        if not independent_netplan_verified():
            raise GuestSetupError(
                f"Independent netplan migration not proven (missing {DEST_NAME} provenance)."
            )
    elif pf.network_backend in ("ifupdown", "systemd-networkd"):
        if not manual_only:
            raise GuestSetupError(
                "Non-netplan backend: machine-verified readiness unavailable. "
                "Use: pveosi-guest-setup verify --manual-only"
            )
        print(
            "WARNING: Manual-only workflow — this does NOT authorize cloud-init package removal."
        )
    if not _ssh_service_active():
        raise GuestSetupError("SSH service is not active.")
    if not _ssh_config_valid():
        raise GuestSetupError("SSH daemon configuration check failed (sshd -t).")
    print("Local checks passed. External SSH login must be confirmed by the operator.")
    print("(External connectivity is not machine-verified from inside the guest.)")
    ans = input(
        "Confirm you logged in via SSH from another host/session (yes/no): "
    ).strip().lower()
    if ans not in ("yes", "y"):
        raise GuestSetupError("External SSH attestation missing; verify blocked.")
    machine_ok = (
        pf.network_backend == "netplan"
        and independent_netplan_verified()
        and not manual_only
    )
    st["verified"] = machine_ok or manual_only
    st["machine_verified"] = machine_ok
    st["manual_only"] = manual_only or pf.network_backend in ("ifupdown", "systemd-networkd")
    st["verify_boot_id"] = current_boot
    st["external_ssh_attested"] = True
    st["phase"] = "manual_acknowledged" if manual_only else "verified"
    st["verify_epoch"] = int(time.time())
    _write_state(st)
    if machine_ok:
        print("VERIFY complete. Host-side pveosi finalize may proceed after guest shutdown.")
    else:
        print(
            "Manual-only acknowledgment recorded. Do not treat this as machine-verified "
            "readiness for cloud-init package removal."
        )
    return 0


def cmd_status() -> int:
    pf = run_preflight()
    st = _read_state()
    print("pveosi-guest-setup status")
    print(f"  Tool version:     {VERSION}")
    print(f"  OS:               {pf.os_info.pretty_name}")
    print(f"  Network backend:  {pf.network_backend}")
    if pf.network_backend == "netplan":
        print(f"  Netplan proven:   {independent_netplan_verified()}")
    print(f"  Cloud-init boot:  {'done' if cloud_init_first_boot_complete() else 'incomplete'}")
    print(f"  CI disabled file: {_path('etc/cloud/cloud-init.disabled').exists()}")
    print(f"  Checkpoint phase: {st.get('phase', 'none')}")
    print(f"  Verified:         {st.get('verified', False)}")
    print(f"  Machine-verified: {st.get('machine_verified', False)}")
    if st.get("manual_only"):
        print("  Manual-only:      yes (not authorized for CI package removal)")
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
    current = (
        _path("etc/hostname").read_text(encoding="utf-8").strip()
        if _path("etc/hostname").is_file()
        else ""
    )
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
    parser.add_argument(
        "--manual-only",
        action="store_true",
        help="Non-netplan manual acknowledgment (not machine-verified readiness)",
    )
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
                if st.get("phase") not in ("prepared", "verified"):
                    raise GuestSetupError("No PREPARE checkpoint.")
                if boot_id() == st.get("prepare_boot_id"):
                    raise GuestSetupError("Reboot required before verify.")
                print("Checkpoint and boot ID look ready for interactive verify.")
                return 0
            return cmd_verify(interactive=True, manual_only=args.manual_only)
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
