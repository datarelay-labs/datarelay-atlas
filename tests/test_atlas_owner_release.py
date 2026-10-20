"""Safe owner-release bundle negative and disposable filesystem tests."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import atlas_owner_release as release


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OwnerReleaseTests(unittest.TestCase):
    def test_relative_path_rejects_traversal(self):
        for bad in ("", "/etc/shadow", "../outside", "docs/../secret",
                    "./foo", "foo\\bar", "wheels//evil.whl"):
            with self.subTest(path=bad), self.assertRaises(release.ReleaseRefused):
                release._safe_relative(bad)

    def test_read_only_bundle_integrity_and_wheel_tamper(self):
        with tempfile.TemporaryDirectory(prefix="atlas-owner-bundle-") as tmp:
            root = Path(tmp)
            names = {
                "app.tar": b"application",
                "tracked.sha256": b"manifest",
                "operator.py": b"operator",
                "deploy.py": b"runner",
                "README.md": b"guide",
                "requirements.lock": b"lock",
                "uv": b"binary",
            }
            for i in range(29):
                names[f"wheels/example{i}-1.0-py3-none-any.whl"] = b"wheel" + bytes([i])
            for name, content in names.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (root / "bundle.json").write_text(json.dumps({
                "schema": 1,
                "app_head": release.APP_HEAD,
                "files": {name: digest(content) for name, content in names.items()}
            }))
            with mock.patch.multiple(
                release, APP_SHA256=digest(b"application"),
                MANIFEST_SHA256=digest(b"manifest"),
                OPERATOR_SHA256=digest(b"operator"),
            ):
                release.inspect_bundle(root)
                wheel = root / "wheels/example4-1.0-py3-none-any.whl"
                wheel.write_text("tampered")
                with self.assertRaisesRegex(release.ReleaseRefused, "RELEASE_HASH_MISMATCH"):
                    release.inspect_bundle(root)
                wheel.write_bytes(names["wheels/example4-1.0-py3-none-any.whl"])
                (root / "extra").write_text("unexpected")
                with self.assertRaisesRegex(release.ReleaseRefused, "RELEASE_FILE_INVENTORY_MISMATCH"):
                    release.inspect_bundle(root)

    def test_build_refuses_untrusted_root_without_changes(self):
        with tempfile.TemporaryDirectory(prefix="atlas-owner-root-") as tmp:
            with mock.patch.object(release.os, "geteuid", return_value=0), \
                 mock.patch.object(release.socket, "gethostname", return_value="prod-atlas"):
                with self.assertRaisesRegex(release.ReleaseRefused, "BUNDLE_MUST_BE_ROOT_OWNED_PROTECTED"):
                    release._require_trusted_operator(Path(tmp))

    def test_wrong_host_is_never_permitted(self):
        with mock.patch.object(release.os, "geteuid", return_value=0), \
             mock.patch.object(release.socket, "gethostname", return_value="dev-atlas"):
            with self.assertRaisesRegex(release.ReleaseRefused, "WRONG_HOST"):
                release._require_trusted_operator(Path("/root"))

    def test_prepare_reports_pinned_source_count_not_stale_label(self):
        from contextlib import redirect_stdout
        from io import StringIO
        with tempfile.TemporaryDirectory(prefix="atlas-owner-label-") as temp:
            destination = Path(temp) / "source"
            op = SimpleNamespace(SOURCE=destination, _manifest=mock.Mock(),
                                 main=mock.Mock(return_value=0))
            output = StringIO()
            def create_build(_archive, target):
                target.mkdir()
            with mock.patch.object(release, "_require_baseline"), \
                 mock.patch.object(release, "_save_manifest"), \
                 mock.patch.object(release, "_extract_exact_source",
                                   side_effect=create_build), \
                 mock.patch.object(release, "_create_offline_runtime"), \
                 redirect_stdout(output):
                self.assertEqual(release.prepare(op), 0)
            self.assertTrue(destination.is_dir())
            self.assertIn(
                f"SOURCE_{release.EXPECTED_TRACKED}_VERIFIED", output.getvalue()
            )
            self.assertNotIn("SOURCE_431_VERIFIED", output.getvalue())
            op.main.assert_called_once_with(
                ["--prepare", "--expected-head", release.APP_HEAD]
            )

    def test_prepare_refuses_invalid_baseline_before_any_mutation(self):
        op = SimpleNamespace()
        with mock.patch.object(release, "_require_baseline",
                               side_effect=release.ReleaseRefused("MISSING_ROLLBACK")), \
             mock.patch.object(release, "_save_manifest") as save, \
             mock.patch.object(release, "_extract_exact_source") as extract:
            with self.assertRaisesRegex(release.ReleaseRefused, "MISSING_ROLLBACK"):
                release.prepare(op)
        save.assert_not_called()
        extract.assert_not_called()

    def test_execute_requires_interactive_console(self):
        op = SimpleNamespace()
        with mock.patch.object(release.sys.stdin, "isatty", return_value=False), \
             mock.patch.object(release, "_require_staged") as stage:
            with self.assertRaisesRegex(release.ReleaseRefused, "INTERACTIVE_TERMINAL_REQUIRED"):
                release.deploy(op)
        stage.assert_not_called()

    def test_declined_operator_confirmation_never_executes(self):
        op = SimpleNamespace(main=mock.Mock())
        with mock.patch.object(release.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(release, "_require_staged"), \
             mock.patch("builtins.input", return_value="no"):
            with self.assertRaisesRegex(release.ReleaseRefused, "DID_NOT_AUTHORIZE"):
                release.deploy(op)
        op.main.assert_not_called()

    def test_frozen_tar_extraction_never_allows_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="atlas-owner-extract-") as tmp:
            root = Path(tmp)
            archive = root / "in.tar"
            with tarfile.open(archive, mode="w") as dest:
                item = tarfile.TarInfo("atlas/secret")
                item.type = tarfile.SYMTYPE
                item.linkname = "/etc/shadow"
                dest.addfile(item)
            with self.assertRaisesRegex(release.ReleaseRefused, "NONREGULAR_SOURCE_ARCHIVE"):
                release._extract_exact_source(archive, root / "dest")
            self.assertFalse((root / "dest/atlas/secret").exists())

    def test_source_root_is_world_traversable_even_with_0077_umask(self):
        import os
        import stat
        with tempfile.TemporaryDirectory(prefix="atlas-release-mode-") as tmp:
            root = Path(tmp)
            archive = root / "code.tar"
            with tarfile.open(archive, mode="w") as target:
                data = b"test-data"
                item = tarfile.TarInfo("README.txt")
                item.size = len(data)
                target.addfile(item, io.BytesIO(data))
            previous = os.umask(0o077)
            try:
                with mock.patch.object(release, "EXPECTED_TRACKED", 1):
                    release._extract_exact_source(archive, root / "source")
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE((root / "source").stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((root / "source/README.txt").stat().st_mode), 0o644)

    def test_uv_environment_uses_root_home_only_under_root(self):
        import subprocess
        with mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
             mock.patch.object(release.os, "geteuid", return_value=1000), \
             mock.patch.dict(release.os.environ, {"HOME": "/tmp/qa-user-home"}):
            release._execute_fixed(["/tmp/uv", "--version"])
            self.assertEqual(run.call_args.kwargs["env"]["HOME"], "/tmp/qa-user-home")
        with mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
             mock.patch.object(release.os, "geteuid", return_value=0):
            release._execute_fixed(["/tmp/uv", "--version"])
            self.assertEqual(run.call_args.kwargs["env"]["HOME"], "/root")

    def test_default_entrypoint_only_inspects(self):
        with mock.patch.object(release, "inspect_bundle") as inspect, \
             mock.patch.object(release, "_require_trusted_operator") as privilege:
            self.assertEqual(release.main([]), 0)
            inspect.assert_called_once()
            privilege.assert_not_called()


if __name__ == "__main__":
    unittest.main()
