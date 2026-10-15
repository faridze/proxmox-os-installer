import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ssh_access", ROOT / "lib/ssh_access.py")
ssh_access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ssh_access)

SOURCE = """#cloud-config
hostname: testvm
user: debian
password: $6$fake-hash
chpasswd:
  expire: false
users:
  - default
"""

class SSHAccessTests(unittest.TestCase):
    def test_nonroot_password_user_preserves_proxmox_data(self):
        out = ssh_access.render_user_data(SOURCE, False)
        self.assertIn("password: $6$fake-hash", out)
        self.assertIn("ssh_pwauth: true", out)
        self.assertNotIn("PermitRootLogin yes", out)
    def test_root_password_requires_root_user(self):
        with self.assertRaises(ValueError):
            ssh_access.render_user_data(SOURCE, True)
    def test_root_password_explicit_opt_in(self):
        out = ssh_access.render_user_data(SOURCE.replace("user: debian", "user: root"), True)
        self.assertIn("disable_root: false", out)
        self.assertIn("PermitRootLogin yes", out)
        self.assertIn("PasswordAuthentication yes", out)
    def test_conflicting_source_fails_closed(self):
        with self.assertRaises(ValueError):
            ssh_access.render_user_data(SOURCE + "ssh_pwauth: false\n", False)
    def test_generated_snippet_private_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            snippets = Path(d) / "snippets"
            snippets.mkdir()
            original = Path(d) / "user.yaml"
            original.write_text(SOURCE)
            dest = snippets / "pveosi-9876-user.yaml"
            ssh_access.create_snippet(original, dest, False)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                ssh_access.create_snippet(original, dest, False)
    def test_rejects_path_outside_snippets(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "base"; src.write_text(SOURCE)
            with self.assertRaises(ValueError):
                ssh_access.create_snippet(src, Path(d) / "pveosi-123-user.yaml", False)

if __name__ == "__main__":
    unittest.main()
