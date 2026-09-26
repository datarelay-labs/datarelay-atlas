"""Quiesced data-root backup and restore-test regressions. No production mutation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from atlas.cli import main
from atlas.data_lock import data_root_write_lock
from atlas.data_protection import backup_data_root, restore_test
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.registry import ProjectRegistry
from atlas.service import AtlasService

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_" + ("a" * 20)


def _sample_root(root: Path) -> None:
    service = AtlasService(root)
    service.register_project(
        project_id="alpha",
        repository="datarelay-labs/alpha",
        display_name="Alpha",
    )
    service.add_source(
        "alpha",
        source_id="charter",
        source_path="docs/charter.md",
        title="Charter",
    )

    def fetch(source, token):  # noqa: ARG001
        return FetchedSource(content="alpha body", source_revision="rev-alpha")

    service.sync_project("alpha", fetch=fetch)


class DataProtectionTests(unittest.TestCase):
    def test_backup_and_restore_round_trip_representative_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            before = (root / "registry.json").read_bytes()
            report = backup_data_root(root, base / "snapshot")
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["project_count"], 1)
            self.assertGreaterEqual(report["file_count"], 3)
            self.assertEqual((root / "registry.json").read_bytes(), before)
            snapshot = base / "snapshot"
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((snapshot / "registry.json").stat().st_mode),
                0o600,
            )
            restored = restore_test(snapshot, base / "proof")
            self.assertEqual(restored["project_count"], 1)
            self.assertEqual((root / "registry.json").read_bytes(), before)
            proof = ProjectRegistry(base / "proof")
            self.assertEqual(proof.get("alpha").repository, "datarelay-labs/alpha")
            document = (base / "proof" / "projections" / "alpha" / "charter.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("alpha body", document)
            self.assertIn("rev-alpha", document)

    def test_cli_backup_includes_controller_state_and_skips_chat_audit_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            repo = base / "repo"
            repo.mkdir()
            head = _git_worktree(repo)
            stdout = StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(
                    main(
                        [
                            "--data-root",
                            str(root),
                            "project",
                            "register",
                            "alpha",
                            "--repository",
                            "datarelay-labs/alpha",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "--data-root",
                            str(root),
                            "work-controller",
                            "register",
                            "alpha-ws",
                            "--repository",
                            "datarelay-labs/alpha",
                            "--issue-number",
                            "43",
                            "--branch",
                            "main",
                            "--worktree",
                            str(repo),
                            "--expected-head",
                            head,
                        ]
                    ),
                    0,
                )
            event = {
                "event_id": "evt-1",
                "workstream": "alpha-ws",
                "issue_number": 43,
                "branch": "main",
                "head": head,
                "attempt": 1,
            }
            event_path = base / "event.json"
            event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            with patch("sys.stdout", StringIO()):
                self.assertEqual(
                    main(
                        [
                            "--data-root",
                            str(root),
                            "work-controller",
                            "enqueue-completion",
                            str(event_path),
                        ]
                    ),
                    0,
                )
            (root / "chat-audit.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
            (root / "chat-audit.lock").write_text("", encoding="utf-8")
            handoff = root / "chat-audit-handoffs"
            handoff.mkdir()
            (handoff / "finding.json").write_text("{}\n", encoding="utf-8")
            (root / "completion-processed").mkdir()
            (root / "completion-processed" / "old.json").write_text(
                json.dumps(event) + "\n",
                encoding="utf-8",
            )
            backup_data_root(root, base / "snapshot")
            proof = base / "proof"
            restore_test(base / "snapshot", proof)
            self.assertIn("alpha-ws", (proof / "work-controller.json").read_text(encoding="utf-8"))
            self.assertTrue((proof / "completion-inbox" / "evt-1.json").is_file())
            self.assertTrue((proof / "completion-processed" / "old.json").is_file())
            self.assertFalse((proof / "chat-audit.json").exists())
            self.assertFalse((proof / "chat-audit-handoffs").exists())
            (root / "notes.txt").write_text("nope\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unexpected entries"):
                backup_data_root(root, base / "rejected")

    def test_controller_lock_blocks_inbox_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            attempt = base / "attempt"
            done = base / "done"
            event = json.dumps(
                {
                    "event_id": "evt-lock",
                    "workstream": "alpha-ws",
                    "issue_number": 43,
                    "branch": "main",
                    "head": "abc1234",
                    "attempt": 1,
                }
            )
            script = textwrap.dedent(
                """
                import json, sys
                from pathlib import Path
                from atlas.work_controller import enqueue_completion_event
                root, attempt, done, raw = sys.argv[1:]
                Path(attempt).write_text("1", encoding="utf-8")
                enqueue_completion_event(Path(root), json.loads(raw))
                Path(done).write_text("1", encoding="utf-8")
                """
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            with data_root_write_lock(root):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script, str(root), str(attempt), str(done), event],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.time() + 5
                while not attempt.exists():
                    if time.time() > deadline:
                        proc.kill()
                        self.fail("enqueue did not reach the lock")
                    time.sleep(0.01)
                time.sleep(0.2)
                self.assertFalse(done.exists())
                self.assertFalse((root / "completion-inbox" / "evt-lock.json").exists())
            _stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertTrue((root / "completion-inbox" / "evt-lock.json").is_file())

    def test_controller_schema_and_completion_bytes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            (root / "work-controller.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "work-controller.json is corrupt"):
                backup_data_root(root, base / "corrupt")
            (root / "work-controller.json").write_text(
                '{"schema_version": 2, "workstreams": {}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "work-controller schema is unsupported"):
                backup_data_root(root, base / "schema")
            (root / "work-controller.json").write_text(
                '{"schema_version": 1, "workstreams": {}}\n',
                encoding="utf-8",
            )
            inbox = root / "completion-inbox"
            inbox.mkdir()
            (inbox / "evt-1.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "completion event is unsupported"):
                backup_data_root(root, base / "event")

    def test_cooperating_writer_blocks_until_lock_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            ProjectRegistry(root).register(
                project_id="alpha",
                repository="datarelay-labs/alpha",
            )
            attempt = base / "attempt"
            done = base / "done"
            script = textwrap.dedent(
                """
                import sys
                from pathlib import Path
                from atlas.registry import ProjectRegistry
                root, attempt, done = (Path(arg) for arg in sys.argv[1:])
                attempt.write_text("1", encoding="utf-8")
                ProjectRegistry(root).register(
                    project_id="beta",
                    repository="datarelay-labs/beta",
                )
                done.write_text("1", encoding="utf-8")
                """
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            with data_root_write_lock(root):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script, str(root), str(attempt), str(done)],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.time() + 5
                while not attempt.exists():
                    if time.time() > deadline:
                        proc.kill()
                        self.fail("writer did not reach the lock")
                    time.sleep(0.01)
                time.sleep(0.2)
                self.assertFalse(done.exists())
                self.assertNotIn("beta", (root / "registry.json").read_text(encoding="utf-8"))
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("beta", (root / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(stdout, "")

    def test_backup_rejects_corrupt_secret_partial_and_unsupported_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            (root / "registry.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "registry.json is corrupt"):
                backup_data_root(root, base / "bad-json")

            (root / "registry.json").write_text(
                '{"schema_version": 2, "projects": {}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "registry schema is unsupported"):
                backup_data_root(root, base / "bad-schema")

            (root / "registry.json").unlink()
            _sample_root(root)
            (root / "notes.txt").write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unexpected entries"):
                backup_data_root(root, base / "extra")
            (root / "notes.txt").unlink()

            (root / "registry.json.tmp").write_text("partial\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "incomplete write marker"):
                backup_data_root(root, base / "tmp")
            (root / "registry.json.tmp").unlink()

            link = root / "linked.json"
            link.symlink_to(root / "registry.json")
            os.replace(link, root / "registry.json")
            with self.assertRaisesRegex(ValidationError, "not a regular file"):
                backup_data_root(root, base / "link")

    def test_backup_rejects_secret_like_projection_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            document = root / "projections" / "alpha" / "charter.md"
            body = document.read_text(encoding="utf-8") + TOKEN + "\n"
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            document.write_text(body, encoding="utf-8")
            meta_path = root / "projections" / "projections.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["projections"]["alpha/charter"]["content_digest"] = digest
            meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "looks secret"):
                backup_data_root(root, base / "secret")

    def test_restore_test_rejects_tampered_and_partial_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            snapshot = base / "snapshot"
            backup_data_root(root, snapshot)
            registry = snapshot / "registry.json"
            registry.write_bytes(registry.read_bytes() + b" ")
            with self.assertRaisesRegex(ValidationError, "digest mismatch"):
                restore_test(snapshot, base / "torn")
            self.assertFalse((base / "torn").exists())

            raw = b"{"
            registry.write_bytes(raw)
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["files"]:
                if entry["path"] == "registry.json":
                    entry["bytes"] = len(raw)
                    entry["sha256"] = hashlib.sha256(raw).hexdigest()
            (snapshot / "manifest.json").write_text(
                json.dumps(manifest) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "corrupt"):
                restore_test(snapshot, base / "corrupt")

            backup_data_root(root, base / "clean")
            clean = base / "clean"
            (clean / "projections" / "extra.md").write_text("x\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unexpected files"):
                restore_test(clean, base / "extra")
            (clean / "projections" / "extra.md").unlink()
            (clean / "projections" / "alpha" / "charter.md").unlink()
            with self.assertRaisesRegex(ValidationError, "missing"):
                restore_test(clean, base / "missing")

            manifest = json.loads((clean / "manifest.json").read_text(encoding="utf-8"))
            manifest["backup_schema_version"] = 2
            (clean / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                restore_test(clean, base / "schema")

    def test_commands_refuse_overlapping_and_existing_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            with self.assertRaisesRegex(ValidationError, "outside the data root"):
                backup_data_root(root, root / "nested")
            snapshot = base / "snapshot"
            backup_data_root(root, snapshot)
            with self.assertRaisesRegex(ValidationError, "already exists"):
                backup_data_root(root, snapshot)
            with self.assertRaisesRegex(ValidationError, "outside the backup"):
                restore_test(snapshot, snapshot / "nested")

    def test_cli_prints_counts_without_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            stdout = StringIO()
            with patch("sys.stdout", stdout):
                code = main(
                    [
                        "ops",
                        "backup",
                        "--data-root",
                        str(root),
                        "--dest",
                        str(base / "snapshot"),
                    ]
                )
            self.assertEqual(code, 0)
            printed = stdout.getvalue()
            self.assertNotIn("alpha body", printed)
            self.assertIn('"status": "ok"', printed)
            stdout.seek(0)
            stdout.truncate(0)
            with patch("sys.stdout", stdout):
                code = main(
                    [
                        "ops",
                        "restore-test",
                        "--backup",
                        str(base / "snapshot"),
                        "--dest",
                        str(base / "proof"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotIn("alpha body", stdout.getvalue())
            self.assertIn('"project_count": 1', stdout.getvalue())

    def test_projection_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            document = root / "projections" / "alpha" / "charter.md"
            document.write_text(document.read_text(encoding="utf-8") + "torn\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "digest mismatch"):
                backup_data_root(root, base / "snapshot")

    def test_symlink_lock_is_refused_without_changing_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            outside = base / "outside-secret"
            outside.write_text("keep\n", encoding="utf-8")
            os.chmod(outside, 0o644)
            before = stat.S_IMODE(outside.stat().st_mode)
            (root / ".write.lock").symlink_to(outside)
            with self.assertRaisesRegex(ValidationError, "not a regular file"):
                backup_data_root(root, base / "snapshot")
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before)
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((base / "snapshot").exists())

    def test_backup_fsyncs_tree_directories_and_parent_after_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            dest = base / "snapshot"
            events: list[tuple] = []
            real_fsync = os.fsync
            real_rename = os.rename

            def tracing_fsync(fd: int) -> None:
                events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
                real_fsync(fd)

            def tracing_rename(src, dst):
                events.append(("rename", os.fspath(src), os.fspath(dst)))
                return real_rename(src, dst)

            with (
                patch("atlas.data_protection.os.fsync", tracing_fsync),
                patch("atlas.data_protection.os.rename", tracing_rename),
            ):
                backup_data_root(root, dest)
            rename_at = next(index for index, event in enumerate(events) if event[0] == "rename")
            before = [Path(event[1]) for event in events[:rename_at] if event[0] == "fsync"]
            after = [Path(event[1]) for event in events[rename_at + 1 :] if event[0] == "fsync"]
            partial = Path(str(dest) + ".partial")
            self.assertTrue(any(path == partial.resolve() for path in before))
            self.assertTrue(any("projections" in path.parts for path in before))
            self.assertTrue(any(path == dest.resolve() for path in after))
            self.assertTrue(any(path == dest.parent.resolve() for path in after))
            self.assertLess(
                next(index for index, path in enumerate(after) if path == dest.resolve()),
                next(index for index, path in enumerate(after) if path == dest.parent.resolve()),
            )


def _git_worktree(path: Path) -> str:
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = "Atlas"
    env["GIT_AUTHOR_EMAIL"] = "atlas@example.com"
    env["GIT_COMMITTER_NAME"] = "Atlas"
    env["GIT_COMMITTER_EMAIL"] = "atlas@example.com"
    subprocess.check_call(["git", "init", "-b", "main"], cwd=path, env=env)
    subprocess.check_call(
        ["git", "remote", "add", "origin", "https://github.com/datarelay-labs/alpha.git"],
        cwd=path,
        env=env,
    )
    (path / "README").write_text("x\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "README"], cwd=path, env=env)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=path, env=env)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        env=env,
        text=True,
    ).strip()


if __name__ == "__main__":
    unittest.main()
