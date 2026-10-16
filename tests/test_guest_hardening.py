"""PVEOSI-014 guest prepare/verify hardening regressions."""
import importlib.util
import json
import os
import stat
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

gn_spec = importlib.util.spec_from_file_location("guest_netplan", ROOT / "lib" / "guest_netplan.py")
gn = importlib.util.module_from_spec(gn_spec)
sys.modules[gn_spec.name] = gn
gn_spec.loader.exec_module(gn)

gnw_spec = importlib.util.spec_from_file_location("guest_network", ROOT / "lib" / "guest_network.py")
gnw = importlib.util.module_from_spec(gnw_spec)
sys.modules[gnw_spec.name] = gnw
gnw_spec.loader.exec_module(gnw)

NETPLAN = """\
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
"""

BOOT_A = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
BOOT_B = "bbbbbbbb-bbbb-cccc-dddd-000000000002"

IPJ_STABLE = (
    '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.2","prefixlen":24,"dynamic":true}],'
    '"flags":["UP"]}]'
)
ROUTE_STABLE = '[{"dst":"default","gateway":"10.0.0.1","dev":"eth0"}]'


class GuestHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.env = os.environ.copy()
        os.environ["PVEOSI_GUEST_ROOT"] = str(self.root)
        os.environ["PVEOSI_GUEST_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_GUEST_MOCK_CMD"] = "1"
        os.environ["PVEOSI_GUEST_MOCK_CI_STATUS"] = "done"
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ADDR"] = IPJ_STABLE
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ROUTE"] = ROUTE_STABLE
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

    def _migrate_once(self):
        gn.migrate_netplan_independence(dry_run=False)

    def test_repeated_prepare_after_retirement(self):
        self._migrate_once()
        guest.cmd_prepare(dry_run=False)
        self.assertFalse((self.root / "etc/netplan/50-cloud-init.yaml").exists())
        backups = list((self.root / "var/lib/pveosi-guest-setup/backups").iterdir())
        self.assertTrue(any(p.name.endswith(".retired") for p in backups))

    def test_repeated_prepare_after_verify_preserves_verified(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        with mock.patch("builtins.input", return_value="yes"):
            guest.cmd_verify(interactive=True)
        guest.cmd_prepare(dry_run=False)
        st = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertTrue(st["machine_verified"])
        self.assertEqual(st["phase"], "verified")

    def test_user_managed_dest_without_provenance(self):
        dest = self.root / "etc/netplan/90-pveosi-static.yaml"
        dest.write_text("network:\n  version: 2\n", encoding="utf-8")
        with self.assertRaises(gn.NetplanSafetyError):
            gn.build_netplan_plan()

    def test_netplan_dest_permissions_umask_022(self):
        old = os.umask(0o022)
        try:
            self._migrate_once()
            mode = stat.S_IMODE((self.root / "etc/netplan/90-pveosi-static.yaml").stat().st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            os.umask(old)

    def test_symlink_dest_refused(self):
        real = self.root / "etc/netplan/real.yaml"
        real.write_text(NETPLAN, encoding="utf-8")
        (self.root / "etc/netplan/90-pveosi-static.yaml").symlink_to(real)
        with self.assertRaises(gn.NetplanSafetyError):
            gn.migration_state()

    def test_dhcp_address_change_needs_confirm(self):
        b = gnw.capture_network_facts()
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ADDR"] = (
            '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.9","prefixlen":24,"dynamic":true}],'
            '"flags":["UP"]}]'
        )
        a = gnw.capture_network_facts()
        r = gnw.compare_network_facts(b, a)
        self.assertTrue(r.needs_operator_confirm)

    def test_static_ip_change_fails(self):
        b = gnw.NetworkFacts(
            interfaces=[gnw.IfaceFacts("eth0", ["10.0.0.2/24"], "static")],
            default_gw="10.0.0.1",
            default_dev="eth0",
            default_metric=None,
        )
        a = gnw.NetworkFacts(
            interfaces=[gnw.IfaceFacts("eth0", ["10.0.0.3/24"], "static")],
            default_gw="10.0.0.1",
            default_dev="eth0",
            default_metric=None,
        )
        r = gnw.compare_network_facts(b, a)
        self.assertFalse(r.ok)

    def test_gateway_change_on_static_fails(self):
        static_netplan = NETPLAN.replace("dhcp4: true", "addresses: [10.0.0.2/24]")
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text(static_netplan, encoding="utf-8")
        b = gnw.capture_network_facts()
        os.environ["PVEOSI_GUEST_MOCK_IPJ_ROUTE"] = '[{"dst":"default","gateway":"10.0.0.9","dev":"eth0"}]'
        a = gnw.capture_network_facts()
        r = gnw.compare_network_facts(b, a)
        self.assertFalse(r.ok)

    def test_dns_failure_blocks_verify(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_DNS_FAIL"] = "1"
        with mock.patch("builtins.input", return_value="yes"):
            with self.assertRaises(guest.GuestSetupError):
                guest.cmd_verify(interactive=True)

    def test_ssh_inactive_blocks_verify(self):
        guest.cmd_prepare(dry_run=False)
        (self.root / "proc/sys/kernel/random/boot_id").write_text(f"{BOOT_B}\n", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_SSH_ACTIVE"] = "inactive"
        with mock.patch("builtins.input", return_value="yes"):
            with self.assertRaises(guest.GuestSetupError):
                guest.cmd_verify(interactive=True)

    def test_networkmanager_with_netplan_refused(self):
        os.environ["PVEOSI_GUEST_MOCK_NM_ACTIVE"] = "active"
        with self.assertRaises(guest.GuestSetupError):
            guest.run_preflight()

    def test_invalid_boot_id_in_checkpoint(self):
        guest.cmd_prepare(dry_run=False)
        sf = self.root / "var/lib/pveosi-guest-setup/state.json"
        st = json.loads(sf.read_text())
        st["prepare_boot_id"] = "not-a-uuid"
        sf.write_text(json.dumps(st))
        with self.assertRaises(guest.GuestSetupError):
            guest._read_state()

    def test_noninteractive_verify_refused(self):
        with self.assertRaises(guest.GuestSetupError):
            guest.cmd_verify(interactive=False)

    def test_prepare_preserves_boot_id_for_reboot_gate(self):
        guest.cmd_prepare(dry_run=False)
        guest.cmd_prepare(dry_run=False)
        st = json.loads((self.root / "var/lib/pveosi-guest-setup/state.json").read_text())
        self.assertEqual(st["prepare_boot_id"], BOOT_A)


if __name__ == "__main__":
    unittest.main()
