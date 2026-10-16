"""PVEOSI-014B guest checkpoint integrity regressions."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(Path(__file__).resolve().parents[1])

spec = importlib.util.spec_from_file_location("guest_setup_engine", ROOT / "lib" / "guest_setup_engine.py")
guest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guest
spec.loader.exec_module(guest)

NETPLAN = """\
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
"""

BOOT_A = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
BOOT_B = "bbbbbbbb-bbbb-cccc-dddd-000000000002"

IPJ_V1 = (
    '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.2","prefixlen":24,"dynamic":true}],'
    '"flags":["UP"]}]'
)
IPJ_V2 = (
    '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.99","prefixlen":24,"dynamic":true}],'
    '"flags":["UP"]}]'
)
ROUTE = '[{"dst":"default","gateway":"10.0.0.1","dev":"eth0"}]'


class GuestStateIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.env = os.environ.copy()
        os.environ["PVEOSI_GUEST_ROOT"] = str(self.root)
        os.environ["PVEOSI_GUEST_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_GUEST_MOCK_CMD"] = "1"
        os.environ["PVEOSI_GUEST_MOCK_CI_STATUS"] = "done"
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ADDR"] = IPJ_V1
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ROUTE"] = ROUTE
        (self.root / "run/systemd/system").mkdir(parents=True)
        (self.root / "etc/cloud").mkdir(parents=True)
        (self.root / "var/lib/cloud/instance/sem").mkdir(parents=True)
        (self.root / "var/lib/cloud/instance/sem/config_scripts_user").write_text("", encoding="utf-8")
        (self.root / "proc/sys/kernel/random").mkdir(parents=True)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_A}\n", encoding="utf-8")
        (self.root / "etc/os-release").write_text(
            'ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian 13"\n', encoding="utf-8"
        )
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text(NETPLAN, encoding="utf-8")
        eff = self.root / "eff.txt"
        eff.write_text("stable", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_NETPLAN_GET"] = f"file:{eff}"
        (self.root / "etc/hostname").write_text("lab\n", encoding="utf-8")
        (self.root / "etc/resolv.conf").write_text("nameserver 10.0.0.1\n", encoding="utf-8")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reboot_then_prepare_blocked_instead_of_baseline_refresh(self):
        """Regression: post-reboot PREPARE must not rewrite network_snapshot."""
        guest.cmd_prepare(dry_run=False)
        st1 = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ADDR"] = IPJ_V2
        with self.assertRaises(guest.GuestSetupError) as ctx:
            guest.cmd_prepare(dry_run=False)
        self.assertIn("verify", str(ctx.exception).lower())
        st2 = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertEqual(st1["network_snapshot"], st2["network_snapshot"])

    def test_same_boot_prepare_preserves_snapshot_when_ip_changes(self):
        guest.cmd_prepare(dry_run=False)
        st1 = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ADDR"] = IPJ_V2
        guest.cmd_prepare(dry_run=False)
        st2 = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertEqual(st1["network_snapshot"], st2["network_snapshot"])

    def test_ifupdown_does_not_create_cloud_init_disabled(self):
        import shutil as sh

        sh.rmtree(self.root / "etc/netplan")
        (self.root / "etc/network").mkdir()
        (self.root / "etc/network/interfaces").write_text("auto lo\n", encoding="utf-8")
        guest.cmd_prepare(dry_run=False)
        self.assertFalse((self.root / "etc/cloud/cloud-init.disabled").exists())
        st = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertTrue(st.get("manual_only"))

    def test_non_netplan_verify_requires_manual_only(self):
        import shutil as sh

        sh.rmtree(self.root / "etc/netplan")
        (self.root / "etc/network").mkdir()
        (self.root / "etc/network/interfaces").write_text("auto lo\n", encoding="utf-8")
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        with mock.patch("builtins.input", return_value="yes"):
            with self.assertRaises(guest.GuestSetupError):
                guest.cmd_verify(interactive=True, manual_only=False)

    def test_cloud_init_error_with_semaphore_refused(self):
        os.environ["PVEOSI_GUEST_MOCK_CI_STATUS"] = "status: error"
        with self.assertRaises(guest.GuestSetupError):
            guest.cmd_prepare(dry_run=False)

    def test_sshd_config_invalid_blocks_verify(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_SSHD_T_FAIL"] = "1"
        with mock.patch("builtins.input", return_value="yes"):
            with self.assertRaises(guest.GuestSetupError):
                guest.cmd_verify(interactive=True)

    def test_netplan_verify_sets_machine_verified(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        with mock.patch("builtins.input", return_value="yes"):
            guest.cmd_verify(interactive=True)
        st = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertTrue(st["machine_verified"])
        self.assertEqual(st["phase"], "verified")


if __name__ == "__main__":
    unittest.main()
