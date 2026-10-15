import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "proxmox-qm-config"
spec = importlib.util.spec_from_file_location("finalize_engine", ROOT / "lib" / "finalize_engine.py")
finalize = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = finalize
spec.loader.exec_module(finalize)


class FinalizeEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Path(self.tmp) / "qm.conf"
        self.cluster = Path(self.tmp) / "cluster.json"
        self.env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_cfg(self, text: str) -> None:
        self.cfg.write_text(text, encoding="utf-8")
        os.environ["PVEOSI_MOCK_QM_CONFIG"] = str(self.cfg)
        os.environ["PVEOSI_MOCK_QM_STATUS"] = "stopped"
        os.environ["PVEOSI_MOCK_QM_SNAPSHOTS"] = ""
        os.environ["PVEOSI_FINALIZE_ALLOW_NONROOT"] = "1"

    def _write_cluster(self, configs: dict[int, str]) -> None:
        self.cluster.write_text(
            json.dumps({str(k): v for k, v in configs.items()}),
            encoding="utf-8",
        )
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)

    def test_refuse_running_vm(self):
        self._write_cfg("name: test\nide2: local-lvm:cloudinit\ntags: pveosi\n")
        os.environ["PVEOSI_MOCK_QM_STATUS"] = "running"
        ctx = finalize.load_context(501)
        with self.assertRaises(finalize.FinalizeError):
            finalize.validate_preconditions(ctx)

    def test_refuse_template(self):
        self._write_cfg("template: 1\ntags: pveosi\n")
        ctx = finalize.load_context(501)
        with self.assertRaises(finalize.FinalizeError):
            finalize.validate_preconditions(ctx)

    def test_refuse_locked_vm(self):
        self._write_cfg("lock: backup\ntags: pveosi\n")
        ctx = finalize.load_context(501)
        with self.assertRaises(finalize.FinalizeError):
            finalize.validate_preconditions(ctx)

    def test_refuse_missing_ownership(self):
        self._write_cfg("name: foreign\nide2: local-lvm:cloudinit\n")
        ctx = finalize.load_context(501)
        with self.assertRaises(finalize.FinalizeError):
            finalize.validate_preconditions(ctx)

    def test_preview_lists_cloudinit_keys(self):
        self._write_cfg(
            "name: test\n"
            "tags: pveosi\n"
            "ide2: local-lvm:cloudinit\n"
            "cicustom: user=local:snippets/pveosi-501-user.yaml\n"
            "ciuser: root\n"
            "citype: nocloud\n"
            "ipconfig0: ip=dhcp\n"
        )
        self._write_cluster(
            {
                501: self.cfg.read_text(),
            }
        )
        ctx = finalize.load_context(501)
        finalize.validate_preconditions(ctx)
        plan = finalize.build_plan(ctx, guest_attested=True)
        self.assertIn("ide2", plan.config_keys_to_remove)
        self.assertIn("cicustom", plan.config_keys_to_remove)
        self.assertIn("citype", plan.config_keys_to_remove)
        self.assertIn("ipconfig0", plan.config_keys_to_remove)
        self.assertTrue(plan.snippet_manual_only)
        self.assertFalse(plan.snippet_blockers)

    def test_pve8_cloudinit_volume_with_media_cdrom(self):
        text = (FIXTURES / "pve8-vm501-cloudinit-cdrom.conf").read_text(encoding="utf-8")
        self._write_cfg(text)
        self._write_cluster({501: text})
        ctx = finalize.load_context(501)
        finalize.validate_preconditions(ctx)
        plan = finalize.build_plan(ctx, guest_attested=True)
        self.assertEqual(plan.cloudinit_ide, "ide2")
        self.assertIn("vm-501-cloudinit", plan.cloudinit_spec or "")

    def test_refuse_ordinary_ide2_cdrom(self):
        self._write_cfg("name: test\ntags: pveosi\nide2: local:iso/test.iso,media=cdrom\n")
        with self.assertRaises(finalize.FinalizeError):
            finalize.load_context(501)

    def test_refuse_fake_cloudinit_volid(self):
        self._write_cfg("name: test\ntags: pveosi\nide2: local-lvm:vm-999-cloudinit\n")
        with self.assertRaises(finalize.FinalizeError):
            finalize.load_context(501)

    def test_snapshot_blocker(self):
        self._write_cfg("name: test\ntags: pveosi\nide2: local-lvm:cloudinit\n")
        os.environ["PVEOSI_MOCK_QM_SNAPSHOTS"] = "snap1"
        ctx = finalize.load_context(501)
        finalize.validate_preconditions(ctx)
        plan = finalize.build_plan(ctx, guest_attested=True)
        self.assertTrue(plan.snapshot_blockers)

    def test_snapshots_json_excludes_current(self):
        os.environ["PVEOSI_MOCK_SNAPSHOTS_JSON"] = json.dumps(
            [{"name": "current"}, {"name": "before-install"}, {"name": "post-update"}]
        )
        self._write_cfg("name: test\ntags: pveosi\nide2: local-lvm:cloudinit\n")
        ctx = finalize.load_context(501)
        self.assertEqual(ctx.snapshots, ["before-install", "post-update"])

    def test_snapshot_enumeration_failure_aborts(self):
        self._write_cfg("name: test\ntags: pveosi\nide2: local-lvm:cloudinit\n")
        os.environ["PVEOSI_MOCK_SNAPSHOT_FAIL"] = "1"
        with self.assertRaises(finalize.FinalizeError) as ctx_err:
            finalize.load_context(501)
        self.assertIn("snapshots", str(ctx_err.exception).lower())

    def test_shared_snippet_blocks_cleanup_not_finalize(self):
        snippet = "local:snippets/pveosi-501-user.yaml"
        cfg501 = (
            "name: test\n"
            "tags: pveosi\n"
            f"cicustom: user={snippet}\n"
            "ide2: local-lvm:cloudinit\n"
        )
        cfg502 = f"name: other\ntags: pveosi\ncicustom: user={snippet}\n"
        self._write_cfg(cfg501)
        self._write_cluster({501: cfg501, 502: cfg502})
        ctx = finalize.load_context(501)
        finalize.validate_preconditions(ctx)
        plan = finalize.build_plan(ctx, guest_attested=True)
        self.assertTrue(plan.snippet_blockers)
        self.assertTrue(any("502" in b for b in plan.snippet_blockers))
        os.environ["PVEOSI_MOCK_STATEFUL"] = "1"
        os.environ["PVEOSI_MOCK_SKIP_SNIPPET_CLEANUP"] = "1"
        finalize.execute_plan(ctx, plan, guest_attested=True)
        self.cfg.write_text(finalize._config_dict_to_text(finalize.qm_config(501)), encoding="utf-8")
        self.assertNotIn("cicustom", finalize.qm_config(501))

    def test_cluster_query_failure_not_treated_as_empty(self):
        self._write_cfg(
            "name: test\n"
            "tags: pveosi\n"
            "cicustom: user=local:snippets/pveosi-501-user.yaml\n"
            "ide2: local-lvm:cloudinit\n"
        )
        os.environ["PVEOSI_MOCK_CLUSTER_FAIL"] = "1"
        ctx = finalize.load_context(501)
        with self.assertRaises(finalize.FinalizeError):
            finalize.build_plan(ctx, guest_attested=True)

    def test_verify_post_finalize_refuses_still_attached(self):
        self._write_cfg("name: test\ntags: pveosi\nide2: local-lvm:cloudinit\n")
        ctx = finalize.load_context(501)
        plan = finalize.build_plan(ctx, guest_attested=True)
        before = dict(ctx.config)
        with self.assertRaises(finalize.FinalizeError) as exc:
            finalize.verify_post_finalize(501, plan, before)
        self.assertIn("still attached", str(exc.exception).lower())

    def test_verify_post_finalize_reports_unused_volume(self):
        self._write_cfg(
            "name: test\n"
            "tags: pveosi\n"
            "unused0: local-lvm:vm-501-cloudinit\n"
        )
        ctx = finalize.load_context(501)
        plan = finalize.build_plan(ctx, guest_attested=True)
        before = dict(ctx.config)
        # No CI keys or IDE — already finalized shape
        plan = finalize.FinalizePlan(
            vmid=501,
            name="test",
            power_state="stopped",
            cloudinit_ide=None,
            cloudinit_spec=None,
            config_keys_to_remove=[],
            snippet_ref=None,
            snippet_manual_only=True,
            snippet_blockers=[],
            snapshot_blockers=[],
            guest_readiness="test",
            preserved_boot={},
            preserved_net={},
            already_finalized=True,
        )
        finalize.verify_post_finalize(501, plan, before)

    def test_dry_run_does_not_mutate(self):
        self._write_cfg("name: test\ntags: pveosi\nide2: local-lvm:cloudinit\n")
        before = self.cfg.read_text()
        rc = finalize.cmd_finalize(["501", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.cfg.read_text(), before)

    def test_no_password_in_preview_output(self):
        self._write_cfg(
            "name: test\n"
            "tags: pveosi\n"
            "cipassword: secret\n"
            "ide2: local-lvm:cloudinit\n"
        )
        ctx = finalize.load_context(501)
        plan = finalize.build_plan(ctx, guest_attested=False)
        text = str(plan.config_keys_to_remove)
        self.assertNotIn("secret", text)

    def test_refuse_unsupported_cicustom_network(self):
        self._write_cfg(
            "name: test\n"
            "tags: pveosi\n"
            "cicustom: network=local:snippets/net.yaml\n"
            "ide2: local-lvm:cloudinit\n"
        )
        with self.assertRaises(finalize.FinalizeError):
            finalize.load_context(501)

    def test_fixture_vm123_cloudinit_identity(self):
        text = (FIXTURES / "pve8-vm123-cloudinit.conf").read_text(encoding="utf-8")
        self._write_cfg(text)
        self._write_cluster({123: text})
        ctx = finalize.load_context(123)
        self.assertEqual(ctx.cloudinit_ide, "ide2")
        vol = ctx.cloudinit_volume
        self.assertIsNotNone(vol)
        self.assertEqual(vol.volid, "vm-123-cloudinit")


if __name__ == "__main__":
    unittest.main()
