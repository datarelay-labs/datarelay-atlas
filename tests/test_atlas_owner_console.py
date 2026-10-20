"""Disposable owner-console regression; never runs an operational action."""
from __future__ import annotations

from contextlib import nullcontext, redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import atlas_owner_console as console


class OwnerConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-console-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = {n: n.encode() for n in ("app.tar", "tracked.sha256", "operator.py", "deploy.py", "uv", "requirements.lock", "README.md")}
        self.files.update({f"wheels/test{i}-1-py3-none-any.whl": b"wheel" for i in range(29)})
        self.head = "a" * 40
        self.files["bundle.json"] = json.dumps({"schema": 1, "app_head": self.head, "files": {n: console.digest(b) for n, b in self.files.items()}}).encode()
        self.archive = self.root / "bundle.tar.gz"
        self.write_archive()
        self.sha = console.digest(self.archive.read_bytes())
        self.bundle = console.inspect_archive(self.archive, self.sha)
        self.sink = io.StringIO()
        self.addCleanup(self.sink.close)

    def write_archive(self, extra=None, files=None):
        with tarfile.open(self.archive, "w:gz") as out:
            d = tarfile.TarInfo("wheels/")
            d.type = tarfile.DIRTYPE
            out.addfile(d)
            for name, content in (self.files if files is None else files).items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                out.addfile(entry, io.BytesIO(content))
            if extra is not None:
                out.addfile(extra, io.BytesIO(b""))

    def inspected(self):
        return console.inspect_archive(self.archive, console.digest(self.archive.read_bytes()))

    def good_facts(self):
        return {
            "/commits/main": {"sha": self.head},
            "/issues/356": {"state": "closed", "body": "SAFETY_FREEZE=OFF\n\nClosed incident."},
            "/rules/branches/main": [
                {"type": name} for name in ("pull_request", "non_fast_forward", "deletion")
            ] + [{"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": n, "integration_id": 15368} for n in console.REQUIRED_CHECKS],
            }}],
        }

    def test_real_archive_inspection_is_read_only(self):
        self.assertEqual(self.bundle.app_head, self.head)
        self.assertEqual(set(self.bundle.files), set(self.files))
        self.assertEqual(list(self.root.iterdir()), [self.archive])

    def test_outer_digest_tamper_rejected(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"tamper")
        with self.assertRaisesRegex(console.Refused, "ARCHIVE_SHA256_MISMATCH"):
            console.inspect_archive(self.archive, self.sha)

    def test_digest_syntax_rejected(self):
        with self.assertRaisesRegex(console.Refused, "SHA256_INVALID"):
            console.inspect_archive(self.archive, "bad")

    def test_symlink_archive_rejected(self):
        link = self.root / "alias.tar.gz"
        link.symlink_to(self.archive)
        with self.assertRaises(OSError):
            console.inspect_archive(link, self.sha)

    def test_path_traversal_rejected_before_unpack(self):
        for name in ("../escape", "/etc/shadow", "./x", "wheels//x", "wheels/../x", "wheels\\x", "evil\nx"):
            with self.subTest(name=name):
                self.write_archive(tarfile.TarInfo(name))
                with self.assertRaisesRegex(console.Refused, "UNSAFE_ARCHIVE_PATH"):
                    self.inspected()
        self.assertFalse((self.root.parent / "escape").exists())

    def test_links_devices_and_fifo_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            entry = tarfile.TarInfo("bad-entry")
            entry.type = kind
            entry.linkname = "/etc/shadow"
            with self.subTest(kind=kind):
                self.write_archive(entry)
                with self.assertRaisesRegex(console.Refused, "ARCHIVE_LINK_OR_SPECIAL_FILE"):
                    self.inspected()

    def test_duplicate_entry_rejected(self):
        self.write_archive(tarfile.TarInfo("deploy.py"))
        with self.assertRaisesRegex(console.Refused, "DUPLICATE_ARCHIVE_ENTRY"):
            self.inspected()

    def test_unexpected_directory_rejected(self):
        entry = tarfile.TarInfo("unrelated/")
        entry.type = tarfile.DIRTYPE
        self.write_archive(entry)
        with self.assertRaisesRegex(console.Refused, "UNEXPECTED_ARCHIVE_DIRECTORY"):
            self.inspected()

    def test_inner_tamper_rejected_even_with_new_outer_checksum(self):
        bad = dict(self.files)
        bad["operator.py"] = b"modified"
        self.write_archive(files=bad)
        with self.assertRaisesRegex(console.Refused, "BUNDLE_MEMBER_SHA256_MISMATCH"):
            self.inspected()

    def test_manifest_extra_missing_or_wrong_head_rejected(self):
        for change in ("missing", "extra", "head"):
            bad = dict(self.files)
            if change == "missing":
                del bad["app.tar"]
            elif change == "extra":
                bad["extra.txt"] = b"unexpected"
            else:
                manifest = json.loads(bad["bundle.json"])
                manifest["app_head"] = "not-a-commit"
                bad["bundle.json"] = json.dumps(manifest).encode()
            with self.subTest(change=change):
                self.write_archive(files=bad)
                with self.assertRaises(console.Refused):
                    self.inspected()

    def test_unpacked_inventory_and_retry_do_not_overwrite(self):
        target = self.root / "unpacked"
        with mock.patch.object(console, "trusted_path"), redirect_stdout(self.sink):
            console.unpack(target, self.bundle)
            before = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
            console.unpack(target, self.bundle)
            self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in target.iterdir()})
            console.verify_installed(target, self.bundle)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(target / "uv").st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(target / "deploy.py").st_mode & 0o777, 0o600)

    def test_existing_changed_files_are_not_replaced(self):
        target = self.root / "changed"
        with mock.patch.object(console, "trusted_path"), redirect_stdout(self.sink):
            console.unpack(target, self.bundle)
            (target / "deploy.py").write_bytes(b"keep this change")
            with self.assertRaisesRegex(console.Refused, "INSTALLED_FILE_CHANGED"):
                console.unpack(target, self.bundle)
        self.assertEqual((target / "deploy.py").read_bytes(), b"keep this change")

    def test_partial_directory_does_not_get_reused(self):
        target = self.root / "partial"
        target.mkdir()
        with mock.patch.object(console, "trusted_path"):
            with self.assertRaisesRegex(console.Refused, "INSTALLED_FILE_INVENTORY_MISMATCH"):
                console.unpack(target, self.bundle)
        self.assertFalse(list(target.iterdir()))

    def test_installed_symlink_rejected(self):
        target = self.root / "links"
        with mock.patch.object(console, "trusted_path"), redirect_stdout(self.sink):
            console.unpack(target, self.bundle)
            (target / "uv").unlink()
            (target / "uv").symlink_to("/usr/bin/true")
            with self.assertRaisesRegex(console.Refused, "INSTALLED_SYMLINK"):
                console.verify_installed(target, self.bundle)

    def test_host_and_root_requirements(self):
        with mock.patch.object(console.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(console.Refused, "ROOT_OPERATOR_REQUIRED"):
                console.require_operator()
        with mock.patch.object(console.os, "geteuid", return_value=0), mock.patch.object(console.socket, "gethostname", return_value="dev-atlas"):
            with self.assertRaisesRegex(console.Refused, "WRONG_HOST"):
                console.require_operator()

    def test_untrusted_path_rejected(self):
        source = self.root / "source.py"
        source.write_text("pass\n")
        source.chmod(0o666)
        with self.assertRaisesRegex(console.Refused, "ROOT_OWNED_PROTECTED_PATH_REQUIRED"):
            console.trusted_path(source, directory=False)

    def test_no_root_or_subprocess_on_default_inspect(self):
        with mock.patch.object(console, "require_operator") as root, mock.patch.object(console, "run_phase") as phase, redirect_stdout(self.sink):
            self.assertEqual(console.main(["--archive", str(self.archive), "--sha256", self.sha]), 0)
        root.assert_not_called()
        phase.assert_not_called()

    def test_release_policy_accepts_only_matching_independent_technical_facts(self):
        self.assertEqual(console.technical_release_blockers(self.bundle, self.good_facts().__getitem__), [])

    def test_main_head_mismatch_is_not_tree_equivalence(self):
        facts = self.good_facts()
        facts["/commits/main"]["sha"] = "b" * 40
        self.assertTrue(any("MAIN_HEAD_MISMATCH" in e for e in console.technical_release_blockers(self.bundle, facts.__getitem__)))

    def test_open_incident_or_freeze_blocks(self):
        for data in ({"state": "open", "body": "SAFETY_FREEZE=OFF"}, {"state": "closed", "body": "SAFETY_FREEZE=ON\n\nDo not apply."}):
            facts = self.good_facts()
            facts["/issues/356"] = data
            self.assertIn("INCIDENT_356_NOT_RELEASED", console.technical_release_blockers(self.bundle, facts.__getitem__))

    def test_missing_rules_missing_context_wrong_app_and_non_strict_block(self):
        for mode in ("none", "missing", "wrong-app", "non-strict"):
            facts = self.good_facts()
            policy = facts["/rules/branches/main"][-1]["parameters"]
            if mode == "none":
                facts["/rules/branches/main"] = []
            elif mode == "missing":
                policy["required_status_checks"].pop()
            elif mode == "wrong-app":
                policy["required_status_checks"][0]["integration_id"] = 999
            else:
                policy["strict_required_status_checks_policy"] = False
            with self.subTest(mode=mode):
                self.assertTrue(console.technical_release_blockers(self.bundle, facts.__getitem__))

    def test_blocked_release_never_invokes_old_operator(self):
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "release_check", side_effect=console.Refused("FROZEN")), mock.patch.object(console.subprocess, "run") as run:
            with self.assertRaisesRegex(console.Refused, "FROZEN"):
                console.run_phase(self.root, self.bundle, "prepare")
        run.assert_not_called()

    def test_phase_order_and_interactive_requirement(self):
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "release_check"), mock.patch.object(console.subprocess, "run") as run:
            for action, state, message in (("preflight", {}, "PREPARE_MUST"), ("deploy", {"completed": "prepare"}, "PREFLIGHT_MUST"), ("deploy", {"completed": "preflight"}, "INTERACTIVE_TERMINAL")):
                with mock.patch.object(console, "read_state", return_value=state), mock.patch.object(console.sys.stdin, "isatty", return_value=False):
                    with self.subTest(action=action, state=state), self.assertRaisesRegex(console.Refused, message):
                        console.run_phase(self.root, self.bundle, action)
            run.assert_not_called()

    def test_phase_failure_never_records_success(self):
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "release_check"), mock.patch.object(console, "read_state", return_value={}), mock.patch.object(console.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)), mock.patch.object(console, "record_state") as record:
            with self.assertRaisesRegex(console.Refused, "UNDERLYING_PHASE_FAILED:prepare:7"):
                console.run_phase(self.root, self.bundle, "prepare")
            self.assertNotIn(mock.call(self.root, self.bundle, "prepare"), record.call_args_list)

    def test_phase_success_uses_isolated_python_and_no_shell(self):
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "release_check"), mock.patch.object(console, "read_state", return_value={}), mock.patch.object(console.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, mock.patch.object(console, "record_state") as record:
            self.assertEqual(console.run_phase(self.root, self.bundle, "prepare"), 0)
            self.assertEqual(run.call_args.args[0][1:3], ["-I", "-B"])
            self.assertNotIn("shell", run.call_args.kwargs)
            record.assert_any_call(self.root, self.bundle, "prepare")

    def test_receipt_bound_to_archive_digest(self):
        with mock.patch.object(console, "trusted_path"):
            console.record_state(self.root, self.bundle, "prepare")
            try:
                self.assertEqual(console.read_state(self.root, self.bundle)["completed"], "prepare")
                with self.assertRaisesRegex(console.Refused, "PHASE_STATE_IDENTITY_MISMATCH"):
                    console.read_state(self.root, replace(self.bundle, archive_sha256="f" * 64))
            finally:
                console.state_path(self.root).unlink(missing_ok=True)

    def test_concurrent_operator_lock_is_rejected(self):
        path = self.root / "lock"
        with mock.patch.object(console.os, "fstat", return_value=type("Meta", (), {"st_mode": stat_mode(), "st_uid": 0})()):
            with console.operator_lock(path):
                with self.assertRaisesRegex(console.Refused, "ANOTHER_OPERATOR_IS_RUNNING"):
                    with console.operator_lock(path):
                        self.fail("Second lock acquired")

    def test_lock_symlink_is_rejected(self):
        target = self.root / "target"
        target.write_bytes(b"untouched")
        link = self.root / "lock"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            with console.operator_lock(link):
                self.fail("Symlink lock accepted")
        self.assertEqual(target.read_bytes(), b"untouched")


    def test_installed_special_file_and_extra_directory_rejected(self):
        for mode in ("fifo", "directory"):
            target = self.root / mode
            with mock.patch.object(console, "trusted_path"), redirect_stdout(self.sink):
                console.unpack(target, self.bundle)
                if mode == "fifo":
                    os.mkfifo(target / "unexpected")
                else:
                    (target / "unexpected").mkdir()
                with self.subTest(mode=mode), self.assertRaisesRegex(console.Refused, "INSTALLED_UNEXPECTED"):
                    console.verify_installed(target, self.bundle)

    def test_failed_deploy_invalidates_previous_preflight_receipt(self):
        previous = {"completed": "preflight"}
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "release_check"), mock.patch.object(console, "read_state", return_value=previous), mock.patch.object(console.sys.stdin, "isatty", return_value=True), mock.patch.object(console.subprocess, "run", return_value=subprocess.CompletedProcess([], 8)), mock.patch.object(console, "record_state") as record:
            with self.assertRaisesRegex(console.Refused, "UNDERLYING_PHASE_FAILED:deploy:8"):
                console.run_phase(self.root, self.bundle, "deploy")
            record.assert_called_once_with(self.root, self.bundle, "deploy-started")

    def test_failed_preflight_retains_only_prepare_state(self):
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "release_check"), mock.patch.object(console, "read_state", return_value={"completed": "preflight"}), mock.patch.object(console.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), mock.patch.object(console, "record_state") as record:
            with self.assertRaisesRegex(console.Refused, "UNDERLYING_PHASE_FAILED:preflight:1"):
                console.run_phase(self.root, self.bundle, "preflight")
            record.assert_called_once_with(self.root, self.bundle, "prepare")

    def test_failed_release_lookup_does_not_execute_subprocess(self):
        with mock.patch.object(console, "verify_installed"), mock.patch.object(console, "technical_release_blockers", side_effect=console.Refused("GITHUB_RELEASE_FACTS_UNAVAILABLE")), mock.patch.object(console.subprocess, "run") as run:
            with self.assertRaisesRegex(console.Refused, "GITHUB_RELEASE_FACTS_UNAVAILABLE"):
                console.run_phase(self.root, self.bundle, "prepare")
            run.assert_not_called()

    def test_redirect_is_not_followed(self):
        with self.assertRaisesRegex(console.Refused, "GITHUB_REDIRECT_REFUSED"):
            console.NoRedirect().redirect_request(None, None, 302, None, {}, "https://elsewhere.invalid")


def stat_mode():
    import stat
    return stat.S_IFREG | 0o600


if __name__ == "__main__":
    unittest.main()
