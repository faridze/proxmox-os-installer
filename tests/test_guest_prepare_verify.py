import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
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


class PrepareVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.env = os.environ.copy()
        os.environ["PVEOSI_GUEST_ROOT"] = str(self.root)
        os.environ["PVEOSI_GUEST_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_GUEST_MOCK_CMD"] = "1"
        os.environ["PVEOSI_GUEST_MOCK_CI_STATUS"] = "done"
        (self.root / "run/systemd/system").mkdir(parents=True)
        (self.root / "etc/cloud").mkdir(parents=True)
        (self.root / "var/lib/cloud/instance/sem").mkdir(parents=True)
        (self.root / "var/lib/cloud/instance/sem/config_scripts_user").write_text("", encoding="utf-8")
        (self.root / "proc/sys/kernel/random").mkdir(parents=True)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(
            "aaaaaaaa-bbbb-cccc-dddd-000000000001\n", encoding="utf-8"
        )
        (self.root / "etc/os-release").write_text(
            'ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian 13"\n', encoding="utf-8"
        )
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text(NETPLAN, encoding="utf-8")
        eff = self.root / "eff.txt"
        eff.write_text("stable", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_NETPLAN_GET"] = f"file:{eff}"
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ADDR"] = (
            '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.2","prefixlen":24,"dynamic":true}],'
            '"flags":["UP"]}]'
        )
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ROUTE"] = '[{"dst":"default","gateway":"10.0.0.1","dev":"eth0"}]'
        (self.root / "etc/resolv.conf").write_text("nameserver 10.0.0.1\n", encoding="utf-8")
        (self.root / "etc/hostname").write_text("lab\n", encoding="utf-8")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prepare_checkpoint_and_ci_disabled(self):
        guest.cmd_prepare(dry_run=False)
        self.assertTrue((self.root / "etc/cloud/cloud-init.disabled").exists())
        st = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertEqual(st["phase"], "prepared")
        self.assertEqual(st["prepare_boot_id"], "aaaaaaaa-bbbb-cccc-dddd-000000000001")

    def test_dry_run_prepare_no_state(self):
        guest.cmd_prepare(dry_run=True)
        self.assertFalse((self.root / "var/lib/pveosi-guest-setup/state.json").exists())
        self.assertFalse((self.root / "etc/cloud/cloud-init.disabled").exists())

    def test_verify_requires_reboot(self):
        guest.cmd_prepare(dry_run=False)
        with self.assertRaises(guest.GuestSetupError):
            guest.cmd_verify(interactive=True)

    def test_verify_success_with_attestation(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(
            "bbbbbbbb-bbbb-cccc-dddd-000000000002\n", encoding="utf-8"
        )
        with mock.patch("builtins.input", return_value="yes"):
            guest.cmd_verify(interactive=True)
        st = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertTrue(st["machine_verified"])
        self.assertEqual(st["phase"], "verified")

    def test_verify_blocks_without_ssh_attest(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text("boot-bbb\n", encoding="utf-8")
        with mock.patch("builtins.input", return_value="no"):
            with self.assertRaises(guest.GuestSetupError):
                guest.cmd_verify(interactive=True)

    def test_first_boot_incomplete_refused(self):
        (self.root / "var/lib/cloud/instance/sem/config_scripts_user").unlink()
        os.environ["PVEOSI_GUEST_MOCK_CI_STATUS"] = "not run"
        with self.assertRaises(guest.GuestSetupError):
            guest.cmd_prepare(dry_run=False)

    def test_ifupdown_fail_closed(self):
        import shutil as sh

        sh.rmtree(self.root / "etc/netplan")
        (self.root / "etc/network").mkdir()
        (self.root / "etc/network/interfaces").write_text("auto lo\n", encoding="utf-8")
        guest.cmd_prepare(dry_run=False)
        self.assertFalse((self.root / "etc/netplan/90-pveosi-static.yaml").exists())

    def test_no_var_lib_cloud_deletion(self):
        cloud = self.root / "var/lib/cloud/data"
        cloud.mkdir(parents=True)
        cloud.joinpath("marker").write_text("keep", encoding="utf-8")
        guest.cmd_prepare(dry_run=False)
        self.assertTrue(cloud.joinpath("marker").exists())


if __name__ == "__main__":
    unittest.main()
