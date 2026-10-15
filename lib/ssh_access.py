#!/usr/bin/env python3
"""Render Proxmox Cloud-Init user-data snippets for opt-in SSH password login.

Merges with `qm cloudinit dump VMID user` output (password hash, keys, hostname).
Requires PyYAML (python3-yaml on Proxmox VE 8+).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised on minimal hosts only
    yaml = None  # type: ignore[assignment]

_SSHD_DROPIN = "/etc/ssh/sshd_config.d/00-pveosi-root-password.conf"
_RUN_CMD_SHELL = "sshd -t && (systemctl reload ssh || systemctl reload sshd) || true"
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _require_yaml() -> Any:
    if yaml is None:
        raise ValueError(
            "PyYAML is required to merge Cloud-Init user-data (install python3-yaml on the node)"
        )
    return yaml


def _duplicate_top_level_keys(raw_body: str) -> list[str]:
    seen: set[str] = set()
    dups: list[str] = []
    for line in raw_body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t", "-")):
            continue
        m = _TOP_KEY_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key in seen:
            dups.append(key)
        seen.add(key)
    return dups


def _cloud_user(data: dict[str, Any]) -> str:
    user = data.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()
    return ""


def _merge_write_files(existing: Any, root_login: bool) -> list[dict[str, Any]]:
    if existing is None:
        base: list[dict[str, Any]] = []
    elif isinstance(existing, list):
        base = [e for e in existing if isinstance(e, dict)]
        if len(base) != len(existing):
            raise ValueError("unsupported write_files entry types")
    else:
        raise ValueError("unsupported write_files structure")

    if not root_login:
        return base

    ours = {
        "path": _SSHD_DROPIN,
        "owner": "root:root",
        "permissions": "0644",
        "content": (
            "PermitRootLogin yes\n"
            "PasswordAuthentication yes\n"
        ),
    }
    merged = [e for e in base if e.get("path") != _SSHD_DROPIN]
    merged.append(ours)
    return merged


def _merge_runcmd(existing: Any, root_login: bool) -> list[Any]:
    if existing is None:
        base: list[Any] = []
    elif isinstance(existing, list):
        base = list(existing)
    else:
        raise ValueError("unsupported runcmd structure")

    if not root_login:
        return base

    ours = ["sh", "-c", _RUN_CMD_SHELL]
    if ours in base:
        return base
    # Avoid duplicate reload if an equivalent shell one-liner exists.
    for entry in base:
        if isinstance(entry, list) and len(entry) >= 3 and entry[0] == "sh":
            cmd = " ".join(str(x) for x in entry[1:])
            if "sshd" in cmd and "reload" in cmd:
                return base
    base.append(ours)
    return base


def _apply_ssh_pwauth(data: dict[str, Any]) -> None:
    """Password SSH was explicitly requested; ensure ssh_pwauth enables it."""
    current = data.get("ssh_pwauth")
    if current is False or current is None:
        data["ssh_pwauth"] = True
    elif current is True:
        return
    else:
        raise ValueError(f"unsupported ssh_pwauth value: {current!r}")


def render_user_data(source: str, root_login: bool) -> str:
    y = _require_yaml()
    if not source.startswith("#cloud-config") or "\x00" in source:
        raise ValueError("unexpected Proxmox user-data format")

    header = "#cloud-config"
    body = source
    if source.startswith("#cloud-config\n"):
        body = source[len("#cloud-config\n") :]
    elif source.startswith("#cloud-config\r\n"):
        body = source[len("#cloud-config\r\n") :]

    dups = _duplicate_top_level_keys(body)
    if dups:
        raise ValueError(
            f"ambiguous cloud-config contains duplicate top-level keys: {', '.join(sorted(set(dups)))}"
        )

    try:
        data = y.safe_load(body) or {}
    except y.YAMLError as exc:
        raise ValueError(f"cloud-config YAML is not parseable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("cloud-config root must be a mapping")

    _apply_ssh_pwauth(data)

    if root_login:
        if _cloud_user(data) != "root":
            raise ValueError("root password login requires cloud-init user=root")
        data["disable_root"] = False
    # Non-root password SSH: do not alter disable_root (Proxmox may set disable_root: false).

    data["write_files"] = _merge_write_files(data.get("write_files"), root_login)
    data["runcmd"] = _merge_runcmd(data.get("runcmd"), root_login)

    rendered_body = y.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if not isinstance(rendered_body, str):
        raise ValueError("failed to render cloud-config YAML")

    # Validate round-trip and single mapping.
    try:
        reparsed = y.safe_load(rendered_body)
    except y.YAMLError as exc:
        raise ValueError(f"rendered cloud-config is invalid YAML: {exc}") from exc
    if not isinstance(reparsed, dict):
        raise ValueError("rendered cloud-config root must be a mapping")

    out = f"{header}\n{rendered_body}".rstrip("\n") + "\n"
    post_dups = _duplicate_top_level_keys(out.split("\n", 1)[-1])
    if post_dups:
        raise ValueError("internal error: rendered cloud-config has duplicate top-level keys")
    return out


def create_snippet(source_path: Path, output_path: Path, root_login: bool) -> None:
    if output_path.name.startswith(".") or not re.fullmatch(
        r"pveosi-[0-9]{3,9}-user\.yaml", output_path.name
    ):
        raise ValueError("unsafe snippet filename")
    if output_path.parent.name != "snippets" or not output_path.is_absolute():
        raise ValueError("snippet path must be an absolute /snippets/... location")
    rendered = render_user_data(source_path.read_text(encoding="utf-8"), root_login)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(output_path), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] != "create" or argv[4] not in ("user", "root"):
        print("Usage: ssh_access.py create PROXMOX_DUMP SNIPPET_PATH user|root", file=sys.stderr)
        return 2
    try:
        create_snippet(Path(argv[2]), Path(argv[3]), argv[4] == "root")
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"ERROR: cannot create cloud-init snippet: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
