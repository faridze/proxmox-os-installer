import importlib.util
import json
import os
import sys
import subprocess
import tarfile
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("update_engine", BASE / "lib/update_engine.py")
update_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = update_engine
spec.loader.exec_module(update_engine)


def _make_package(root: Path, version: str = "9.9.9-test") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "catalog").mkdir(exist_ok=True)
    shutil_copy = __import__("shutil").copy
    shutil_copy(BASE / "catalog/os-images.tsv", root / "catalog/os-images.tsv")
    (root / "bin").mkdir(exist_ok=True)
    shutil_copy(BASE / "bin/proxmox-os-installer", root / "bin/proxmox-os-installer")
    (root / "lib").mkdir(exist_ok=True)
    for name in (
        "image_catalog.py",
        "preflight.py",
        "cache_ops.py",
        "ssh_access.py",
        "update_engine.py",
        "common.sh",
        "interactive.sh",
        "install_aliases.sh",
        "snippets_guidance.sh",
    ):
        shutil_copy(BASE / f"lib/{name}", root / f"lib/{name}")
    (root / "install.sh").write_bytes((BASE / "install.sh").read_bytes())


def _tar_bytes(tree: Path) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in tree.rglob("*"):
            if path.is_file():
                tf.add(path, arcname=f"pkg-root/{path.relative_to(tree)}")
    return buf.getvalue()


class UpdateEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pveosi-test.")
        self.install_root = Path(self.tmp) / "install"
        self.install_root.mkdir()
        self.paths = update_engine.InstallPaths(
            install_root=self.install_root,
            metadata_path=self.install_root / "install-metadata.json",
            entry_link=Path(self.tmp) / "sbin/proxmox-os-installer",
            alias_bindir=Path(self.tmp) / "sbin",
        )
        self.paths.alias_bindir.mkdir(parents=True)
        os.environ["PVEOSI_INSTALL_ROOT"] = str(self.install_root)
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "update.lock")
        os.environ["PVEOSI_UPDATE_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_INSTALL_ALLOW_NONROOT"] = "1"
        os.environ["PVEOSI_INSTALL_ENTRY"] = str(self.paths.entry_link)
        os.environ["PVEOSI_ALIAS_BINDIR"] = str(self.paths.alias_bindir)

    def tearDown(self):
        os.environ.pop("PVEOSI_INSTALL_ROOT", None)
        os.environ.pop("PVEOSI_INSTALL_ALLOW_NONROOT", None)
        __import__("shutil").rmtree(self.tmp, ignore_errors=True)

    def _write_meta(self, **kwargs):
        meta = {
            "version": "0.2.2",
            "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "repository": "https://github.com/faridze/proxmox-os-installer",
            "install_ref": "main",
            "channel": "main",
            "installed_at": "2026-01-01T00:00:00Z",
        }
        meta.update(kwargs)
        update_engine.write_metadata(self.paths, meta)

    def test_pick_ref_unknown_requires_explicit(self):
        with self.assertRaises(update_engine.UpdateError):
            update_engine.pick_update_ref({}, None)

    def test_refuse_implicit_main_from_branch(self):
        meta = {"install_ref": "cursor/safe-provisioning-5dc3", "commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
        with self.assertRaises(update_engine.UpdateError):
            update_engine.refuse_implicit_main_downgrade(meta, "main", "cccccccccccccccccccccccccccccccccccccccc", explicit_ref=None)

    def test_explicit_main_allowed_on_branch(self):
        meta = {"install_ref": "cursor/safe-provisioning-5dc3"}
        update_engine.refuse_implicit_main_downgrade(meta, "main", "cccccccccccccccccccccccccccccccccccccccc", explicit_ref="main")

    def test_safe_extract_rejects_traversal(self):
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"evil"
            info = tarfile.TarInfo(name="root/../escape")
            info.size = len(data)
            tf.addfile(info, BytesIO(data))
        archive = Path(self.tmp) / "bad.tar.gz"
        archive.write_bytes(buf.getvalue())
        with self.assertRaises(update_engine.UpdateError):
            update_engine.safe_extract_tar(archive, Path(self.tmp) / "out")

    def test_network_failure(self):
        self._write_meta()
        target = update_engine.ResolvedTarget(
            ref="main",
            commit="dddddddddddddddddddddddddddddddddddddddd",
            version="0.2.3",
            source=update_engine.DEFAULT_REPO,
        )
        with mock.patch.object(update_engine, "download_tarball", side_effect=update_engine.NetworkError("offline")):
            with self.assertRaises(update_engine.NetworkError):
                update_engine.perform_update(self.paths, target)

    def test_already_up_to_date_no_install(self):
        commit = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        self._write_meta(commit_sha=commit)
        target = update_engine.ResolvedTarget(ref="main", commit=commit, version="0.2.3", source=update_engine.DEFAULT_REPO)
        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg)
        body = _tar_bytes(pkg)

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "resolve_commit", return_value=commit):
                out = update_engine.perform_update(self.paths, target, explicit_ref="main")
        self.assertEqual(out.commit, commit)

    def test_update_installs_and_metadata(self):
        old_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        new_commit = "ffffffffffffffffffffffffffffffffffffffff"
        self._write_meta(commit_sha=old_commit)
        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg, "0.2.3")
        body = _tar_bytes(pkg)

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        def fake_resolve(ref, repo=update_engine.DEFAULT_REPO):
            return new_commit

        target = update_engine.ResolvedTarget(ref="main", commit=new_commit, version="0.2.3", source=update_engine.DEFAULT_REPO)
        lock = Path(self.tmp) / "lock"
        os.environ["PVEOSI_UPDATE_LOCK"] = str(lock)
        os.environ["PVEOSI_INSTALL_ENTRY"] = str(self.paths.entry_link)
        os.environ["PVEOSI_ALIAS_BINDIR"] = str(self.paths.alias_bindir)
        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "resolve_commit", side_effect=fake_resolve):
                update_engine.perform_update(self.paths, target)
        meta = json.loads(self.paths.metadata_path.read_text())
        self.assertEqual(meta["commit_sha"], new_commit)
        self.assertTrue((self.install_root / "lib/update_engine.py").is_file())

    def test_rollback_on_activation_failure(self):
        old_commit = "1111111111111111111111111111111111111111"
        new_commit = "2222222222222222222222222222222222222222"
        self._write_meta(commit_sha=old_commit)
        (self.install_root / "VERSION").write_text("0.2.2\n")
        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg, "0.2.3")
        body = _tar_bytes(pkg)

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        target = update_engine.ResolvedTarget(ref="main", commit=new_commit, version="0.2.3", source=update_engine.DEFAULT_REPO)
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "lock")
        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "refresh_aliases", side_effect=RuntimeError("alias fail")):
                with self.assertRaises(update_engine.UpdateError):
                    update_engine.perform_update(self.paths, target)
        self.assertTrue((self.install_root / "VERSION").read_text().startswith("0.2.2"))

    def test_concurrent_lock(self):
        lock = Path(self.tmp) / "lock"
        os.environ["PVEOSI_UPDATE_LOCK"] = str(lock)
        fd1 = update_engine.acquire_lock(lock)
        with self.assertRaises(update_engine.UpdateError):
            update_engine.acquire_lock(lock)
        os.close(fd1)

    def test_invalid_ref(self):
        with self.assertRaises(update_engine.UpdateError):
            update_engine.validate_ref("../main")

    def test_missing_archive_file(self):
        pkg = Path(self.tmp) / "pkg"
        pkg.mkdir()
        (pkg / "VERSION").write_text("1\n")
        with self.assertRaises(update_engine.UpdateError):
            update_engine.validate_package_tree(pkg)

    def test_corrupt_tar(self):
        bad = Path(self.tmp) / "bad.tar.gz"
        bad.write_bytes(b"not a tar")
        with self.assertRaises((update_engine.UpdateError, tarfile.TarError)):
            update_engine.safe_extract_tar(bad, Path(self.tmp) / "x")

    def test_trusted_repo_rejects_deceptive_url(self):
        with self.assertRaises(update_engine.UpdateError):
            update_engine.trusted_repo_slug(
                {"repository": "https://github.com/evil.com/faridze/proxmox-os-installer"}
            )
        with self.assertRaises(update_engine.UpdateError):
            update_engine.trusted_repo_slug({"repository": "https://github.com/other/evil-repo"})
        with self.assertRaises(update_engine.UpdateError):
            update_engine.trusted_repo_slug({"repository": "http://github.com/faridze/proxmox-os-installer"})
        self.assertEqual(
            update_engine.DEFAULT_REPO,
            update_engine.trusted_repo_slug({"repository": "https://github.com/faridze/proxmox-os-installer/"}),
        )

    def _run_fake_update(self, old_commit: str, new_commit: str, version: str = "0.2.3"):
        pkg = Path(self.tmp) / f"pkg-{new_commit[:8]}"
        _make_package(pkg, version)
        body = _tar_bytes(pkg)

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        def fake_resolve(ref, repo=update_engine.DEFAULT_REPO):
            return new_commit

        target = update_engine.ResolvedTarget(
            ref="main", commit=new_commit, version=version, source=update_engine.DEFAULT_REPO
        )
        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "resolve_commit", side_effect=fake_resolve):
                update_engine.perform_update(self.paths, target, explicit_ref="main")
        return target

    def test_consecutive_updates_retain_previous_generation(self):
        c1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        c2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        c3 = "cccccccccccccccccccccccccccccccccccccccc"
        self._write_meta(commit_sha=c1)
        (self.install_root / "VERSION").write_text("1.0.0\n")
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "lock")
        os.environ["PVEOSI_INSTALL_ENTRY"] = str(self.paths.entry_link)
        os.environ["PVEOSI_ALIAS_BINDIR"] = str(self.paths.alias_bindir)

        prev = update_engine.previous_path(self.paths)
        self._run_fake_update(c1, c2, "2.0.0")
        self.assertTrue(prev.exists())
        self.assertEqual((prev / "VERSION").read_text().strip(), "1.0.0")

        self._run_fake_update(c2, c3, "3.0.0")
        self.assertTrue(prev.exists())
        self.assertEqual((prev / "VERSION").read_text().strip(), "2.0.0")
        self.assertEqual(
            json.loads(self.paths.metadata_path.read_text())["commit_sha"],
            c3,
        )

    def test_stale_update_aborts_if_install_changed(self):
        old = "1111111111111111111111111111111111111111"
        new = "2222222222222222222222222222222222222222"
        other = "3333333333333333333333333333333333333333"
        self._write_meta(commit_sha=old)
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "lock")

        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg, "9.9.9")
        body = _tar_bytes(pkg)
        target = update_engine.ResolvedTarget(ref="main", commit=new, version="9.9.9", source=update_engine.DEFAULT_REPO)

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        original_stage = update_engine.stage_install

        def stage_mutate(*args, **kwargs):
            update_engine.write_metadata(
                self.paths,
                {
                    "version": "x",
                    "commit_sha": other,
                    "repository": update_engine.DEFAULT_REPO_HTTPS,
                    "install_ref": "main",
                    "channel": "main",
                    "installed_at": "2026-01-01T00:00:00Z",
                },
            )
            return original_stage(*args, **kwargs)

        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "stage_install", side_effect=stage_mutate):
                with self.assertRaises(update_engine.UpdateError) as ctx:
                    update_engine.perform_update(self.paths, target, explicit_ref="main")
        self.assertIn("changed during update", str(ctx.exception).lower())

    def test_concurrent_updates_second_fails_fast(self):
        c1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        c2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self._write_meta(commit_sha=c1)
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "lock")
        os.environ["PVEOSI_INSTALL_ENTRY"] = str(self.paths.entry_link)
        os.environ["PVEOSI_ALIAS_BINDIR"] = str(self.paths.alias_bindir)
        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg, "2.0.0")
        body = _tar_bytes(pkg)
        holder_started = threading.Event()
        release_holder = threading.Event()
        errors: list[Exception] = []

        def slow_download(c, dest, repo=update_engine.DEFAULT_REPO):
            holder_started.set()
            release_holder.wait(timeout=10)
            dest.write_bytes(body)

        target = update_engine.ResolvedTarget(
            ref="main", commit=c2, version="2.0.0", source=update_engine.DEFAULT_REPO
        )

        def holder():
            try:
                with mock.patch.object(update_engine, "download_tarball", side_effect=slow_download):
                    update_engine.perform_update(self.paths, target, explicit_ref="main")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=holder)
        t1.start()
        self.assertTrue(holder_started.wait(timeout=5))
        try:
            update_engine.perform_update(self.paths, target, explicit_ref="main")
        except update_engine.UpdateError as exc:
            errors.append(exc)
        release_holder.set()
        t1.join(timeout=15)
        in_progress = [e for e in errors if "already in progress" in str(e).lower()]
        self.assertEqual(len(in_progress), 1)

    def test_metadata_write_failure_restores_backup(self):
        old = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        new = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self._write_meta(commit_sha=old)
        (self.install_root / "VERSION").write_text("1.0.0\n")
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "lock")
        os.environ["PVEOSI_INSTALL_ENTRY"] = str(self.paths.entry_link)
        os.environ["PVEOSI_ALIAS_BINDIR"] = str(self.paths.alias_bindir)
        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg, "2.0.0")
        body = _tar_bytes(pkg)
        target = update_engine.ResolvedTarget(ref="main", commit=new, version="2.0.0", source=update_engine.DEFAULT_REPO)

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "write_metadata", side_effect=OSError("disk full")):
                with self.assertRaises(update_engine.UpdateError):
                    update_engine.perform_update(self.paths, target, explicit_ref="main")
        self.assertEqual((self.install_root / "VERSION").read_text().strip(), "1.0.0")
        meta = json.loads(self.paths.metadata_path.read_text())
        self.assertEqual(meta["commit_sha"], old)

    def test_staging_failure_leaves_live_install(self):
        self._write_meta()
        os.environ["PVEOSI_UPDATE_LOCK"] = str(Path(self.tmp) / "lock")
        pkg = Path(self.tmp) / "pkg"
        _make_package(pkg)
        body = _tar_bytes(pkg)
        target = update_engine.ResolvedTarget(
            ref="main",
            commit="dddddddddddddddddddddddddddddddddddddddd",
            version="9.9.9",
            source=update_engine.DEFAULT_REPO,
        )

        def fake_download(c, dest, repo=update_engine.DEFAULT_REPO):
            dest.write_bytes(body)

        with mock.patch.object(update_engine, "download_tarball", side_effect=fake_download):
            with mock.patch.object(update_engine, "stage_install", side_effect=subprocess.CalledProcessError(1, "bash")):
                with self.assertRaises(update_engine.UpdateError):
                    update_engine.perform_update(self.paths, target, explicit_ref="main")
        self.assertFalse(update_engine.staging_path(self.paths).exists())


if __name__ == "__main__":
    unittest.main()
