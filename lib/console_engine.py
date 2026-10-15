#!/usr/bin/env python3
"""Proxmox VM console (VGA vs serial) configuration for pveosi-managed VMs."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
from finalize_engine import (  # noqa: E402
    FinalizeError,
    PVEOSI_TAG,
    _assert_unchanged,
    _parse_config_text,
    _run,
    qm_config,
    qm_status,
)

CONSOLE_DOC_GRAPHICAL = (
    "Many Debian/Ubuntu cloud images send boot and login output to the serial console only. "
    "Standard VGA may appear blank until the guest is configured for framebuffer or getty on tty1. "
    "This tool does not verify graphical login. SSH access does not prove the noVNC console works."
)


@dataclass
class ConsoleState:
    vmid: int
    name: str
    status: str
    vga: str
    serial0: Optional[str]
    pveosi_owned: bool
    config: dict[str, str]


@dataclass
class ConsolePlan:
    vmid: int
    display_label: str
    serial_label: str
    operations: list[str]
    warnings: list[str]
    remove_serial: bool


def _pveosi_owned(config: dict[str, str]) -> bool:
    tags = config.get("tags", "")
    return PVEOSI_TAG in [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]


def describe_display(vga: str) -> str:
    v = (vga or "default").lower()
    if v in ("serial0", "serial"):
        return "serial"
    if v == "std":
        return "standard"
    return "other"


def load_console_state(vmid: int) -> ConsoleState:
    if vmid < 100:
        raise FinalizeError("Invalid VM ID.")
    config = qm_config(vmid)
    if not config:
        raise FinalizeError(f"VM {vmid} does not exist.")
    if config.get("template") == "1":
        raise FinalizeError("Templates cannot be reconfigured with this tool.")
    status = qm_status(vmid)
    return ConsoleState(
        vmid=vmid,
        name=config.get("name", f"vm-{vmid}"),
        status=status,
        vga=config.get("vga", "std"),
        serial0=config.get("serial0"),
        pveosi_owned=_pveosi_owned(config),
        config=config,
    )


def validate_console_preconditions(state: ConsoleState) -> None:
    if not state.pveosi_owned:
        raise FinalizeError(
            "Cannot establish pveosi ownership (missing tag 'pveosi'). Refusing to modify an unmanaged VM."
        )
    if state.status != "stopped":
        raise FinalizeError(f"VM must be stopped (current status: {state.status}).")
    lock = state.config.get("lock", "")
    if lock:
        raise FinalizeError(f"VM is locked ({lock}); retry when the lock is cleared.")


def build_console_plan(state: ConsoleState, remove_serial: bool) -> ConsolePlan:
    ops: list[str] = []
    warnings: list[str] = []
    disp = describe_display(state.vga)
    serial_present = "present" if state.serial0 else "absent"

    if remove_serial:
        if disp != "standard":
            raise FinalizeError(
                "Serial removal requires Standard VGA display; convert to VGA first and verify the graphical console."
            )
        if not state.serial0:
            return ConsolePlan(
                vmid=state.vmid,
                display_label=disp,
                serial_label=serial_present,
                operations=[],
                warnings=["Serial port already absent."],
                remove_serial=False,
            )
        ops.append(f"qm set {state.vmid} --delete serial0")
        return ConsolePlan(
            vmid=state.vmid,
            display_label=disp,
            serial_label="present",
            operations=ops,
            warnings=[
                "Only remove serial0 after you have verified the Proxmox graphical (noVNC) console, "
                "not SSH alone."
            ],
            remove_serial=True,
        )

    if disp == "standard":
        warnings.append("Display is already Standard VGA.")
        return ConsolePlan(
            vmid=state.vmid,
            display_label=disp,
            serial_label=serial_present,
            operations=[],
            warnings=warnings,
            remove_serial=False,
        )

    ops.append(f"qm set {state.vmid} --vga std")
    warnings.append(CONSOLE_DOC_GRAPHICAL)
    warnings.append("Serial Port 0 will remain attached for troubleshooting unless removed in a later step.")
    return ConsolePlan(
        vmid=state.vmid,
        display_label=disp,
        serial_label=serial_present,
        operations=ops,
        warnings=warnings,
        remove_serial=False,
    )


def execute_console_plan(state: ConsoleState, plan: ConsolePlan) -> None:
    if not plan.operations:
        return
    baseline = dict(state.config)
    preserve_keys = tuple(
        k
        for k in baseline
        if k not in ("vga", "serial0", "serial1", "serial2")
    )
    for op in plan.operations:
        parts = op.split()
        if len(parts) < 4 or parts[0] != "qm" or parts[1] != "set":
            raise FinalizeError(f"Unsupported planned operation: {op}")
        if "--delete" in parts:
            key = parts[parts.index("--delete") + 1]
            r = _run(["qm", "set", str(state.vmid), "--delete", key])
        elif "--vga" in parts:
            r = _run(["qm", "set", str(state.vmid), "--vga", "std"])
        else:
            raise FinalizeError(f"Unsupported planned operation: {op}")
        if r.returncode != 0:
            raise FinalizeError(f"Console configuration failed: {op}")
        after = qm_config(state.vmid)
        _assert_unchanged(baseline, after, preserve_keys, "VM hardware")


def print_console_preview(state: ConsoleState, plan: ConsolePlan, dry_run: bool) -> None:
    print("Console configuration preview")
    print(f"  VMID:         {state.vmid}")
    print(f"  Name:         {state.name}")
    print(f"  Power state:  {state.status}")
    print(f"  Display:      {state.vga} ({plan.display_label})")
    print(f"  Serial Port:  {state.serial0 or 'not set'} ({plan.serial_label})")
    for w in plan.warnings:
        print(f"  Note:         {w}")
    print()
    if not plan.operations:
        print("No host changes required.")
        return
    if dry_run:
        print("DRY RUN: no changes will be made.")
    print("Planned operations:")
    for op in plan.operations:
        print(f"  - {op}")
    print()


def cmd_console(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pveosi console", description="Adjust Proxmox VM console display settings.")
    parser.add_argument("vmid", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--remove-serial",
        action="store_true",
        help="Remove serial0 only after Standard VGA is configured and graphical console was verified.",
    )
    args = parser.parse_args(argv)

    if os.geteuid() != 0 and os.environ.get("PVEOSI_CONSOLE_ALLOW_NONROOT") != "1":
        print("ERROR: run as root.", file=sys.stderr)
        return 1

    try:
        state = load_console_state(args.vmid)
        validate_console_preconditions(state)
        plan = build_console_plan(state, remove_serial=args.remove_serial)
        print_console_preview(state, plan, dry_run=args.dry_run)
        if args.dry_run or not plan.operations:
            return 0
        if args.remove_serial:
            att = input(
                "Confirm you verified the Proxmox graphical (noVNC) console works on this VM (yes/no): "
            ).strip().lower()
        else:
            att = input("Proceed with console configuration changes? (yes/no): ").strip().lower()
        if att not in ("yes", "y"):
            print("Cancelled.")
            return 0
        execute_console_plan(state, plan)
        print("Console configuration updated.")
        if not args.remove_serial and plan.operations:
            print(
                f"After booting the guest, verify the graphical console in Proxmox, then optionally run:\n"
                f"  pveosi console {args.vmid} --remove-serial"
            )
    except FinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "console":
        print("Usage: console_engine.py console <VMID> [--dry-run] [--remove-serial]", file=sys.stderr)
        sys.exit(2)
    sys.exit(cmd_console(sys.argv[2:]))


if __name__ == "__main__":
    main()
