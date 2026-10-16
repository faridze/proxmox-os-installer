import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "lib" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


finalize = _load("finalize_engine", "finalize_engine.py")
console = _load("console_engine", "console_engine.py")


class ConsoleEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Path(self.tmp) / "qm.conf"
        self.status = Path(self.tmp) / "status"
        self.env = os.environ.copy()
        self.status.write_text("stopped", encoding="utf-8")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text: str) -> None:
        self.cfg.write_text(text, encoding="utf-8")
        os.environ["PVEOSI_MOCK_QM_CONFIG"] = str(self.cfg)
        os.environ["PVEOSI_MOCK_QM_STATUS_FILE"] = str(self.status)
        os.environ["PVEOSI_MOCK_STATEFUL"] = "1"
        os.environ["PVEOSI_CONSOLE_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_MOCK_QM_SNAPSHOTS"] = ""

    def test_convert_serial_to_vga_stopped_vm(self):
        self._write(
            "name: t\n"
            "tags: pveosi\n"
            "vga: serial0\n"
            "serial0: socket\n"
            "scsi0: local-lvm:disk0\n"
            "net0: virtio=aa,bridge=vmbr0\n"
        )
        state = console.load_console_state(601)
        console.validate_console_preconditions(state)
        plan = console.build_console_plan(state, remove_serial=False)
        self.assertEqual(plan.operations, ["qm set 601 --vga std"])
        console.execute_console_plan(state, plan)
        after = finalize.qm_config(601)
        self.assertEqual(after.get("vga"), "std")
        self.assertEqual(after.get("serial0"), "socket")
        self.assertEqual(after.get("scsi0"), "local-lvm:disk0")

    def test_refuse_running_vm(self):
        self._write("name: t\ntags: pveosi\nvga: serial0\nserial0: socket\n")
        self.status.write_text("running", encoding="utf-8")
        state = console.load_console_state(601)
        with self.assertRaises(finalize.FinalizeError):
            console.validate_console_preconditions(state)

    def test_remove_serial_without_vga_blocked(self):
        self._write("name: t\ntags: pveosi\nvga: serial0\nserial0: socket\n")
        state = console.load_console_state(601)
        with self.assertRaises(finalize.FinalizeError):
            console.build_console_plan(state, remove_serial=True)

    def test_remove_serial_after_vga(self):
        self._write(
            "name: t\ntags: pveosi\nvga: std\nserial0: socket\nscsi0: local-lvm:disk0\n"
        )
        state = console.load_console_state(601)
        plan = console.build_console_plan(state, remove_serial=True)
        console.execute_console_plan(state, plan)
        after = finalize.qm_config(601)
        self.assertNotIn("serial0", after)
        self.assertEqual(after.get("vga"), "std")

    def test_finalized_vm_console_still_works(self):
        self._write(
            "name: t\n"
            "tags: pveosi\n"
            "vga: serial0\n"
            "serial0: socket\n"
            "scsi0: local-lvm:disk0\n"
        )
        state = console.load_console_state(601)
        console.validate_console_preconditions(state)
        plan = console.build_console_plan(state, remove_serial=False)
        self.assertTrue(plan.operations)

    def test_graphical_warning_in_plan(self):
        self._write("name: t\ntags: pveosi\nvga: serial0\nserial0: socket\n")
        plan = console.build_console_plan(console.load_console_state(601), False)
        self.assertTrue(any("graphical" in w.lower() or "vga" in w.lower() for w in plan.warnings))


if __name__ == "__main__":
    unittest.main()
