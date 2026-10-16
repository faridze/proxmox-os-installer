import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ssh_access", ROOT / "lib/ssh_access.py")
ssh_access = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ssh_access
SPEC.loader.exec_module(ssh_access)

FIXTURES = ROOT / "tests/fixtures/cloudinit"

SOURCE = """#cloud-config
hostname: testvm
user: debian
password: $6$fake-hash
chpasswd:
  expire: false
users:
  - default
"""


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class SSHAccessTests(unittest.TestCase):
    def test_nonroot_password_user_preserves_proxmox_data(self):
        out = ssh_access.render_user_data(SOURCE, False)
        self.assertIn("password: $6$fake-hash", out)
        self.assertIn("ssh_pwauth: true", out)
        self.assertNotIn("PermitRootLogin yes", out)
        self.assertNotIn("00-pveosi-root-password.conf", out)

    def test_reported_proxmox_failure_disable_root_false(self):
        src = _load_fixture("proxmox-debian-root-disable-root-false.yaml")
        out = ssh_access.render_user_data(src, True)
        self.assertIn("password: $6$rounds=656000$testhashvalue", out)
        self.assertIn("disable_root: false", out)
        self.assertIn("ssh_pwauth: true", out)
        self.assertIn("PermitRootLogin yes", out)
        self.assertEqual(out.count("ssh_pwauth:"), 1)

    def test_root_password_requires_root_user(self):
        with self.assertRaises(ValueError):
            ssh_access.render_user_data(SOURCE, True)

    def test_root_password_explicit_opt_in(self):
        out = ssh_access.render_user_data(SOURCE.replace("user: debian", "user: root"), True)
        self.assertIn("disable_root: false", out)
        self.assertIn("PermitRootLogin yes", out)
        self.assertIn("PasswordAuthentication yes", out)

    def test_existing_ssh_pwauth_true_preserved(self):
        src = SOURCE + "ssh_pwauth: true\n"
        out = ssh_access.render_user_data(src, False)
        self.assertIn("ssh_pwauth: true", out)
        self.assertEqual(out.count("ssh_pwauth:"), 1)

    def test_existing_ssh_pwauth_false_upgraded_for_password_ssh(self):
        src = SOURCE + "ssh_pwauth: false\n"
        out = ssh_access.render_user_data(src, False)
        self.assertIn("ssh_pwauth: true", out)

    def test_disable_root_true_nonroot_unchanged(self):
        src = SOURCE.replace("user: debian", "user: debian") + "disable_root: true\n"
        out = ssh_access.render_user_data(src, False)
        self.assertIn("disable_root: true", out)

    def test_merge_existing_write_files_and_runcmd(self):
        src = (
            SOURCE.replace("user: debian", "user: root")
            + "write_files:\n  - path: /etc/motd\n    content: hi\n"
            + "runcmd:\n  - echo hello\n"
        )
        out = ssh_access.render_user_data(src, True)
        self.assertIn("/etc/motd", out)
        self.assertIn("echo hello", out)
        self.assertIn(_SSHD := "/etc/ssh/sshd_config.d/00-pveosi-root-password.conf", out)
        self.assertEqual(out.count("write_files:"), 1)
        self.assertEqual(out.count("runcmd:"), 1)

    def test_duplicate_top_level_keys_fail_closed(self):
        bad = SOURCE + "hostname: duplicate\n"
        with self.assertRaises(ValueError) as ctx:
            ssh_access.render_user_data(bad, False)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_ssh_authorized_keys_preserved(self):
        src = SOURCE + "ssh_authorized_keys:\n  - ssh-ed25519 AAAAtest\n"
        out = ssh_access.render_user_data(src, False)
        self.assertIn("ssh-ed25519 AAAAtest", out)

    def test_valid_yaml_roundtrip(self):
        import yaml

        out = ssh_access.render_user_data(_load_fixture("proxmox-debian-root-disable-root-false.yaml"), True)
        data = yaml.safe_load(out.split("\n", 1)[1])
        self.assertIsInstance(data, dict)
        self.assertTrue(data.get("ssh_pwauth"))

    def test_no_plaintext_password_in_output(self):
        secret = "SuperSecretPlaintext!"
        src = f"#cloud-config\nuser: root\npassword: $6$hashedonly\nplaintext leak test\n"
        out = ssh_access.render_user_data(
            src.replace("plaintext leak test", f"chpasswd:\n  expire: false\n"),
            True,
        )
        self.assertNotIn(secret, out)

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
            src = Path(d) / "base"
            src.write_text(SOURCE)
            with self.assertRaises(ValueError):
                ssh_access.create_snippet(src, Path(d) / "pveosi-123-user.yaml", False)

    def test_no_duplicate_top_level_keys_in_rendered(self):
        out = ssh_access.render_user_data(_load_fixture("proxmox-debian-root-disable-root-false.yaml"), True)
        keys = []
        for line in out.splitlines():
            if line and not line.startswith((" ", "#", "-")) and ":" in line:
                keys.append(line.split(":", 1)[0].strip())
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
