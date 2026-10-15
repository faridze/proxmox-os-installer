import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight", BASE / "lib/preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightUnitTests(unittest.TestCase):
    def test_ssh_validate_ed25519(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGexampleexampleexampleexampleexa test@host"
        self.assertTrue(preflight.validate_ssh_pubkey_material(key).startswith("ssh-ed25519"))

    def test_ssh_rejects_garbage(self):
        with self.assertRaises(ValueError):
            preflight.validate_ssh_pubkey_material("not-a-key")

    def test_image_validate_size(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "disk.qcow2"
            img.write_bytes(b"\x00" * 64)
            info = {
                "format": "qcow2",
                "virtual-size": 50 * 1024**3,
                "backing-filename": None,
            }
            with mock.patch.object(preflight.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=0,
                    stdout=json.dumps(info),
                    stderr="",
                )
                preflight.validate_image_against_disk(img, 100)

    def test_image_rejects_small_disk(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "disk.qcow2"
            info = {"format": "qcow2", "virtual-size": 200 * 1024**3}
            with mock.patch.object(preflight.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=0,
                    stdout=json.dumps(info),
                    stderr="",
                )
                with self.assertRaises(ValueError):
                    preflight.validate_image_against_disk(img, 100)


if __name__ == "__main__":
    unittest.main()
