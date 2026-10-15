#!/usr/bin/env python3
"""Optional post-install Cloud-Init finalization for pveosi-managed VMs (Proxmox VE 8+).

Proxmox VE 8+ semantics (documented, not executed destructively here):
- ``qm set <vmid> --delete <ideN>`` removes the IDE device from the VM config.
  The backing volume may remain and appear as ``unusedM:`` until manually removed.
- ``qm disk unlink <vmid> --idlink <ideN>`` detaches without deleting the volume
  (similar outcome for unused entries depending on version/options).
- This tool only uses ``qm set --delete`` for verified Cloud-Init IDE slots and
  ``--delete`` for Cloud-Init configuration keys. It never runs broad disk-delete
  commands or edits ``/etc/pve/qemu-server/*.conf`` directly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PVEOSI_TAG = "pveosi"
CI_KEY_EXACT = frozenset(
    {
        "cicustom",
        "ciuser",
        "cipassword",
        "sshkeys",
        "nameserver",
        "searchdomain",
        "citype",
        "ciupgrade",
    }
)
CI_KEY_PREFIX = ("ipconfig",)
SENSITIVE_RE = re.compile(r"(cipassword|password|sshkeys)", re.I)
BOOT_KEYS = ("scsi0", "scsi1", "sata0", "sata1", "virtio0", "virtio1")
NET_KEYS = ("net0", "net1", "net2", "net3")
VM_CLOUDINIT_VOL_RE = re.compile(r"^vm-(\d+)-cloudinit$")


class FinalizeError(Exception):
    """User-facing finalization failure."""


@dataclass
class IdeVolume:
    bus: str
    storage: str
    volid: str
    options: dict[str, str]
    raw: str


@dataclass
class VmContext:
    vmid: int
    config: dict[str, str]
    status: str
    snapshots: list[str]
    pveosi_owned: bool
    cloudinit_ide: Optional[str] = None
    cloudinit_volume: Optional[IdeVolume] = None
    cicustom_user: Optional[str] = None
    snippet_vmid: Optional[int] = None


@dataclass
class FinalizePlan:
    vmid: int
    name: str
    power_state: str
    cloudinit_ide: Optional[str]
    cloudinit_spec: Optional[str]
    config_keys_to_remove: list[str]
    snippet_ref: Optional[str]
    snippet_manual_only: bool
    snippet_blockers: list[str]
    snapshot_blockers: list[str]
    guest_readiness: str
    preserved_boot: dict[str, str]
    preserved_net: dict[str, str]
    operations: list[str] = field(default_factory=list)
    already_finalized: bool = False


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _parse_config_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip()
    return out


def qm_config(vmid: int) -> dict[str, str]:
    if "PVEOSI_MOCK_QM_CONFIG" in os.environ:
        text = Path(os.environ["PVEOSI_MOCK_QM_CONFIG"]).read_text(encoding="utf-8")
        return _parse_config_text(text)
    r = _run(["qm", "config", str(vmid)])
    if r.returncode != 0:
        raise FinalizeError(f"Cannot read VM {vmid} configuration.")
    return _parse_config_text(r.stdout)


def qm_status(vmid: int) -> str:
    if "PVEOSI_MOCK_QM_STATUS" in os.environ:
        return os.environ["PVEOSI_MOCK_QM_STATUS"].strip()
    r = _run(["qm", "status", str(vmid)])
    if r.returncode != 0:
        raise FinalizeError(f"Cannot read VM {vmid} status.")
    for line in r.stdout.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def _snapshots_from_pvesh_json(payload: Any) -> list[str]:
    names: list[str] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    else:
        raise FinalizeError("Unexpected snapshot API response format.")
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name and name != "current":
            names.append(name)
    return names


def _snapshots_from_listsnapshot(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("Name") or line.startswith("---"):
            continue
        token = line.split()[0]
        if token in ("current", "->"):
            continue
        if token.startswith("(") or token.endswith(")"):
            continue
        names.append(token)
    return names


def qm_snapshots(vmid: int) -> list[str]:
    if os.environ.get("PVEOSI_MOCK_SNAPSHOT_FAIL") == "1":
        raise FinalizeError(f"Cannot enumerate snapshots for VM {vmid}; aborting.")
    if "PVEOSI_MOCK_SNAPSHOTS_JSON" in os.environ:
        return _snapshots_from_pvesh_json(json.loads(os.environ["PVEOSI_MOCK_SNAPSHOTS_JSON"]))
    if "PVEOSI_MOCK_QM_SNAPSHOTS" in os.environ:
        raw = os.environ["PVEOSI_MOCK_QM_SNAPSHOTS"]
        if raw.startswith("["):
            return _snapshots_from_pvesh_json(json.loads(raw))
        return [s for s in raw.split(",") if s and s != "current"]
    node = os.environ.get("PVEOSI_NODE", "localhost")
    r = _run(
        [
            "pvesh",
            "get",
            f"/nodes/{node}/qemu/{vmid}/snapshot",
            "--output-format",
            "json",
        ]
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            return _snapshots_from_pvesh_json(json.loads(r.stdout))
        except (json.JSONDecodeError, FinalizeError):
            pass
    r = _run(["qm", "listsnapshot", str(vmid)])
    if r.returncode != 0:
        raise FinalizeError(f"Cannot enumerate snapshots for VM {vmid}; aborting.")
    return _snapshots_from_listsnapshot(r.stdout)


def _cluster_vm_configs() -> dict[int, dict[str, str]]:
    if os.environ.get("PVEOSI_MOCK_CLUSTER_FAIL") == "1":
        raise FinalizeError("Cannot query cluster VM list for snippet reference checks.")
    if "PVEOSI_MOCK_CLUSTER_CONFIGS" in os.environ:
        raw = json.loads(Path(os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"]).read_text(encoding="utf-8"))
        return {int(k): _parse_config_text(v) for k, v in raw.items()}
    r = _run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
    if r.returncode != 0:
        raise FinalizeError("Cannot query cluster VM list for snippet reference checks.")
    try:
        resources = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise FinalizeError("Cannot parse cluster VM list.") from exc
    configs: dict[int, dict[str, str]] = {}
    for item in resources:
        if not isinstance(item, dict):
            continue
        vmid = item.get("vmid")
        if vmid is None:
            continue
        try:
            vid = int(vmid)
        except (TypeError, ValueError):
            continue
        configs[vid] = qm_config(vid)
    return configs


def _parse_ide_volume(bus: str, raw: str) -> IdeVolume:
    if not raw or ":" not in raw.split(",", 1)[0]:
        raise FinalizeError(f"Ambiguous disk specification on {bus}: {raw!r}")
    base, *opts = raw.split(",")
    if ":" not in base:
        raise FinalizeError(f"Ambiguous disk specification on {bus}: {raw!r}")
    storage, volid = base.split(":", 1)
    if not storage or not volid:
        raise FinalizeError(f"Ambiguous disk specification on {bus}: {raw!r}")
    options: dict[str, str] = {}
    for opt in opts:
        if "=" in opt:
            k, v = opt.split("=", 1)
            options[k.strip()] = v.strip()
    return IdeVolume(bus=bus, storage=storage, volid=volid, options=options, raw=raw)


def _is_cloudinit_volume(vmid: int, vol: IdeVolume) -> bool:
    if vol.volid == "cloudinit":
        return True
    m = VM_CLOUDINIT_VOL_RE.match(vol.volid)
    if m and int(m.group(1)) == vmid:
        return True
    return False


def _find_cloudinit_ide(vmid: int, config: dict[str, str]) -> tuple[Optional[str], Optional[IdeVolume]]:
    found_bus: Optional[str] = None
    found_vol: Optional[IdeVolume] = None
    for key, val in config.items():
        if not re.fullmatch(r"ide\d+", key):
            continue
        vol = _parse_ide_volume(key, val)
        if _is_cloudinit_volume(vmid, vol):
            if found_bus is not None:
                raise FinalizeError("Multiple Cloud-Init IDE devices detected; refusing.")
            found_bus, found_vol = key, vol
            continue
        if "cloudinit" in vol.volid.lower():
            raise FinalizeError(
                f"{key} volume name looks like Cloud-Init but does not match a verified pattern."
            )
        if vol.options.get("media") == "cdrom" or ".iso" in vol.volid or "/iso/" in vol.volid:
            raise FinalizeError(f"{key} holds an ordinary CD-ROM/ISO, not a verified Cloud-Init drive.")
    return found_bus, found_vol


def _cloudinit_config_keys(config: dict[str, str]) -> list[str]:
    keys: list[str] = []
    for key in config:
        if key in CI_KEY_EXACT:
            keys.append(key)
        elif any(key.startswith(p) for p in CI_KEY_PREFIX):
            keys.append(key)
        elif key.startswith("ide") and "cloudinit" in config.get(key, ""):
            # legacy substring guard; verified IDE handled separately
            continue
    return sorted(keys)


def _parse_cicustom_user(config: dict[str, str]) -> Optional[str]:
    raw = config.get("cicustom", "")
    users: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.startswith("user="):
            users.append(part.split("=", 1)[1].strip())
        elif part.split("=", 1)[0] in ("network", "meta", "vendor"):
            raise FinalizeError(
                f"Unsupported cicustom reference ({part.split('=',1)[0]}); finalize manually."
            )
    if len(users) > 1:
        raise FinalizeError("Ambiguous cicustom user references.")
    return users[0] if users else None


def _pveosi_owned(config: dict[str, str], vmid: int, cicustom_user: Optional[str]) -> bool:
    tags = config.get("tags", "")
    if PVEOSI_TAG in [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]:
        return True
    if cicustom_user and re.fullmatch(r"pveosi-\d+-user\.yaml", Path(cicustom_user).name):
        m = re.search(r"pveosi-(\d+)-user\.yaml", cicustom_user)
        if m and int(m.group(1)) == vmid:
            return True
    return False


def _snippet_references_elsewhere(snippet_ref: str, vmid: int) -> list[str]:
    refs: list[str] = []
    cluster = _cluster_vm_configs()
    for other, cfg in cluster.items():
        if other == vmid:
            continue
        cic = _parse_cicustom_user(cfg)
        if cic and cic == snippet_ref:
            refs.append(str(other))
    return refs


def load_context(vmid: int) -> VmContext:
    if vmid < 100:
        raise FinalizeError("Invalid VM ID.")
    config = qm_config(vmid)
    if not config:
        raise FinalizeError(f"VM {vmid} does not exist.")
    status = qm_status(vmid)
    snaps = qm_snapshots(vmid)
    ide, vol = _find_cloudinit_ide(vmid, config)
    try:
        cic = _parse_cicustom_user(config)
    except FinalizeError:
        raise
    sn_vmid: Optional[int] = None
    if cic:
        m = re.fullmatch(r"pveosi-(\d+)-user\.yaml", Path(cic).name)
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
        cloudinit_volume=vol,
        cicustom_user=cic,
        snippet_vmid=sn_vmid,
    )


def build_plan(ctx: VmContext, guest_attested: bool) -> FinalizePlan:
    name = ctx.config.get("name", f"vm-{ctx.vmid}")
    keys = _cloudinit_config_keys(ctx.config)
    if ctx.cloudinit_ide and ctx.cloudinit_ide not in keys:
        keys.append(ctx.cloudinit_ide)
    keys = sorted(set(keys))

    blockers: list[str] = []
    if ctx.snapshots:
        blockers.append(f"snapshots present: {', '.join(ctx.snapshots)}")

    sn_blockers: list[str] = []
    snippet_ref = ctx.cicustom_user
    if snippet_ref:
        if not re.fullmatch(r"[^/]+:snippets/pveosi-\d+-user\.yaml", snippet_ref):
            if not re.fullmatch(r"pveosi-\d+-user\.yaml", Path(snippet_ref).name):
                sn_blockers.append("cicustom snippet is not a recognized pveosi volid")
            else:
                sn_blockers.append("cicustom snippet must be a full storage volid (storage:snippets/...)")
        elif ctx.snippet_vmid != ctx.vmid:
            sn_blockers.append("snippet VMID does not match this VM")
        else:
            others = _snippet_references_elsewhere(snippet_ref, ctx.vmid)
            if others:
                sn_blockers.append(f"snippet referenced by other VMs: {', '.join(others)}")

    already = ctx.cloudinit_ide is None and not keys
    guest_state = "operator-attested" if guest_attested else "unknown"
    ops: list[str] = []
    if not already:
        for k in keys:
            ops.append(f"qm set {ctx.vmid} --delete {k}")

    preserved_boot = {k: ctx.config[k] for k in BOOT_KEYS if k in ctx.config}
    preserved_net = {k: ctx.config[k] for k in NET_KEYS if k in ctx.config}

    return FinalizePlan(
        vmid=ctx.vmid,
        name=name,
        power_state=ctx.status,
        cloudinit_ide=ctx.cloudinit_ide,
        cloudinit_spec=ctx.cloudinit_volume.raw if ctx.cloudinit_volume else None,
        config_keys_to_remove=keys,
        snippet_ref=snippet_ref,
        snippet_manual_only=True,
        snippet_blockers=sn_blockers,
        snapshot_blockers=blockers,
        guest_readiness=guest_state,
        preserved_boot=preserved_boot,
        preserved_net=preserved_net,
        operations=ops,
        already_finalized=already,
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
    if ctx.cloudinit_ide is None and not _cloudinit_config_keys(ctx.config):
        return


def _assert_unchanged(before: dict[str, str], after: dict[str, str], keys: tuple[str, ...], label: str) -> None:
    for key in keys:
        if key in before and before.get(key) != after.get(key):
            raise FinalizeError(
                f"{label} changed during finalization ({key}); manual review required."
            )


def _unused_cloudinit_entries(config: dict[str, str], vmid: int) -> list[str]:
    unused: list[str] = []
    for key, val in config.items():
        if not key.startswith("unused"):
            continue
        try:
            vol = _parse_ide_volume(key, val)
        except FinalizeError:
            continue
        if _is_cloudinit_volume(vmid, vol):
            unused.append(f"{key}: {val}")
    return unused


def verify_post_finalize(
    vmid: int,
    plan: FinalizePlan,
    before_config: dict[str, str],
) -> None:
    after = qm_config(vmid)
    if _find_cloudinit_ide(vmid, after)[0]:
        raise FinalizeError("Cloud-Init IDE device is still attached; finalization failed.")
    remaining = _cloudinit_config_keys(after)
    if plan.cloudinit_ide and plan.cloudinit_ide in remaining:
        remaining.remove(plan.cloudinit_ide)
    if remaining:
        raise FinalizeError(
            f"Cloud-Init configuration keys remain ({', '.join(remaining)}); manual review required."
        )
    _assert_unchanged(before_config, after, tuple(plan.preserved_boot.keys()), "Boot disk")
    _assert_unchanged(before_config, after, tuple(plan.preserved_net.keys()), "Network")
    orphaned = _unused_cloudinit_entries(after, vmid)
    if orphaned:
        print("NOTE: Cloud-Init volume(s) may remain as unused entries (not deleted automatically):")
        for line in orphaned:
            print(f"  - {line}")
        print("Remove manually only after confirming volume identity and ownership.")


def _revalidate_for_execute(ctx: VmContext, plan: FinalizePlan) -> None:
    fresh = load_context(ctx.vmid)
    validate_preconditions(fresh)
    if fresh.status != "stopped":
        raise FinalizeError("VM is no longer stopped; aborting.")
    if fresh.snapshots:
        raise FinalizeError(f"Snapshots appeared during finalization: {', '.join(fresh.snapshots)}")
    if fresh.cloudinit_ide != plan.cloudinit_ide:
        raise FinalizeError("Cloud-Init disk identity changed; aborting.")
    if fresh.cloudinit_volume and plan.cloudinit_spec and fresh.cloudinit_volume.raw != plan.cloudinit_spec:
        raise FinalizeError("Cloud-Init disk specification changed; aborting.")
    expected = set(plan.config_keys_to_remove)
    current = set(_cloudinit_config_keys(fresh.config))
    if fresh.cloudinit_ide:
        current.add(fresh.cloudinit_ide)
    if current != expected and not (not current and plan.already_finalized):
        raise FinalizeError("VM Cloud-Init configuration changed since preview; aborting.")


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
    if plan.cloudinit_spec:
        print(f"  CI volume:   {plan.cloudinit_spec}")
    print(f"  Remove keys: {', '.join(plan.config_keys_to_remove) or 'none'}")
    print(f"  Snippet:     {plan.snippet_ref or 'none'}")
    print("  Snippet del: manual only (automatic deletion disabled in this release)")
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
        print(f"  - {op}")
    print()


def execute_plan(ctx: VmContext, plan: FinalizePlan) -> None:
    if plan.snapshot_blockers:
        raise FinalizeError(f"Cannot finalize: {'; '.join(plan.snapshot_blockers)}")
    if plan.already_finalized:
        print("Already finalized; no host changes applied.")
        return
    if plan.snippet_blockers:
        raise FinalizeError(f"Snippet reference is ambiguous: {'; '.join(plan.snippet_blockers)}")

    before_config = dict(ctx.config)
    _revalidate_for_execute(ctx, plan)

    applied: list[str] = []
    try:
        for key in plan.config_keys_to_remove:
            _revalidate_for_execute(ctx, plan)
            r = _run(["qm", "set", str(ctx.vmid), "--delete", key])
            if r.returncode != 0:
                raise FinalizeError(
                    f"Failed while removing {key}. Applied: {', '.join(applied) or 'none'}. "
                    f"Inspect 'qm config {ctx.vmid}' before retrying."
                )
            applied.append(key)
        verify_post_finalize(ctx.vmid, plan, before_config)
    except FinalizeError:
        raise

    if plan.snippet_ref:
        print(
            "Snippet file retention: delete manually on the snippets storage only after verifying "
            f"no VM references remain: {plan.snippet_ref}"
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
        execute_plan(ctx, plan)
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
