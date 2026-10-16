import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight", BASE / "lib/preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class VmidTests(unittest.TestCase):
    def _mock_qm(self, returncode, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_in_use(self):
        with mock.patch.object(
            preflight,
            "_vmid_from_cluster_resources",
            return_value=None,
        ), mock.patch.object(
            preflight.subprocess,
            "run",
            return_value=self._mock_qm(0),
        ):
            self.assertEqual(preflight.vmid_availability("100"), "in_use")

    def test_free_strict_message(self):
        with mock.patch.object(
            preflight,
            "_vmid_from_cluster_resources",
            return_value=None,
        ), mock.patch.object(
            preflight.subprocess,
            "run",
            return_value=self._mock_qm(
                2,
                stderr="Configuration file 'nodes/pve/qemu-server/100.conf' does not exist",
            ),
        ):
            self.assertEqual(preflight.vmid_availability("100"), "free")

    def test_misleading_configuration_file_fail_closed(self):
        with mock.patch.object(
            preflight,
            "_vmid_from_cluster_resources",
            return_value=None,
        ), mock.patch.object(
            preflight.subprocess,
            "run",
            return_value=self._mock_qm(
                2,
                stderr="configuration file permission denied for cluster filesystem",
            ),
        ):
            with self.assertRaises(preflight.VmidAvailabilityError):
                preflight.vmid_availability("100")

    def test_cluster_resources_in_use(self):
        with mock.patch.object(
            preflight,
            "_vmid_from_cluster_resources",
            return_value="in_use",
        ):
            self.assertEqual(preflight.vmid_availability("200"), "in_use")

    def test_cluster_resources_free(self):
        with mock.patch.object(
            preflight,
            "_vmid_from_cluster_resources",
            return_value="free",
        ):
            self.assertEqual(preflight.vmid_availability("200"), "free")

    def test_unknown_fail_closed(self):
        with mock.patch.object(
            preflight,
            "_vmid_from_cluster_resources",
            return_value=None,
        ), mock.patch.object(
            preflight.subprocess,
            "run",
            return_value=self._mock_qm(111, stderr="cluster not ready"),
        ):
            with self.assertRaises(preflight.VmidAvailabilityError):
                preflight.vmid_availability("100")


if __name__ == "__main__":
    unittest.main()
