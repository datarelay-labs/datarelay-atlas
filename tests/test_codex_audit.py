"""Codex audit provider and worktree identity contract regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas.codex_audit import (
    CodexAuditProvider,
    build_codex_audit_command,
    build_codex_audit_prompt,
)
from atlas.provenance import ValidationError
from atlas.work_controller import (
    CompletionEvent,
    WorkstreamRecord,
    heads_match,
    normalize_github_repository,
    validate_worktree_identity,
)


HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class WorktreeIdentityTests(unittest.TestCase):
    def test_normalize_github_repository_variants(self):
        self.assertEqual(
            normalize_github_repository("datarelay-labs/datarelay-atlas"),
            "datarelay-labs/datarelay-atlas",
        )
        self.assertEqual(
            normalize_github_repository(
                "https://github.com/datarelay-labs/datarelay-atlas.git"
            ),
            "datarelay-labs/datarelay-atlas",
        )
        self.assertEqual(
            normalize_github_repository(
                "git@github.com:datarelay-labs/datarelay-atlas.git"
            ),
            "datarelay-labs/datarelay-atlas",
        )

    def test_heads_match_prefix(self):
        self.assertTrue(heads_match(HEAD, HEAD[:12]))
        self.assertFalse(heads_match(HEAD, HEAD2))

    def test_validate_worktree_identity_success_and_failures(self):
        calls: list[tuple[tuple[str, ...], str]] = []

        def fake_git(argv: list[str], cwd: str) -> str:
            calls.append((tuple(argv), cwd))
            key = tuple(argv)
            mapping = {
                ("git", "rev-parse", "--show-toplevel"): cwd,
                ("git", "remote", "get-url", "origin"): (
                    "https://github.com/datarelay-labs/datarelay-atlas.git"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
            }
            if key not in mapping:
                raise ValidationError(f"unexpected git argv: {argv}")
            return mapping[key]

        with tempfile.TemporaryDirectory() as tmp:
            identity = validate_worktree_identity(
                tmp,
                repository="datarelay-labs/datarelay-atlas",
                branch="feature/x",
                expected_head=HEAD,
                git_runner=fake_git,
            )
            self.assertEqual(identity.repository, "datarelay-labs/datarelay-atlas")
            self.assertEqual(identity.branch, "feature/x")
            self.assertEqual(identity.head, HEAD)

            with self.assertRaises(ValidationError):
                validate_worktree_identity(
                    tmp,
                    repository="datarelay-labs/datarelay-atlas",
                    branch="other",
                    expected_head=HEAD,
                    git_runner=fake_git,
                )

            def detached_git(argv: list[str], cwd: str) -> str:
                if tuple(argv) == ("git", "branch", "--show-current"):
                    return ""
                return fake_git(argv, cwd)

            with self.assertRaises(ValidationError):
                validate_worktree_identity(
                    tmp,
                    repository="datarelay-labs/datarelay-atlas",
                    branch="feature/x",
                    expected_head=HEAD,
                    git_runner=detached_git,
                )


class CodexAuditProviderTests(unittest.TestCase):
    def _event(self) -> CompletionEvent:
        return CompletionEvent(
            event_id="evt-1",
            workstream="awc-poc",
            issue_number=12,
            branch="feature/x",
            head=HEAD,
            attempt=1,
        )

    def _record(self, worktree: str) -> WorkstreamRecord:
        return WorkstreamRecord(
            workstream="awc-poc",
            repository="datarelay-labs/datarelay-atlas",
            issue_number=12,
            branch="feature/x",
            worktree_path=worktree,
            expected_head=HEAD,
            state="AUDITING",
            attempt=0,
            max_attempts=3,
        )

    def test_build_command_is_read_only_and_targets_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = build_codex_audit_command(
                tmp, last_message_path=str(Path(tmp) / "out.txt")
            )
        self.assertEqual(cmd[0:2], ["codex", "exec"])
        self.assertIn("-C", cmd)
        self.assertIn("-s", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("--ephemeral", cmd)
        self.assertIn("-o", cmd)
        self.assertEqual(cmd[-1], "-")
        self.assertNotIn("OPENAI_API_KEY", " ".join(cmd))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_prompt_is_bounded_and_credential_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            from atlas.work_controller import WorktreeIdentity

            identity = WorktreeIdentity(
                worktree_path=tmp,
                repository="datarelay-labs/datarelay-atlas",
                branch="feature/x",
                head=HEAD,
                toplevel=tmp,
            )
            prompt = build_codex_audit_prompt(
                self._event(), self._record(tmp), identity=identity
            )
        self.assertIn("/work-resume", prompt)
        self.assertIn("read_only", prompt)
        self.assertIn("no_edits", prompt)
        self.assertNotIn("OPENAI_API_KEY", prompt)
        self.assertNotIn("sk-", prompt)

    def test_provider_parses_rework_and_records_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            observed: dict = {}

            def fake_git(argv: list[str], cwd: str) -> str:
                mapping = {
                    ("git", "rev-parse", "--show-toplevel"): cwd,
                    ("git", "remote", "get-url", "origin"): (
                        "git@github.com:datarelay-labs/datarelay-atlas.git"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): HEAD,
                }
                return mapping[tuple(argv)]

            def fake_runner(command: list[str], prompt: str, cwd: str) -> str:
                observed["command"] = command
                observed["prompt"] = prompt
                observed["cwd"] = cwd
                self.assertEqual(cwd, str(Path(tmp).resolve()))
                self.assertIn("read-only", command)
                out = Path(command[command.index("-o") + 1])
                out.write_text(
                    '{"verdict":"REWORK","findings":"/resume drift; non-autonomous dispatch"}',
                    encoding="utf-8",
                )
                return out.read_text(encoding="utf-8")

            provider = CodexAuditProvider(
                runner=fake_runner,
                git_runner=fake_git,
                evidence="synthetic fixture evidence",
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("/resume drift", result.findings)
            self.assertIsNotNone(provider.last_command)
            assert provider.last_command is not None
            self.assertEqual(provider.last_command[0:2], ["codex", "exec"])
            self.assertIn("read-only", provider.last_command)
            self.assertNotIn("OPENAI_API_KEY", observed["prompt"])

    def test_provider_maps_runner_failure_to_human_required(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_git(argv: list[str], cwd: str) -> str:
                mapping = {
                    ("git", "rev-parse", "--show-toplevel"): cwd,
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): HEAD,
                }
                return mapping[tuple(argv)]

            def boom(command: list[str], prompt: str, cwd: str) -> str:
                raise ValidationError("codex exec failed (exit 1): boom")

            provider = CodexAuditProvider(
                runner=boom,
                git_runner=fake_git,
                evidence="synthetic fixture evidence",
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("codex audit failed", result.findings)

    def test_provider_maps_malformed_output_to_human_required(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_git(argv: list[str], cwd: str) -> str:
                mapping = {
                    ("git", "rev-parse", "--show-toplevel"): cwd,
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): HEAD,
                }
                return mapping[tuple(argv)]

            def bad_runner(command: list[str], prompt: str, cwd: str) -> str:
                return "not-json and no verdict"

            provider = CodexAuditProvider(
                runner=bad_runner,
                git_runner=fake_git,
                evidence="synthetic fixture evidence",
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("parse failed", result.findings)

    def test_default_runner_refuses_non_readonly_command(self):
        from atlas.codex_audit import default_codex_runner

        with self.assertRaises(ValidationError):
            default_codex_runner(
                ["codex", "exec", "-C", ".", "-s", "workspace-write", "-o", "x", "-"],
                "prompt",
                ".",
            )


if __name__ == "__main__":
    unittest.main()
