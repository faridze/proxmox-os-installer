import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("snippet_cleanup", ROOT / "lib" / "snippet_cleanup.py")
snippet = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = snippet
spec.loader.exec_module(snippet)

spec_f = importlib.util.spec_from_file_location("finalize_engine", ROOT / "lib" / "finalize_engine.py")
finalize = importlib.util.module_from_spec(spec_f)
sys.modules[spec_f.name] = finalize
spec_f.loader.exec_module(finalize)


class SnippetCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.snip_dir = Path(self.tmp) / "snippets"
        self.snip_dir.mkdir()
        self.snip = self.snip_dir / "pveosi-501-user.yaml"
        self.snip.write_text("users:\n  - name: root\n", encoding="utf-8")
        self.cluster = Path(self.tmp) / "cluster.json"
        self.env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _refs(self, volid, vmid):
        return finalize._snippet_references_elsewhere(volid, vmid)

    def test_safe_delete(self):
        volid = "local:snippets/pveosi-501-user.yaml"
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{self.snip}"
        self.cluster.write_text(json.dumps({"501": "tags: pveosi\n"}), encoding="utf-8")
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)
        plan = snippet.assess_snippet_cleanup(501, volid, [], self._refs)
        self.assertTrue(plan.safe_to_delete)
        self.assertEqual(snippet.execute_snippet_cleanup(plan), "removed")
        self.assertFalse(self.snip.exists())

    def test_missing_idempotent(self):
        volid = "local:snippets/pveosi-501-user.yaml"
        self.snip.unlink()
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{self.snip}"
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)
        self.cluster.write_text("{}", encoding="utf-8")
        plan = snippet.assess_snippet_cleanup(501, volid, [], self._refs)
        self.assertTrue(plan.already_absent)
        self.assertEqual(snippet.execute_snippet_cleanup(plan), "not_found")

    def test_shared_snippet_refused(self):
        volid = "local:snippets/pveosi-501-user.yaml"
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{self.snip}"
        cfg501 = f"tags: pveosi\ncicustom: user={volid}\n"
        cfg502 = f"cicustom: user={volid}\n"
        self.cluster.write_text(json.dumps({"501": cfg501, "502": cfg502}), encoding="utf-8")
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)
        plan = snippet.assess_snippet_cleanup(501, volid, [], self._refs)
        self.assertFalse(plan.safe_to_delete)
        self.assertTrue(any("502" in b for b in plan.blockers))

    def test_foreign_snippet_refused(self):
        volid = "local:snippets/custom-user.yaml"
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{self.snip}"
        plan = snippet.assess_snippet_cleanup(501, volid, [], lambda _v, _i: [])
        self.assertFalse(plan.safe_to_delete)

    def test_snapshot_refused(self):
        volid = "local:snippets/pveosi-501-user.yaml"
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{self.snip}"
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)
        self.cluster.write_text("{}", encoding="utf-8")
        plan = snippet.assess_snippet_cleanup(501, volid, ["snap1"], self._refs)
        self.assertFalse(plan.safe_to_delete)

    def test_symlink_refused(self):
        volid = "local:snippets/pveosi-501-user.yaml"
        link = Path(self.tmp) / "link.yaml"
        link.symlink_to(self.snip)
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{link}"
        os.environ["PVEOSI_MOCK_CLUSTER_CONFIGS"] = str(self.cluster)
        self.cluster.write_text("{}", encoding="utf-8")
        plan = snippet.assess_snippet_cleanup(501, volid, [], self._refs)
        self.assertFalse(plan.safe_to_delete)

    def test_cluster_fail_refused(self):
        volid = "local:snippets/pveosi-501-user.yaml"
        os.environ["PVEOSI_MOCK_SNIPPET_PATH"] = f"{volid}:{self.snip}"
        os.environ["PVEOSI_MOCK_CLUSTER_FAIL"] = "1"

        def boom(_v, _i):
            raise finalize.FinalizeError("cluster down")

        plan = snippet.assess_snippet_cleanup(501, volid, [], boom)
        self.assertFalse(plan.safe_to_delete)


if __name__ == "__main__":
    unittest.main()
