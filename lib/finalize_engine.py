#!/usr/bin/env python3
"""Optional post-install Cloud-Init finalization for pveosi-managed VMs (Proxmox VE 8+)."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

PVEOSI_TAG = "pveosi"
SNIPPET_RE = re.compile(r"^user=(?:[^:]+:)?snippets/(pveosi-(\d+)-user\.yaml)$")
CLOUDINIT_IDE_RE = re.compile(r"^(ide\d+):\s*([^,\s]+:cloudinit)\s*$")
CI_KEYS = (
    "cicustom",
    "ciuser",
    "cipassword",
    "sshkeys",
    "ipconfig0",
    "ipconfig1",
    "nameserver",
    "searchdomain",
)
SENSITIVE_RE = re.compile(r"(cipassword|password|sshkeys)", re.I)


class FinalizeError(Exception):
    """User-facing finalization failure."""


@dataclass
class VmContext:
    vmid: int
    config: dict[str, str]
    status: str
    snapshots: list[str]
    pveosi_owned: bool
    cloudinit_ide: Optional[str] = None
    cloudinit_spec: Optional[str] = None
    cicustom_user: Optional[str] = None
    snippet_name: Optional[str] = None
    snippet_vmid: Optional[int] = None


@dataclass
class FinalizePlan:
    vmid: int
    name: str
    power_state: str
    cloudinit_ide: Optional[str]
    config_keys_to_remove: list[str]
    snippet_ref: Optional[str]
    snippet_safe_to_remove: bool
    snippet_blockers: list[str]
    snapshot_blockers: list[str]
    guest_readiness: str
    operations: list[str] = field(default_factory=list)
    already_finalized: bool = False


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def qm_config(vmid: int) -> dict[str, str]:
    if "PVEOSI_MOCK_QM_CONFIG" in os.environ:
        mock = os.environ["PVEOSI_MOCK_QM_CONFIG"]
        text = Path(mock).read_text(encoding="utf-8")
    else:
        r = _run(["qm", "config", str(vmid)])
        if r.returncode != 0:
            raise FinalizeError(f"Cannot read VM {vmid} configuration.")
        text = r.stdout
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip()
    return out


def qm_status(vmid: int) -> str:
    mock = os.environ.get("PVEOSI_MOCK_QM_STATUS")
    if mock:
        return mock.strip()
    r = _run(["qm", "status", str(vmid)])
    if r.returncode != 0:
        raise FinalizeError(f"Cannot read VM {vmid} status.")
    for line in r.stdout.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def qm_snapshots(vmid: int) -> list[str]:
    mock = os.environ.get("PVEOSI_MOCK_QM_SNAPSHOTS")
    if mock:
        return [s for s in mock.split(",") if s]
    r = _run(["qm", "listsnapshot", str(vmid)])
    if r.returncode != 0:
        return []
    snaps: list[str] = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] != "Name":
            snaps.append(parts[0])
    return snaps


def list_vmids() -> list[int]:
    mock = os.environ.get("PVEOSI_MOCK_ALL_VMIDS")
    if mock:
        return [int(x) for x in mock.split(",") if x.strip().isdigit()]
    r = _run(["qm", "list"])
    if r.returncode != 0:
        return []
    ids: list[int] = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            ids.append(int(parts[0]))
    return ids


def _parse_cloudinit(config: dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    for key, val in config.items():
        m = CLOUDINIT_IDE_RE.match(f"{key}: {val}")
        if m:
            return m.group(1), m.group(2)
    return None, None


def _parse_cicustom(config: dict[str, str]) -> Optional[str]:
    raw = config.get("cicustom", "")
    for part in raw.split(","):
        part = part.strip()
        if part.startswith("user="):
            return part.split("=", 1)[1]
    return None


def _pveosi_owned(config: dict[str, str], vmid: int, cicustom_user: Optional[str]) -> bool:
    tags = config.get("tags", "")
    if PVEOSI_TAG in [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]:
        return True
    if cicustom_user and re.fullmatch(r"pveosi-\d+-user\.yaml", Path(cicustom_user).name):
        m = re.search(r"pveosi-(\d+)-user\.yaml", cicustom_user)
        if m and int(m.group(1)) == vmid:
            return True
    return False


def load_context(vmid: int) -> VmContext:
    if vmid < 100:
        raise FinalizeError("Invalid VM ID.")
    config = qm_config(vmid)
    if not config:
        raise FinalizeError(f"VM {vmid} does not exist.")
    status = qm_status(vmid)
    snaps = qm_snapshots(vmid)
    ide, spec = _parse_cloudinit(config)
    cic = _parse_cicustom(config)
    sn_vmid: Optional[int] = None
    sn_name: Optional[str] = None
    if cic:
        sn_name = Path(cic).name
        m = re.fullmatch(r"pveosi-(\d+)-user\.yaml", sn_name)
        if m:
            sn_vmid = int(m.group(1))
    owned = _pveosi_owned(config, vmid, cic)
    return VmContext(
        vmid=vmid,
        config=config,
        status=status,
        snapshots=snaps,
        pveosi_owned=owned,
        cloudinit_ide=ide,
        cloudinit_spec=spec,
        cicustom_user=cic,
        snippet_name=sn_name,
        snippet_vmid=sn_vmid,
    )


def _snippet_references_elsewhere(snippet: str, vmid: int) -> list[str]:
    refs: list[str] = []
    for other in list_vmids():
        if other == vmid:
            continue
        cfg = qm_config(other)
        cic = _parse_cicustom(cfg)
        if cic and Path(cic).name == Path(snippet).name:
            refs.append(str(other))
    return refs


def build_plan(ctx: VmContext, guest_attested: bool) -> FinalizePlan:
    name = ctx.config.get("name", f"vm-{ctx.vmid}")
    keys = [k for k in CI_KEYS if k in ctx.config]
    if ctx.cloudinit_ide:
        keys.append(ctx.cloudinit_ide)

    blockers: list[str] = []
    if ctx.snapshots:
        blockers.append(f"snapshots present: {', '.join(ctx.snapshots)}")

    sn_blockers: list[str] = []
    snippet_safe = False
    snippet_ref = ctx.cicustom_user
    if snippet_ref:
        if not re.fullmatch(r"pveosi-\d+-user\.yaml", Path(snippet_ref).name):
            sn_blockers.append("cicustom snippet is not a pveosi-generated filename")
        elif ctx.snippet_vmid != ctx.vmid:
            sn_blockers.append("snippet VMID does not match this VM")
        else:
            others = _snippet_references_elsewhere(snippet_ref, ctx.vmid)
            if others:
                sn_blockers.append(f"snippet referenced by other VMs: {', '.join(others)}")
            elif not blockers:
                snippet_safe = True

    already = not ctx.cloudinit_ide and not any(k in ctx.config for k in CI_KEYS if k != "cicustom")
    if already and not ctx.cicustom_user:
        already = True
    elif not ctx.cloudinit_ide and not keys:
        already = True
    else:
        already = False

    guest_state = "operator-attested" if guest_attested else "unknown"
    ops: list[str] = []
    if not already:
        for k in keys:
            if k.startswith("ide"):
                ops.append(f"qm set {ctx.vmid} --delete {k}")
            else:
                ops.append(f"qm set {ctx.vmid} --delete {k}")
        if snippet_safe and snippet_ref:
            ops.append(f"remove snippet file {snippet_ref} (only after explicit approval)")

    return FinalizePlan(
        vmid=ctx.vmid,
        name=name,
        power_state=ctx.status,
        cloudinit_ide=ctx.cloudinit_ide,
        config_keys_to_remove=keys,
        snippet_ref=snippet_ref,
        snippet_safe_to_remove=snippet_safe,
        snippet_blockers=sn_blockers,
        snapshot_blockers=blockers,
        guest_readiness=guest_state,
        operations=ops,
        already_finalized=already and not ctx.cicustom_user,
    )


def validate_preconditions(ctx: VmContext) -> None:
    if ctx.config.get("template") == "1":
        raise FinalizeError("Templates cannot be finalized with this tool.")
    if ctx.status != "stopped":
        raise FinalizeError(f"VM must be stopped (current status: {ctx.status}).")
    lock = ctx.config.get("lock", "")
    if lock:
        raise FinalizeError(f"VM is locked ({lock}); retry when the lock is cleared.")
    if not ctx.pveosi_owned:
        raise FinalizeError(
            "Cannot establish pveosi ownership (missing tag 'pveosi' or matching pveosi snippet). "
            "Refusing to modify an unmanaged VM."
        )
    if ctx.cloudinit_ide is None and not any(k in ctx.config for k in CI_KEYS):
        return
    for key, val in ctx.config.items():
        if key.startswith("ide") and "cloudinit" not in val and key == ctx.cloudinit_ide:
            raise FinalizeError(f"Cloud-Init device {key} is not a verified cloudinit drive.")
    # Reject ordinary CD-ROM on ide2 without cloudinit marker.
    if ctx.config.get("ide2") and "cloudinit" not in ctx.config.get("ide2", "") and "ide2" not in (
        ctx.cloudinit_ide or ""
    ):
        if "media=cdrom" in ctx.config.get("ide2", "") and not ctx.cloudinit_ide:
            raise FinalizeError(
                "ide2 contains an ordinary CD-ROM, not a verified Cloud-Init drive; refusing to delete it."
            )


def print_guest_checklist() -> None:
    print("Guest preparation (operator-attested; not verified from the Proxmox host):")
    print("  1. Complete first boot successfully.")
    print("  2. Run: cloud-init status --wait (must report done).")
    print("  3. Confirm SSH and console access.")
    print("  4. Change the initial login password if desired.")
    print("  5. Verify IP, gateway, DNS, and hostname inside the guest.")
    print("  6. Create /etc/cloud/cloud-init.disabled inside the guest.")
    print("  7. Reboot the guest and verify network and SSH again.")
    print("  8. Shut down the guest cleanly.")
    print()
    print("Do not run cloud-init clean or remove /var/lib/cloud from the host.")
    print("QEMU Guest Agent enablement in VM config does not prove the agent works.")
    print()


def print_preview(plan: FinalizePlan, dry_run: bool) -> None:
    print("Cloud-Init finalization preview")
    print(f"  VMID:        {plan.vmid}")
    print(f"  Name:        {plan.name}")
    print(f"  Power state: {plan.power_state}")
    print(f"  Cloud-Init:  {plan.cloudinit_ide or 'none detected'}")
    print(f"  Remove keys: {', '.join(plan.config_keys_to_remove) or 'none'}")
    print(f"  Snippet:     {plan.snippet_ref or 'none'}")
    print(f"  Snippet OK:  {'yes' if plan.snippet_safe_to_remove else 'no'}")
    if plan.snippet_blockers:
        print(f"  Snippet note: {'; '.join(plan.snippet_blockers)}")
    if plan.snapshot_blockers:
        print(f"  Blockers:    {'; '.join(plan.snapshot_blockers)}")
    print(f"  Guest ready: {plan.guest_readiness}")
    print()
    if plan.already_finalized:
        print("VM appears already finalized (no active Cloud-Init attachments detected).")
        return
    if dry_run:
        print("DRY RUN: no changes will be made.")
    print("Planned operations:")
    for op in plan.operations:
        if "cipassword" in op.lower() or "password" in op.lower():
            continue
        print(f"  - {op}")
    print()


def _sanitize_config_for_display(config: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for k, v in config.items():
        if SENSITIVE_RE.search(k):
            safe[k] = "[REDACTED]"
        else:
            safe[k] = v
    return safe


def execute_plan(ctx: VmContext, plan: FinalizePlan, remove_snippet: bool) -> None:
    if plan.snapshot_blockers:
        raise FinalizeError(f"Cannot finalize: {'; '.join(plan.snapshot_blockers)}")
    if plan.already_finalized:
        print("Already finalized; no host changes applied.")
        return

    # Revalidate immediately before mutation.
    fresh = load_context(ctx.vmid)
    validate_preconditions(fresh)
    if fresh.status != "stopped":
        raise FinalizeError("VM is no longer stopped; aborting.")

    for key in plan.config_keys_to_remove:
        if key.startswith("ide"):
            r = _run(["qm", "set", str(ctx.vmid), "--delete", key])
        else:
            r = _run(["qm", "set", str(ctx.vmid), "--delete", key])
        if r.returncode != 0:
            raise FinalizeError(
                f"Failed while removing {key}. Inspect 'qm config {ctx.vmid}' before retrying."
            )

    verify = load_context(ctx.vmid)
    if verify.cloudinit_ide:
        raise FinalizeError(
            "Cloud-Init drive still attached after finalization steps; manual review required."
        )

    if remove_snippet and plan.snippet_safe_to_remove and plan.snippet_ref:
        path = os.environ.get("PVEOSI_MOCK_SNIPPET_PATH")
        if path:
            p = Path(path)
            if p.is_symlink():
                raise FinalizeError("Refusing to delete snippet symlink.")
            p.unlink(missing_ok=True)
        else:
            print(
                "Snippet removal requires manual deletion on the snippets storage "
                f"for {plan.snippet_ref} after verifying no references remain."
            )

    print("Finalization completed on the Proxmox host.")
    print("Boot the guest and verify SSH, password, and networking before deleting backups.")


def cmd_finalize(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pveosi finalize",
        description=(
            "Detach Proxmox Cloud-Init after the guest has been prepared and shut down. "
            "Does not modify guest passwords, SSH settings, or networking inside the VM."
        ),
    )
    parser.add_argument("vmid", type=int, help="VM ID to finalize")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; make no changes.")
    args = parser.parse_args(argv)

    if os.geteuid() != 0 and os.environ.get("PVEOSI_FINALIZE_ALLOW_NONROOT") != "1":
        print("ERROR: run as root.", file=sys.stderr)
        return 1

    try:
        ctx = load_context(args.vmid)
        validate_preconditions(ctx)
        print_guest_checklist()
        if args.dry_run:
            plan = build_plan(ctx, guest_attested=False)
            print_preview(plan, dry_run=True)
            return 0

        att = input("Confirm guest preparation steps above are complete (yes/no): ").strip().lower()
        if att not in ("yes", "y"):
            print("Cancelled.")
            return 0
        plan = build_plan(ctx, guest_attested=True)
        print_preview(plan, dry_run=False)
        if plan.already_finalized:
            return 0
        if plan.snapshot_blockers:
            print(f"ERROR: {'; '.join(plan.snapshot_blockers)}", file=sys.stderr)
            return 1
        confirm = input("Proceed with host-side Cloud-Init finalization? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("Cancelled.")
            return 0
        remove_snip = False
        if plan.snippet_ref:
            sn = input("Also remove the pveosi snippet file if safe? (yes/no): ").strip().lower()
            remove_snip = sn in ("yes", "y")
        execute_plan(ctx, plan, remove_snippet=remove_snip)
    except FinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "finalize":
        print("Usage: finalize_engine.py finalize <VMID> [--dry-run]", file=sys.stderr)
        sys.exit(2)
    sys.exit(cmd_finalize(sys.argv[2:]))


if __name__ == "__main__":
    main()
