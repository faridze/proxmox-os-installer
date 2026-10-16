import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finalize_engine", ROOT / "lib" / "finalize_engine.py")
finalize = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = finalize
spec.loader.exec_module(finalize)


def _base_config() -> str:
    return (
        "name: stateful-test\n"
        "tags: pveosi\n"
        "scsi0: local-lvm:vm-501-disk-0,size=32G\n"
        "net0: virtio=BC:24:11:AA:BB:CC,bridge=vmbr0\n"
        "vga: std\n"
        "serial0: socket\n"
        "ide2: local-lvm:vm-501-cloudinit,media=cdrom\n"
        "cicustom: user=local:snippets/pveosi-501-user.yaml\n"
        "ciuser: root\n"
        "citype: nocloud\n"
        "ipconfig0: ip=dhcp\n"
        "cipassword: $6$rounds=5000$salt$hashvalue\n"
    )


class StatefulFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Path(self.tmp) / "qm.conf"
        self.cluster = Path(self.tmp) / "cluster.json"
        self.status_file = Path(self.tmp) / "status"
        self.snap_file = Path(self.tmp) / "snapshots"
        self.env = os.environ.copy()
        self.status_file.write_text("stopped", encoding="utf-8")
        self.snap_file.write_text("", encoding="utf-8")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _enable_stateful(self, cfg_text: str) -> None:
        self.cfg.write_text(cfg_text, encoding="utf-8")
        os.environ["PVEOSI_MOCK_STATEFUL"] = "1"
        os.environ["PVEOSI_MOCK_QM_CONFIG"] = str(self.cfg)
        os.environ["PVEOSI_MOCK_QM_STATUS_FILE"] = str(self.status_file)
        os.environ["PVEOSI_MOCK_QM_SNAPSHOTS_FILE"] = str(self.snap_file)
        os.environ["PVEOSI_FINALIZE_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_MOCK_SKIP_SNIPPET_CLEANUP"] = "1"
        snippet = "local:snippets/pveosi-501-user.yaml"
        self.cluster.write_text(
            json.dumps({"501": _base_config()}),
            encoding="utf-8",
        )
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)

    def _load_and_plan(self):
        ctx = finalize.load_context(501)
        finalize.validate_preconditions(ctx)
        plan = finalize.build_plan(ctx, guest_attested=True)
        return ctx, plan

    def test_successful_multi_key_sequence(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        self.assertGreater(len(plan.config_keys_to_remove), 2)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            finalize.execute_plan(ctx, plan)
        out = buf.getvalue()
        self.assertNotIn("hashvalue", out)
        self.assertNotIn("$6$rounds", out)
        after = finalize.qm_config(501)
        self.assertEqual(finalize._current_ci_key_set(after, 501), set())
        self.assertEqual(after.get("scsi0"), "local-lvm:vm-501-disk-0,size=32G")
        self.assertEqual(after.get("net0"), "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0")
        self.assertEqual(after.get("vga"), "std")
        self.assertEqual(after.get("serial0"), "socket")

    def test_second_delete_failure_leaves_partial(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        os.environ["PVEOSI_MOCK_QM_SET_FAIL_ON"] = "ciuser"
        with self.assertRaises(finalize.FinalizeError) as err:
            finalize.execute_plan(ctx, plan)
        self.assertIn("Remaining:", str(err.exception))
        self.assertIn("Completed:", str(err.exception))
        self.assertNotIn("hashvalue", str(err.exception))
        after = finalize.qm_config(501)
        self.assertIn("ciuser", after)
        self.assertNotIn("cicustom", after)

    def test_unrelated_config_change_aborts(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        real_run = finalize._run
        deletes = 0

        def hooked(cmd):
            nonlocal deletes
            res = real_run(cmd)
            if res.returncode == 0 and cmd[0:2] == ["qm", "set"] and "--delete" in cmd:
                deletes += 1
                if deletes == 1:
                    text = self.cfg.read_text(encoding="utf-8")
                    self.cfg.write_text(
                        text.replace(
                            "scsi0: local-lvm:vm-501-disk-0,size=32G",
                            "scsi0: local-lvm:vm-501-disk-0,size=64G",
                        ),
                        encoding="utf-8",
                    )
            return res

        with mock.patch.object(finalize, "_run", hooked):
            with self.assertRaises(finalize.FinalizeError) as err:
                finalize.execute_plan(ctx, plan)
        self.assertIn("Non-Cloud-Init", str(err.exception))

    def test_vm_became_running_aborts(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        real_run = finalize._run
        deletes = 0

        def hooked(cmd):
            nonlocal deletes
            res = real_run(cmd)
            if res.returncode == 0 and cmd[0:2] == ["qm", "set"] and "--delete" in cmd:
                deletes += 1
                if deletes == 1:
                    self.status_file.write_text("running", encoding="utf-8")
            return res

        with mock.patch.object(finalize, "_run", hooked):
            with self.assertRaises(finalize.FinalizeError) as err:
                finalize.execute_plan(ctx, plan)
        self.assertIn("stopped", str(err.exception).lower())

    def test_lock_appears_aborts(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        real_run = finalize._run
        deletes = 0

        def hooked(cmd):
            nonlocal deletes
            res = real_run(cmd)
            if res.returncode == 0 and cmd[0:2] == ["qm", "set"] and "--delete" in cmd:
                deletes += 1
                if deletes == 1:
                    text = self.cfg.read_text(encoding="utf-8")
                    self.cfg.write_text(f"lock: backup\n{text}", encoding="utf-8")
            return res

        with mock.patch.object(finalize, "_run", hooked):
            with self.assertRaises(finalize.FinalizeError) as err:
                finalize.execute_plan(ctx, plan)
        self.assertIn("lock", str(err.exception).lower())

    def test_snapshot_appears_aborts(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        real_run = finalize._run
        deletes = 0

        def hooked(cmd):
            nonlocal deletes
            res = real_run(cmd)
            if res.returncode == 0 and cmd[0:2] == ["qm", "set"] and "--delete" in cmd:
                deletes += 1
                if deletes == 1:
                    self.snap_file.write_text("snap-new", encoding="utf-8")
            return res

        with mock.patch.object(finalize, "_run", hooked):
            with self.assertRaises(finalize.FinalizeError) as err:
                finalize.execute_plan(ctx, plan)
        self.assertIn("snapshot", str(err.exception).lower())

    def test_cloudinit_disk_identity_change_aborts(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        real_run = finalize._run
        deletes = 0

        def hooked(cmd):
            nonlocal deletes
            res = real_run(cmd)
            if res.returncode == 0 and cmd[0:2] == ["qm", "set"] and "--delete" in cmd:
                deletes += 1
                if deletes == 1 and "ide2:" in self.cfg.read_text(encoding="utf-8"):
                    text = self.cfg.read_text(encoding="utf-8")
                    text = text.replace(
                        "ide2: local-lvm:vm-501-cloudinit,media=cdrom",
                        "ide2: local-lvm:vm-501-cloudinit,media=disk",
                    )
                    self.cfg.write_text(text, encoding="utf-8")
            return res

        with mock.patch.object(finalize, "_run", hooked):
            with self.assertRaises(finalize.FinalizeError):
                finalize.execute_plan(ctx, plan)

    def test_safe_retry_after_partial(self):
        self._enable_stateful(_base_config())
        ctx, plan = self._load_and_plan()
        os.environ["PVEOSI_MOCK_QM_SET_FAIL_ON"] = "ciuser"
        with self.assertRaises(finalize.FinalizeError):
            finalize.execute_plan(ctx, plan)
        del os.environ["PVEOSI_MOCK_QM_SET_FAIL_ON"]
        ctx2 = finalize.load_context(501)
        plan2 = finalize.build_plan(ctx2, guest_attested=True)
        self.assertIn("ciuser", plan2.config_keys_to_remove)
        finalize.execute_plan(ctx2, plan2)
        self.assertEqual(finalize._current_ci_key_set(finalize.qm_config(501), 501), set())

    def test_post_verify_detects_leftover_ci(self):
        self._enable_stateful("name: t\ntags: pveosi\nide2: local-lvm:cloudinit\nciuser: root\n")
        ctx = finalize.load_context(501)
        plan = finalize.build_plan(ctx, guest_attested=True)
        before = dict(ctx.config)
        with self.assertRaises(finalize.FinalizeError):
            finalize.verify_post_finalize(501, plan, before)

    def test_cluster_node_specific_config(self):
        resources = Path(self.tmp) / "resources.json"
        pvesh = Path(self.tmp) / "pvesh.json"
        snippet = "local:snippets/pveosi-501-user.yaml"
        resources.write_text(
            json.dumps([{"vmid": 501, "node": "pve1"}, {"vmid": 502, "node": "pve2"}]),
            encoding="utf-8",
        )
        pvesh.write_text(
            json.dumps(
                {
                    "pve1:501": f"name: a\ntags: pveosi\ncicustom: user={snippet}\nide2: local-lvm:cloudinit\n",
                    "pve2:502": f"name: b\ncicustom: user={snippet}\n",
                }
            ),
            encoding="utf-8",
        )
        os.environ["PVEOSI_MOCK_CLUSTER_RESOURCES"] = str(resources)
        os.environ["PVEOSI_MOCK_PVESH_CONFIGS"] = str(pvesh)
        refs = finalize._snippet_references_elsewhere(snippet, 501)
        self.assertEqual(refs, ["502"])

    def test_cluster_missing_node_fails_closed(self):
        resources = Path(self.tmp) / "resources.json"
        resources.write_text(json.dumps([{"vmid": 502}]), encoding="utf-8")
        os.environ["PVEOSI_MOCK_CLUSTER_RESOURCES"] = str(resources)
        with self.assertRaises(finalize.FinalizeError):
            finalize._cluster_vm_configs()


if __name__ == "__main__":
    unittest.main()
