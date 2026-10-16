import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("guest_setup_engine", ROOT / "lib" / "guest_setup_engine.py")
guest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guest
spec.loader.exec_module(guest)


class GuestSetupTests(unittest.TestCase):
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

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_os_release(self, text: str) -> None:
        (self.root / "etc/os-release").write_text(text, encoding="utf-8")

    def test_debian_13_netplan_preflight(self):
        self._write_os_release('ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian GNU/Linux 13"\n')
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text("network:\n  version: 2\n", encoding="utf-8")
        pf = guest.run_preflight()
        self.assertEqual(pf.network_backend, "netplan")

    def test_refuse_unsupported_os(self):
        self._write_os_release('ID=almalinux\nVERSION_ID="9.4"\nPRETTY_NAME="AlmaLinux 9.4"\n')
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/a.yaml").write_text("network:\n  version: 2\n", encoding="utf-8")
        with self.assertRaises(guest.GuestSetupError):
            guest.run_preflight()

    def test_cmd_preflight_and_status(self):
        self._write_os_release('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n')
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text("network:\n  version: 2\n", encoding="utf-8")
        self.assertEqual(guest.cmd_main(["--preflight"]), 0)
        self.assertEqual(guest.cmd_main(["--status"]), 0)

    def test_wrapper_finds_engine_next_to_bundle(self):
        wrapper = ROOT / "bin/pveosi-guest-setup"
        self.assertTrue(wrapper.is_file())
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn("guest_setup_engine.py", text)
        self.assertNotIn("proxmox-os-installer/lib", text)


if __name__ == "__main__":
    unittest.main()
