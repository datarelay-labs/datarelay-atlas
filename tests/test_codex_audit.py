"""Codex audit provider and worktree identity contract regressions."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.codex_audit import (
    CODEX_DISABLED_FEATURES,
    CodexAuditProvider,
    build_codex_audit_command,
    build_codex_audit_prompt,
    collect_audit_evidence_bundle,
    collect_ci_evidence,
    collect_pr_review_evidence,
    collect_test_evidence,
    collect_work_packet_evidence,
)
from atlas.provenance import ValidationError
from atlas.work_controller import (
    CompletionEvent,
    WorkstreamRecord,
    WorktreeIdentity,
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
        def fake_git(argv: list[str], cwd: str) -> str:
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

    def _identity(self, worktree: str) -> WorktreeIdentity:
        return WorktreeIdentity(
            worktree_path=worktree,
            repository="datarelay-labs/datarelay-atlas",
            branch="feature/x",
            head=HEAD,
            toplevel=worktree,
        )

    def _bundle(self) -> dict:
        return {
            "schema": "awc.codex_evidence_bundle.v1",
            "git": {"status": " M atlas/codex_audit.py", "head": HEAD},
            "work_packet": {"status": "OK", "body": "STATUS=ACTIVE"},
            "tests": {"status": "PASS", "exit_code": 0},
            "ci": {"status": "OK", "checks": "affected-tests\tpass"},
        }

    def test_build_command_disables_tools_apps_browser_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = build_codex_audit_command(
                tmp, last_message_path=str(Path(tmp) / "out.txt")
            )
        self.assertEqual(cmd[0:2], ["codex", "exec"])
        self.assertIn("-C", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertIn('web_search="disabled"', cmd)
        for feature in CODEX_DISABLED_FEATURES:
            self.assertIn(feature, cmd)
        self.assertIn("shell_tool", cmd)
        self.assertIn("browser_use", cmd)
        self.assertIn("apps", cmd)
        self.assertIn("plugins", cmd)
        self.assertIn("hooks", cmd)
        self.assertIn("multi_agent", cmd)
        self.assertNotIn("skill_search", cmd)
        self.assertNotIn("skill_mcp_dependency_install", cmd)
        self.assertEqual(cmd[-1], "-")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_prompt_is_judge_bundle_only_and_credential_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_codex_audit_prompt(
                self._event(),
                self._record(tmp),
                identity=self._identity(tmp),
                evidence_bundle=self._bundle(),
            )
        self.assertIn("judge_evidence_bundle_only", prompt)
        self.assertIn("EVIDENCE_BUNDLE_JSON", prompt)
        self.assertIn("tools_disabled", prompt)
        self.assertIn("no_shell", prompt)
        self.assertIn("/work-resume", prompt)
        self.assertNotIn("OPENAI_API_KEY", prompt)
        self.assertNotIn("sk-", prompt)

    def test_secret_detection_allows_env_var_name_mention(self):
        from atlas.codex_audit import _looks_like_secret

        docs_mention = (
            "Optional: set OPENAI_API_KEY when using --audit-adapter openai. "
            "Default Codex path needs no API key."
        )
        self.assertFalse(_looks_like_secret(docs_mention))
        # Build trigger strings dynamically so committed sources do not embed
        # credential-shaped literals into future evidence-bundle diffs.
        assigned = "OPENAI_API_KEY" + "=" + "redacted-value"
        sk_shaped = "sk-" + ("a" * 24)
        self.assertTrue(_looks_like_secret(assigned))
        self.assertTrue(_looks_like_secret("token " + sk_shaped))


    def test_collect_bundle_uses_injected_collectors(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_git(argv: list[str], cwd: str) -> str:
                mapping = {
                    ("git", "status", "--short", "--branch"): "## feature/x",
                    ("git", "rev-parse", "HEAD"): HEAD,
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "diff", "--stat", "origin/main...HEAD"): "1 file",
                    ("git", "diff", "--find-renames", "origin/main...HEAD"): "diff",
                    ("git", "diff", "--stat", "HEAD"): "workdir",
                    ("git", "diff", "--find-renames", "HEAD"): "workdir-diff",
                    ("git", "diff", "--cached", "--stat"): "",
                    ("git", "diff", "--cached", "--find-renames"): "",
                }
                return mapping[tuple(argv)]

            def fake_cmd(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["gh", "issue", "view"]:
                    payload = {
                        "number": 12,
                        "title": "[AI Work] x",
                        "state": "OPEN",
                        "updatedAt": "2026-09-21T00:00:00Z",
                        "body": "STATUS=ACTIVE\n",
                    }
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(payload), stderr=""
                    )
                if argv[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
                if argv[:3] == ["python3", "-m", "unittest"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="Ran 1 test\nOK\n", stderr=""
                    )
                raise AssertionError(f"unexpected argv {argv}")

            bundle = collect_audit_evidence_bundle(
                self._event(),
                self._record(tmp),
                identity=self._identity(tmp),
                git_runner=fake_git,
                command_runner=fake_cmd,
            )
            self.assertEqual(bundle["schema"], "awc.codex_evidence_bundle.v1")
            self.assertEqual(bundle["git"]["head"], HEAD)
            self.assertEqual(bundle["work_packet"]["status"], "OK")
            self.assertEqual(bundle["tests"]["status"], "PASS")
            self.assertEqual(bundle["ci"]["status"], "ABSENT")
            self.assertEqual(bundle["pr_reviews"]["status"], "ABSENT")

    def test_provider_parses_rework_and_records_tool_disabled_command(self):
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
                self.assertIn("--ignore-user-config", command)
                self.assertIn("shell_tool", command)
                self.assertIn("browser_use", command)
                self.assertIn("apps", command)
                self.assertIn("plugins", command)
                self.assertIn("hooks", command)
                self.assertIn("judge_evidence_bundle_only", prompt)
                self.assertIn("EVIDENCE_BUNDLE_JSON", prompt)
                out = Path(command[command.index("-o") + 1])
                out.write_text(
                    '{"verdict":"REWORK","findings":"bundle shows gaps"}',
                    encoding="utf-8",
                )
                return out.read_text(encoding="utf-8")

            provider = CodexAuditProvider(
                runner=fake_runner,
                git_runner=fake_git,
                evidence_bundle=self._bundle(),
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("bundle shows gaps", result.findings)
            assert provider.last_command is not None
            self.assertIn("--disable", provider.last_command)
            self.assertIn("shell_tool", provider.last_command)
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
                evidence_bundle=self._bundle(),
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
                evidence_bundle=self._bundle(),
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("parse failed", result.findings)

    def test_default_runner_refuses_enabled_shell_tool(self):
        from atlas.codex_audit import default_codex_runner

        with self.assertRaises(ValidationError):
            default_codex_runner(
                ["codex", "exec", "-C", ".", "-s", "read-only", "-o", "x", "-"],
                "prompt",
                ".",
            )

    def test_default_runner_requires_ignore_user_config(self):
        from atlas.codex_audit import default_codex_runner

        with self.assertRaises(ValidationError) as ctx:
            default_codex_runner(
                [
                    "codex",
                    "exec",
                    "-C",
                    ".",
                    "-s",
                    "read-only",
                    "--disable",
                    "shell_tool",
                    "--disable",
                    "apps",
                    "--disable",
                    "browser_use",
                    "-o",
                    "x",
                    "-",
                ],
                "prompt",
                ".",
            )
        self.assertIn("ignore-user-config", str(ctx.exception))

    def test_work_packet_and_ci_collectors_surface_errors(self):
        def boom(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="nope")

        packet = collect_work_packet_evidence(
            repository="datarelay-labs/datarelay-atlas",
            issue_number=12,
            command_runner=boom,
        )
        self.assertEqual(packet["status"], "ERROR")
        ci = collect_ci_evidence(
            repository="datarelay-labs/datarelay-atlas",
            head=HEAD,
            command_runner=boom,
        )
        self.assertEqual(ci["status"], "ERROR")
        tests = collect_test_evidence("/tmp", command_runner=boom)
        self.assertEqual(tests["status"], "FAIL")

    def test_run_capture_maps_missing_executable(self):
        from atlas.codex_audit import _run_capture

        completed = _run_capture(
            ["gh-definitely-missing-awc", "issue", "view", "1"],
            ".",
            timeout_sec=5,
        )
        self.assertEqual(completed.returncode, 127)
        self.assertIn("not found", completed.stderr)

    def test_ci_collector_maps_pending_exit_code(self):
        calls: list[list[str]] = []

        def fake(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:3] == ["gh", "pr", "list"]:
                # Documented raw SHA search (not hash:<SHA>).
                self.assertIn(HEAD, argv)
                self.assertTrue(all(not str(part).startswith("hash:") for part in argv))
                payload = [
                    {
                        "number": 15,
                        "url": "https://example.invalid/pr/15",
                        "state": "OPEN",
                        "headRefOid": HEAD,
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(
                    argv, 8, stdout="check\tpending\n", stderr=""
                )
            raise AssertionError(argv)

        ci = collect_ci_evidence(
            repository="datarelay-labs/datarelay-atlas",
            head=HEAD,
            command_runner=fake,
        )
        self.assertEqual(ci["status"], "PENDING")
        self.assertEqual(ci["checks_exit_code"], 8)

    def test_ci_collector_uses_raw_sha_search(self):
        seen: list[list[str]] = []

        def fake(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

        ci = collect_ci_evidence(
            repository="datarelay-labs/datarelay-atlas",
            head=HEAD,
            command_runner=fake,
        )
        self.assertEqual(ci["status"], "ABSENT")
        self.assertEqual(seen[0][seen[0].index("--search") + 1], HEAD)
        self.assertNotIn(f"hash:{HEAD[:12]}", seen[0])

    def test_pr_review_evidence_ok_and_fail_closed(self):
        def ok_runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["gh", "api"] and argv[2].endswith("/reviews"):
                payload = [
                    {
                        "id": 1,
                        "user": {"login": "reviewer"},
                        "state": "COMMENTED",
                        "body": "P1 collect reviews",
                        "commit_id": HEAD,
                        "submitted_at": "2026-09-21T00:00:00Z",
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            if argv[:2] == ["gh", "api"] and argv[2].endswith("/comments"):
                payload = [
                    {
                        "id": 99,
                        "user": {"login": "reviewer"},
                        "body": "actionable finding",
                        "path": "atlas/codex_audit.py",
                        "line": 10,
                        "commit_id": HEAD,
                        "created_at": "2026-09-21T00:00:00Z",
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            raise AssertionError(argv)

        ok = collect_pr_review_evidence(
            repository="datarelay-labs/datarelay-atlas",
            pr_number=15,
            command_runner=ok_runner,
        )
        self.assertEqual(ok["status"], "OK")
        self.assertEqual(ok["reviews"][0]["body"], "P1 collect reviews")
        self.assertEqual(ok["inline_comments"][0]["id"], 99)

    def test_bounded_review_items_preserve_original_commit_and_line(self):
        from atlas.codex_audit import _bounded_review_items

        remapped = HEAD
        original = "2a502eec7f267814fed3de46da7db88b474446b1"
        items = _bounded_review_items(
            [
                {
                    "id": 4064109451,
                    "user": {"login": "chatgpt-codex-connector[bot]"},
                    "body": "stale mapped comment",
                    "path": "docs/runbooks/autonomous-work-controller-poc.md",
                    "line": 141,
                    "original_line": 138,
                    "commit_id": remapped,
                    "original_commit_id": original,
                    "created_at": "2026-09-21T16:18:48Z",
                    "updated_at": "2026-09-21T16:45:00Z",
                }
            ],
            max_chars=4000,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["commit_id"], remapped)
        self.assertEqual(items[0]["original_commit_id"], original)
        self.assertEqual(items[0]["line"], 141)
        self.assertEqual(items[0]["original_line"], 138)
        self.assertEqual(items[0]["updated_at"], "2026-09-21T16:45:00Z")

        def boom(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied")

        err = collect_pr_review_evidence(
            repository="datarelay-labs/datarelay-atlas",
            pr_number=15,
            command_runner=boom,
        )
        self.assertEqual(err["status"], "ERROR")

    def test_bundle_includes_pr_reviews_when_ci_finds_pr(self):
        def fake_git(argv: list[str], cwd: str) -> str:
            mapping = {
                ("git", "status", "--short", "--branch"): "## feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "remote", "get-url", "origin"): "datarelay-labs/datarelay-atlas",
                ("git", "diff", "--stat", "origin/main...HEAD"): "",
                ("git", "diff", "--find-renames", "origin/main...HEAD"): "",
                ("git", "diff", "--stat", "HEAD"): "",
                ("git", "diff", "--find-renames", "HEAD"): "",
                ("git", "diff", "--cached", "--stat"): "",
                ("git", "diff", "--cached", "--find-renames"): "",
            }
            return mapping[tuple(argv)]

        def fake_cmd(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": 12,
                            "title": "[AI Work] x",
                            "state": "OPEN",
                            "updatedAt": "2026-09-21T00:00:00Z",
                            "body": "STATUS=ACTIVE\n",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "pr", "list"]:
                self.assertEqual(argv[argv.index("--search") + 1], HEAD)
                payload = [
                    {
                        "number": 15,
                        "url": "https://example.invalid/pr/15",
                        "state": "OPEN",
                        "title": "x",
                        "headRefOid": HEAD,
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="check\tpass\n", stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/reviews" in argv[2]:
                return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
            if argv[:2] == ["gh", "api"] and argv[2].endswith("/comments"):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "id": 1,
                                "user": {"login": "bot"},
                                "body": "P1 finding",
                                "path": "atlas/codex_audit.py",
                                "line": 1,
                                "commit_id": HEAD,
                            }
                        ]
                    ),
                    stderr="",
                )
            if argv[:3] == ["python3", "-m", "unittest"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="OK\n", stderr=""
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as tmp:
            bundle = collect_audit_evidence_bundle(
                self._event(),
                self._record(tmp),
                identity=self._identity(tmp),
                git_runner=fake_git,
                command_runner=fake_cmd,
            )
        self.assertEqual(bundle["ci"]["status"], "OK")
        self.assertEqual(bundle["pr_reviews"]["status"], "OK")
        self.assertEqual(bundle["pr_reviews"]["inline_comments"][0]["body"], "P1 finding")

    def test_bundle_fail_closed_when_review_api_errors(self):
        def fake_git(argv: list[str], cwd: str) -> str:
            mapping = {
                ("git", "status", "--short", "--branch"): "## feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "remote", "get-url", "origin"): "datarelay-labs/datarelay-atlas",
                ("git", "diff", "--stat", "origin/main...HEAD"): "",
                ("git", "diff", "--find-renames", "origin/main...HEAD"): "",
                ("git", "diff", "--stat", "HEAD"): "",
                ("git", "diff", "--find-renames", "HEAD"): "",
                ("git", "diff", "--cached", "--stat"): "",
                ("git", "diff", "--cached", "--find-renames"): "",
            }
            return mapping[tuple(argv)]

        def fake_cmd(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": 12,
                            "title": "x",
                            "state": "OPEN",
                            "updatedAt": "2026-09-21T00:00:00Z",
                            "body": "STATUS=ACTIVE\n",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "pr", "list"]:
                payload = [
                    {
                        "number": 15,
                        "url": "https://example.invalid/pr/15",
                        "state": "OPEN",
                        "title": "x",
                        "headRefOid": HEAD,
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
            if argv[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="api failed"
                )
            if argv[:3] == ["python3", "-m", "unittest"]:
                return subprocess.CompletedProcess(argv, 0, stdout="OK\n", stderr="")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as tmp:
            bundle = collect_audit_evidence_bundle(
                self._event(),
                self._record(tmp),
                identity=self._identity(tmp),
                git_runner=fake_git,
                command_runner=fake_cmd,
            )
        self.assertEqual(bundle["ci"]["status"], "OK")
        self.assertEqual(bundle["pr_reviews"]["status"], "ERROR")

    def test_provider_skips_codex_when_pr_reviews_error(self):
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

            def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
                raise AssertionError("codex runner must not be called")

            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD},
                    "pr_reviews": {
                        "status": "ERROR",
                        "detail": "api failed",
                    },
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("PR review evidence status=ERROR", result.findings)
            self.assertIsNone(provider.last_command)
            self.assertIsNone(provider.last_prompt)


if __name__ == "__main__":
    unittest.main()
