#!/usr/bin/env python3
"""Render a root-only Proxmox Cloud-Init user-data snippet for FIRST BOOT only.

Starts with `qm cloudinit dump VMID user` (preserves Proxmox user, password
hash, hostname and keys) and appends opt-in SSH settings. No YAML dependency.
The generated file must remain accessible for VM start/migration.
"""
import os
import re
import sys
from pathlib import Path

_BLOCKED = re.compile(r"(?m)^(?:ssh_pwauth|disable_root|write_files|runcmd)\s*:")


def render_user_data(source: str, root_login: bool) -> str:
    if not source.startswith("#cloud-config\n") or "\x00" in source:
        raise ValueError("unexpected Proxmox user-data format")
    if _BLOCKED.search(source):
        raise ValueError("generated user-data contains conflicting SSH settings")
    if root_login and not re.search(r"(?m)^user:\s*root\s*$", source):
        raise ValueError("root password login requires cloud-init user=root")
    additions = "\nssh_pwauth: true\n"
    if root_login:
        additions += """disable_root: false
write_files:
  - path: /etc/ssh/sshd_config.d/00-pveosi-root-password.conf
    owner: root:root
    permissions: '0644'
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
runcmd:
  - [sh, -c, 'sshd -t && (systemctl reload ssh || systemctl reload sshd) || true']
"""
    return source.rstrip("\n") + "\n" + additions


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
