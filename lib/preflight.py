#!/usr/bin/env python3
"""Proxmox preflight checks (storage, bridges, VM IDs, images). No VM mutations."""
import base64
import binascii
import json
import re
import subprocess
import sys
from pathlib import Path

THIN_TYPES = frozenset({"lvmthin", "zfspool", "rbd", "nfs"})

FREE_VMID_HINTS = (
    "does not exist",
    "not exist",
    "no such file",
    "unable to find configuration",
    "configuration file",
    "not found",
)


class VmidAvailabilityError(RuntimeError):
    """Cannot determine whether a VM ID is free (fail closed)."""


def _run_json(cmd):
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return json.loads(r.stdout or "null")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as exc:
        raise RuntimeError(f"command failed: {' '.join(cmd)} ({exc})") from exc


def vmid_availability(vmid):
    """Return 'free', 'in_use', or raise VmidAvailabilityError when uncertain."""
    if not str(vmid).isdigit():
        raise VmidAvailabilityError("invalid VM ID")
    r = subprocess.run(
        ["qm", "config", str(vmid)],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        return "in_use"
    text = f"{r.stderr or ''}\n{r.stdout or ''}".lower()
    if any(hint in text for hint in FREE_VMID_HINTS):
        return "free"
    raise VmidAvailabilityError(
        (r.stderr or r.stdout or "qm config failed").strip() or "qm config failed"
    )


def storage_rows():
    """Parse `pvesm status` table: name, type, status, total, used, avail, pct."""
    r = subprocess.run(["pvesm", "status"], check=True, capture_output=True, text=True)
    rows = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        name, typ, status = parts[0], parts[1], parts[2]
        try:
            total = int(parts[3])
            used = int(parts[4])
            avail = int(parts[5])
        except ValueError:
            continue
        rows.append(
            {
                "name": name,
                "type": typ,
                "status": status,
                "total_kib": total,
                "used_kib": used,
                "avail_kib": avail,
            }
        )
    return rows


def storage_supports_vm_disks(storage_id):
    r = subprocess.run(
        ["pvesm", "status", "--content", "images"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("cannot read storage content map")
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[0] == storage_id and parts[2] == "active":
            return True
    return False


def validate_storage(storage_id, disk_gib):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]*", storage_id):
        raise ValueError(f"invalid storage id: {storage_id}")
    rows = {r["name"]: r for r in storage_rows()}
    if storage_id not in rows:
        raise ValueError(f"storage {storage_id} not found")
    row = rows[storage_id]
    if row["status"] != "active":
        raise ValueError(f"storage {storage_id} is not active (status={row['status']})")
    if not storage_supports_vm_disks(storage_id):
        raise ValueError(
            f"storage {storage_id} does not support active VM disk images (images content)"
        )
    avail_gib = row["avail_kib"] / (1024 * 1024)
    thin = row["type"] in THIN_TYPES
    warnings = []
    if thin:
        warnings.append(
            f"Storage {storage_id} ({row['type']}) may use thin provisioning; "
            f"reported free space (~{avail_gib:.1f} GiB) is not a guarantee that "
            f"{disk_gib} GiB can be allocated safely."
        )
    elif avail_gib < float(disk_gib):
        raise ValueError(
            f"storage {storage_id} reports only ~{avail_gib:.1f} GiB free; "
            f"requested disk is {disk_gib} GiB"
        )
    return {"thin": thin, "avail_gib": avail_gib, "warnings": warnings}


def bridge_exists(bridge):
    r = subprocess.run(["ip", "link", "show", "dev", bridge], capture_output=True)
    return r.returncode == 0


def list_active_storages():
    out = []
    for row in storage_rows():
        if row["status"] != "active":
            continue
        if not storage_supports_vm_disks(row["name"]):
            continue
        out.append(row)
    return out


def pick_default_storage():
    for prefer in ("local-lvm", "local-zfs", "local"):
        for row in list_active_storages():
            if row["name"] == prefer:
                return prefer
    active = list_active_storages()
    return active[0]["name"] if active else "local-lvm"


def pick_default_bridge():
    return "vmbr0" if bridge_exists("vmbr0") else ""


def validate_image_against_disk(image_path, disk_gib):
    r = subprocess.run(
        ["qemu-img", "info", "--output=json", str(image_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(r.stdout)
    fmt = data.get("format")
    if fmt not in ("qcow2", "raw"):
        raise ValueError(f"unsupported image format: {fmt}")
    if data.get("backing-filename"):
        raise ValueError("image has a backing file; refusing to import")
    vsize = int(data["virtual-size"])
    need = int(disk_gib) * 1024**3
    if vsize > need:
        raise ValueError(
            f"target disk {disk_gib} GiB is smaller than image virtual size "
            f"({vsize / (1024**3):.2f} GiB)"
        )
    return vsize


def validate_ssh_pubkey_material(text):
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ValueError("empty SSH public key")
    if len(lines) != 1:
        raise ValueError("SSH public key must be a single line")
    line = lines[0]
    parts = line.split()
    if len(parts) < 2:
        raise ValueError("SSH public key must be type and base64 blob")
    keytype = parts[0]
    allowed_exact = (
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    )
    if keytype not in allowed_exact:
        raise ValueError(f"unsupported SSH key type: {keytype}")
    blob = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", blob):
        raise ValueError("invalid SSH key base64 payload")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid SSH key base64 payload") from exc
    if keytype == "ssh-ed25519" and len(raw) < 32:
        raise ValueError("ssh-ed25519 key material too short")
    if keytype == "ssh-rsa" and len(raw) < 256:
        raise ValueError("ssh-rsa key material too short")
    return line


def verify_vm_config(vmid, expect_scsi=True, expect_cloudinit=True):
    r = subprocess.run(["qm", "config", str(vmid)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read config for VM {vmid}")
    cfg = r.stdout
    missing = []
    if expect_scsi and "scsi0:" not in cfg:
        missing.append("scsi0 disk")
    if expect_cloudinit and "ide2:" not in cfg:
        missing.append("cloud-init drive (ide2)")
    if missing:
        raise RuntimeError(f"VM {vmid} incomplete configuration: missing {', '.join(missing)}")
    return True


def main():
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "vmid-free":
            try:
                state = vmid_availability(sys.argv[2])
            except VmidAvailabilityError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(2)
            sys.exit(1 if state == "in_use" else 0)
        if len(sys.argv) >= 4 and sys.argv[1] == "storage-validate":
            result = validate_storage(sys.argv[2], int(sys.argv[3]))
            for w in result["warnings"]:
                print(w, file=sys.stderr)
            print("OK")
            sys.exit(0)
        if sys.argv[1:2] == ["list-storages"]:
            for row in list_active_storages():
                avail_gib = row["avail_kib"] / (1024 * 1024)
                print(f"{row['name']}|{row['type']}|{avail_gib:.1f}")
            sys.exit(0)
        if sys.argv[1:2] == ["default-storage"]:
            print(pick_default_storage())
            sys.exit(0)
        if sys.argv[1:2] == ["default-bridge"]:
            print(pick_default_bridge())
            sys.exit(0)
        if len(sys.argv) >= 3 and sys.argv[1] == "bridge-exists":
            sys.exit(0 if bridge_exists(sys.argv[2]) else 1)
        if len(sys.argv) >= 4 and sys.argv[1] == "image-validate":
            validate_image_against_disk(sys.argv[2], int(sys.argv[3]))
            print("OK")
            sys.exit(0)
        if len(sys.argv) >= 3 and sys.argv[1] == "ssh-validate-file":
            text = Path(sys.argv[2]).read_text(encoding="utf-8")
            print(validate_ssh_pubkey_material(text))
            sys.exit(0)
        if len(sys.argv) >= 3 and sys.argv[1] == "ssh-validate-line":
            print(validate_ssh_pubkey_material(sys.argv[2]))
            sys.exit(0)
        if len(sys.argv) >= 3 and sys.argv[1] == "verify-vm":
            verify_vm_config(sys.argv[2])
            print("OK")
            sys.exit(0)
        print(
            "Usage: preflight.py vmid-free ID | storage-validate ID DISK_GIB | "
            "list-storages | default-storage | default-bridge | bridge-exists BR | "
            "image-validate PATH DISK_GIB | ssh-validate-file PATH | "
            "ssh-validate-line KEY | verify-vm ID",
            file=sys.stderr,
        )
        return 2
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
