#!/usr/bin/env python3
"""Safe optional deletion of pveosi Cloud-Init snippet files after finalize."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

PVEOSI_SNIPPET_VOL_RE = re.compile(r"^([^:]+):snippets/pveosi-(\d+)-user\.yaml$")
PVEOSI_SNIPPET_NAME_RE = re.compile(r"^pveosi-(\d+)-user\.yaml$")


class SnippetCleanupError(Exception):
    """User-facing snippet cleanup failure."""


@dataclass
class SnippetCleanupPlan:
    vmid: int
    volid: str
    host_path: Optional[str]
    safe_to_delete: bool
    blockers: list[str]
    already_absent: bool


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def resolve_snippet_path(volid: str) -> str:
    mock_json = os.environ.get("PVEOSI_MOCK_SNIPPET_PATHS")
    if mock_json:
        import json

        store = json.loads(mock_json)
        if volid in store:
            return str(store[volid])
        raise SnippetCleanupError(f"Cannot resolve snippet path for {volid}.")
    if "PVEOSI_MOCK_SNIPPET_PATH" in os.environ:
        mapping = os.environ["PVEOSI_MOCK_SNIPPET_PATH"]
        if mapping.startswith("/"):
            return mapping
        raise SnippetCleanupError(f"Cannot resolve snippet path for {volid}.")
    r = _run(["pvesm", "path", volid])
    if r.returncode != 0:
        raise SnippetCleanupError(f"Cannot resolve Proxmox storage path for {volid}.")
    return r.stdout.strip()


def validate_snippet_path(volid: str, host_path: str, vmid: int) -> None:
    m = PVEOSI_SNIPPET_VOL_RE.match(volid)
    if not m or int(m.group(2)) != vmid:
        raise SnippetCleanupError("Snippet volume reference is not a recognized pveosi snippet for this VM.")
    path = Path(host_path)
    if path.is_symlink():
        raise SnippetCleanupError("Snippet path is a symlink; refusing deletion.")
    try:
        real = path.resolve(strict=False)
    except OSError as exc:
        raise SnippetCleanupError("Cannot resolve snippet path safely.") from exc
    if not str(real).startswith("/"):
        raise SnippetCleanupError("Snippet path is not an absolute filesystem path.")
    if real.name != f"pveosi-{vmid}-user.yaml":
        raise SnippetCleanupError("Snippet filename does not match this VM.")
    if real.parent.name != "snippets":
        raise SnippetCleanupError("Snippet path is outside snippets storage.")
    if ".." in path.parts:
        raise SnippetCleanupError("Snippet path contains traversal components.")


def assess_snippet_cleanup(
    vmid: int,
    volid: str,
    snapshots: list[str],
    cluster_refs_elsewhere: Callable[[str, int], list[str]],
) -> SnippetCleanupPlan:
    blockers: list[str] = []
    m = PVEOSI_SNIPPET_VOL_RE.match(volid)
    if not m:
        blockers.append("not a pveosi snippets volid")
    elif int(m.group(2)) != vmid:
        blockers.append("snippet VMID does not match target VM")
    if snapshots:
        blockers.append(
            "VM has snapshots; snippet may be referenced by snapshot metadata — delete manually"
        )
    try:
        others = cluster_refs_elsewhere(volid, vmid)
        if others:
            blockers.append(f"snippet still referenced by VM(s): {', '.join(others)}")
    except Exception as exc:
        blockers.append(f"cluster reference check failed: {exc}")

    host_path: Optional[str] = None
    already_absent = False
    if not blockers:
        try:
            host_path = resolve_snippet_path(volid)
            validate_snippet_path(volid, host_path, vmid)
            p = Path(host_path)
            if not p.exists():
                already_absent = True
            elif not p.is_file():
                blockers.append("snippet path is not a regular file")
        except SnippetCleanupError as exc:
            blockers.append(str(exc))

    return SnippetCleanupPlan(
        vmid=vmid,
        volid=volid,
        host_path=host_path,
        safe_to_delete=not blockers,
        blockers=blockers,
        already_absent=already_absent,
    )


def execute_snippet_cleanup(plan: SnippetCleanupPlan) -> str:
    """Return status: removed | not_found | retained."""
    if not plan.safe_to_delete:
        raise SnippetCleanupError(
            f"Snippet cleanup refused: {'; '.join(plan.blockers)}"
        )
    if plan.already_absent or not plan.host_path:
        return "not_found"
    path = Path(plan.host_path)
    if path.is_symlink():
        raise SnippetCleanupError("Snippet path is a symlink; refusing deletion.")
    if not path.exists():
        return "not_found"
    path.unlink()
    if path.exists():
        raise SnippetCleanupError("Snippet file still present after deletion.")
    return "removed"
