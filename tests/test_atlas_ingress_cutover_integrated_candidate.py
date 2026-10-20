"""Exact-candidate manifest and rollback identity for source-only QA.

All filesystem effects use a disposable local directory. No deployment,
production access, service management, or privilege escalation.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import atlas_ingress_cutover_transaction as release
from scripts.atlas_release_manifest import build_manifest

REPOSITORY = Path(__file__).resolve().parents[1]
FROZEN_CANDIDATE = "826c26604c4b47b6aab45a889a15b9552985b5ef"
FROZEN_COUNT = 439
FROZEN_MANIFEST_DIGEST = "379c0d19151ec803b72ca1c7fe3477f3573609ca93a3307a18799d1e4f75c5ba"
BASELINE = "9177c976a591143b045ebdeabdbccd0d48b87c6c"


class IntegratedCandidateContractTests(unittest.TestCase):
    def test_candidate_is_pinned_and_rollback_identity_is_preserved(self):
        self.assertEqual(release.HEAD, FROZEN_CANDIDATE)
        self.assertEqual(release.TRACKED_FILE_COUNT, FROZEN_COUNT)
        self.assertEqual(release.MANIFEST_SHA256, FROZEN_MANIFEST_DIGEST)
        self.assertIn(FROZEN_CANDIDATE[:7], str(release.SOURCE))
        self.assertIn(FROZEN_CANDIDATE[:7], str(release.STAGE))
        self.assertNotEqual(release.SOURCE, release.LIVE)
        self.assertNotEqual(release.STAGE, release.LIVE)
        self.assertNotEqual(release.SOURCE, release.CODE_BACKUP)
        self.assertIn(BASELINE[:8], str(release.DATA_BACKUP))
        self.assertIn(BASELINE[:8], str(release.ENV_BACKUP))
        self.assertIn(FROZEN_CANDIDATE[:7], str(release.FRESH_BACKUP))
        self.assertNotEqual(release.FRESH_BACKUP, release.DATA_BACKUP)

    def test_actual_frozen_git_manifest_matches_pinned_digest(self):
        data, count = build_manifest(REPOSITORY, FROZEN_CANDIDATE)
        self.assertEqual(count, FROZEN_COUNT)
        self.assertEqual(hashlib.sha256(data).hexdigest(), FROZEN_MANIFEST_DIGEST)

    def test_wrong_candidate_is_refused_before_operations(self):
        with mock.patch.object(release, "SystemOperations") as factory:
            self.assertEqual(release.main(["--execute", "--expected-head", BASELINE]), 2)
            factory.assert_not_called()

    def test_integrated_release_manifest_protects_actual_tracked_bytes(self):
        data, count = build_manifest(REPOSITORY, FROZEN_CANDIDATE)
        self.assertEqual(count, FROZEN_COUNT)
        archived = subprocess.check_output(
            ["git", "archive", "--format=tar", FROZEN_CANDIDATE], cwd=REPOSITORY
        )
        with tempfile.TemporaryDirectory(prefix="atlas-qa-integrated-") as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            candidate = root / "candidate"
            candidate.mkdir(mode=0o755)
            with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as archive:
                for item in archive:
                    rel = Path(item.name)
                    if (
                        rel.is_absolute()
                        or ".." in rel.parts
                        or not (item.isfile() or item.isdir())
                    ):
                        self.fail("invalid archive member in trusted release fixture")
                    target = candidate / rel
                    if item.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(item) as source:
                        target.write_bytes(source.read())
            executable = candidate / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"disposable-test-interpreter")
            manifest = root / "manifest.sha256"
            manifest.write_bytes(data)
            with mock.patch.multiple(
                release, MANIFEST=manifest, MANIFEST_SHA256=FROZEN_MANIFEST_DIGEST
            ):
                release._manifest(candidate)
                altered = candidate / "README.md"
                previous = altered.read_bytes()
                altered.write_bytes(previous + b"tamper")
                with self.assertRaisesRegex(release.CutoverError, "COMMAND_FAILED:sha256sum"):
                    release._manifest(candidate)
                altered.write_bytes(previous)
                extra = candidate / "unexpected.txt"
                extra.write_text("unexpected")
                with self.assertRaisesRegex(release.CutoverError, "UNTRACKED_RELEASE_BYTES_PRESENT"):
                    release._manifest(candidate)
                extra.unlink()
                executable.unlink()
                with self.assertRaisesRegex(release.CutoverError, "CANDIDATE_INTERPRETER_MISSING"):
                    release._manifest(candidate)


if __name__ == "__main__":
    unittest.main()
