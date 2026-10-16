import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock
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
        (self.root / "run/systemd/system").mkdir(parents=True)
        (self.root / "etc/cloud").mkdir(parents=True)

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
        self.assertEqual(pf.os_info.id, "debian")
        self.assertEqual(pf.network_backend, "netplan")

    def test_ubuntu_2404_supported(self):
        self._write_os_release('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04 LTS"\n')
        (self.root / "etc/network").mkdir(parents=True)
        (self.root / "etc/network/interfaces").write_text("auto lo\n", encoding="utf-8")
        pf = guest.run_preflight()
        self.assertEqual(pf.os_info.version_id, "24.04")

    def test_refuse_unsupported_os(self):
        self._write_os_release('ID=almalinux\nVERSION_ID="9.4"\nPRETTY_NAME="AlmaLinux 9.4"\n')
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/a.yaml").write_text("network:\n  version: 2\n", encoding="utf-8")
        with self.assertRaises(guest.GuestSetupError):
            guest.run_preflight()

    def test_refuse_unknown_network(self):
        self._write_os_release('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n')
        with self.assertRaises(guest.GuestSetupError) as ctx:
            guest.run_preflight()
        self.assertIn("network", str(ctx.exception).lower())

    def test_refuse_networkmanager_active(self):
        self._write_os_release('ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04"\n')
        (self.root / "etc/network").mkdir(parents=True)
        (self.root / "etc/network/interfaces").write_text("auto lo\n", encoding="utf-8")
        os.environ["PVEOSI_GUEST_MOCK_NM_ACTIVE"] = "active"
        # Pretend nmcli exists so NM detection runs before ifupdown-only path.
        os.environ["PATH"] = f"{self.root}/bin:{os.environ.get('PATH', '')}"
        (self.root / "bin").mkdir()
        (self.root / "bin/nmcli").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.root / "bin/nmcli").chmod(0o755)
        with self.assertRaises(guest.GuestSetupError) as ctx:
            guest.run_preflight()
        self.assertIn("NetworkManager", str(ctx.exception))

    def test_disable_cloud_init_creates_marker(self):
        self._write_os_release('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n')
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text("network:\n  version: 2\n", encoding="utf-8")
        marker = self.root / "etc/cloud/cloud-init.disabled"
        with mock.patch("builtins.input", side_effect=["yes", "no"]):
            guest.disable_cloud_init_interactive()
        self.assertTrue(marker.is_file())

    def test_cmd_preflight_exit_zero(self):
        self._write_os_release('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n')
        (self.root / "etc/netplan").mkdir()
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text("network:\n  version: 2\n", encoding="utf-8")
        self.assertEqual(guest.cmd_main(["--preflight"]), 0)


if __name__ == "__main__":
    unittest.main()
