"""Codex audit provider and worktree identity contract regressions."""

from __future__ import annotations

import hashlib
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
    collect_git_evidence,
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
    require_clean_porcelain,
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

    def test_require_canonical_target_repo_rejects_clone_urls(self):
        from atlas.work_controller import require_canonical_target_repo

        self.assertEqual(
            require_canonical_target_repo(
                "datarelay-labs/datarelay-atlas",
                "datarelay-labs/datarelay-atlas",
            ),
            "datarelay-labs/datarelay-atlas",
        )
        with self.assertRaises(ValidationError) as ctx:
            require_canonical_target_repo(
                "https://github.com/datarelay-labs/datarelay-atlas.git",
                "datarelay-labs/datarelay-atlas",
            )
        self.assertIn("canonical owner/repo slug", str(ctx.exception))

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

    def test_require_clean_porcelain_forces_untracked_files_all_argv(self):
        """Fake git must see --untracked-files=all; config cannot suppress ??."""
        seen: list[list[str]] = []

        def fake_git(argv: list[str], cwd: str) -> str:
            seen.append(list(argv))
            if argv == ["git", "status", "--porcelain"]:
                return ""  # would hide untracked under showUntrackedFiles=no
            if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
                return "?? hidden_untracked.py\n"
            raise ValidationError(f"unexpected git argv: {argv}")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError) as ctx:
                require_clean_porcelain(tmp, git_runner=fake_git)
        self.assertIn("dirty", str(ctx.exception).lower())
        self.assertIn(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            seen,
        )
        self.assertNotIn(["git", "status", "--porcelain"], seen)

    def test_require_clean_porcelain_detects_untracked_despite_config(self):
        """Real repo with status.showUntrackedFiles=no still fails on untracked."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "awc@example.com"],
                cwd=tmp,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "awc"],
                cwd=tmp,
                check=True,
                capture_output=True,
            )
            Path(tmp, "tracked.txt").write_text("ok\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "tracked.txt"], cwd=tmp, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=tmp,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "status.showUntrackedFiles", "no"],
                cwd=tmp,
                check=True,
                capture_output=True,
            )
            Path(tmp, "secret_untracked.py").write_text("x=1\n", encoding="utf-8")
            # Default porcelain (no --untracked-files=all) appears clean.
            default = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=tmp,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(default.strip(), "")
            with self.assertRaises(ValidationError) as ctx:
                require_clean_porcelain(tmp)
            self.assertIn("dirty", str(ctx.exception).lower())
            self.assertIn("untracked-files=all", str(ctx.exception))


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
        from atlas.work_controller import _looks_like_secret

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
        self.assertTrue(
            _looks_like_secret("GITHUB_TOKEN" + "=" + "ghp_" + ("b" * 20))
        )
        self.assertTrue(
            _looks_like_secret("AWS_SECRET_ACCESS_KEY" + "=" + ("c" * 24))
        )
        self.assertTrue(
            _looks_like_secret("service_token" + "=" + "supersecretvalue123")
        )
        self.assertTrue(_looks_like_secret("access_token: bare-secret-value"))
        self.assertTrue(
            _looks_like_secret('{"client_secret": "json-secret-value"}')
        )
        self.assertTrue(_looks_like_secret('{"password":"correct horse"}'))

    def test_prompt_guard_allows_safe_redacted_placeholders(self):
        from atlas.work_controller import (
            _contains_unsafe_secret,
            _looks_like_secret,
            redact_sensitive_audit_text,
        )

        # Exact sanitizer output must not block the Codex prompt guard.
        safe = "OPENAI_API_KEY=<redacted>\npassword=<redacted>"
        self.assertTrue(_looks_like_secret(safe), safe)
        self.assertFalse(_contains_unsafe_secret(safe), safe)
        quoted_safe = 'PASSWORD="<redacted>"\napiKey=\'<redacted>\''
        self.assertFalse(_contains_unsafe_secret(quoted_safe), quoted_safe)
        bearer_safe = "Authorization: Bearer <redacted>"
        self.assertFalse(_contains_unsafe_secret(bearer_safe), bearer_safe)
        pem_safe = redact_sensitive_audit_text(
            "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
        )
        self.assertIn("<redacted-private-key>", pem_safe)
        self.assertFalse(_contains_unsafe_secret(pem_safe), pem_safe)

        # Live credentials still fail closed.
        live = "OPENAI_API_KEY" + "=" + ("x" * 24)
        self.assertTrue(_contains_unsafe_secret(live), live)
        mixed = safe + "\n" + live
        self.assertTrue(_contains_unsafe_secret(mixed), mixed)
        # Placeholder must terminate the value; prefix spoofing stays unsafe.
        spoofed = "PASSWORD=<redacted>hunter2"
        self.assertTrue(_contains_unsafe_secret(spoofed), spoofed)
        self.assertTrue(_looks_like_secret(spoofed), spoofed)
        backslash_spoof = "PASSWORD=<redacted>\\hunter2"
        self.assertTrue(_contains_unsafe_secret(backslash_spoof), backslash_spoof)
        # Shell juxtaposition after a closed quote must stay unsafe.
        quoted_spoof = 'PASSWORD="<redacted>"hunter2'
        self.assertTrue(_contains_unsafe_secret(quoted_spoof), quoted_spoof)
        single_quoted_spoof = "PASSWORD='<redacted>'hunter2"
        self.assertTrue(
            _contains_unsafe_secret(single_quoted_spoof), single_quoted_spoof
        )
        # Punctuation is not a terminator when a suffix continues the value.
        punct_spoof = 'PASSWORD="<redacted>",hunter2'
        self.assertTrue(_contains_unsafe_secret(punct_spoof), punct_spoof)
        bare_punct_spoof = "PASSWORD=<redacted>,hunter2"
        self.assertTrue(_contains_unsafe_secret(bare_punct_spoof), bare_punct_spoof)
        brace_spoof = 'PASSWORD="<redacted>"}hunter2'
        self.assertTrue(_contains_unsafe_secret(brace_spoof), brace_spoof)
        # Comma + another quoted fragment is shell concatenation, not JSON.
        quoted_suffix = 'PASSWORD="<redacted>","hunter2"'
        self.assertTrue(_contains_unsafe_secret(quoted_suffix), quoted_suffix)
        # JSON next-key shape must not exempt shell `=` assignments.
        shell_json_spoof = 'PASSWORD="<redacted>","hunter2":x'
        self.assertTrue(_contains_unsafe_secret(shell_json_spoof), shell_json_spoof)
        # Bearer placeholder must terminate; a glued suffix stays unsafe.
        bearer_spoof = "Bearer <redacted>hunter2"
        self.assertTrue(_contains_unsafe_secret(bearer_spoof), bearer_spoof)
        self.assertTrue(
            _contains_unsafe_secret("Bearer <redacted>,hunter2"),
            "Bearer <redacted>,hunter2",
        )
        self.assertTrue(
            _contains_unsafe_secret('Bearer <redacted>"hunter2"'),
            'Bearer <redacted>"hunter2"',
        )
        self.assertFalse(
            _contains_unsafe_secret("Bearer <redacted>"), "Bearer <redacted>"
        )
        # Authorization scheme matching is case-insensitive.
        lower_bearer = "authorization: bearer hunter2secretvalue"
        self.assertTrue(_contains_unsafe_secret(lower_bearer), lower_bearer)
        self.assertIn(
            "Bearer <redacted>",
            redact_sensitive_audit_text(lower_bearer),
        )

        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_codex_audit_prompt(
                self._event(),
                self._record(tmp),
                identity=self._identity(tmp),
                evidence_bundle={
                    **self._bundle(),
                    "tests": {
                        "status": "PASS",
                        "detail": safe,
                        "transcript": safe,
                    },
                },
            )
        self.assertIn("<redacted>", prompt)
        self.assertFalse(_contains_unsafe_secret(prompt), prompt[:500])

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
                    ("git", "status", "--short", "--branch"): "## feature/x",
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
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
                    ("git", "status", "--short", "--branch"): "## feature/x",
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
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
                    ("git", "status", "--short", "--branch"): "## feature/x",
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
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
        # Nonzero exit without a unittest "Ran N tests" summary is infrastructure.
        self.assertEqual(tests["status"], "ERROR")

    def test_unittest_collection_errors_are_error_not_fail(self):
        from atlas.codex_audit import classify_unittest_result

        infra = (
            "ERROR: tests.test_missing (unittest.loader._FailedTest)\n"
            "ImportError: Failed to import test module: test_missing\n"
            "ModuleNotFoundError: No module named 'missing_dep'\n"
            "\n"
            "Ran 1 test in 0.001s\n"
            "\n"
            "FAILED (errors=1)\n"
        )
        self.assertEqual(classify_unittest_result(1, infra), "ERROR")

        assertion = (
            "FAIL: test_gap (tests.test_x.X)\n"
            "AssertionError: expected 1\n"
            "\n"
            "Ran 1 test in 0.001s\n"
            "\n"
            "FAILED (failures=1)\n"
        )
        self.assertEqual(classify_unittest_result(1, assertion), "FAIL")
        self.assertEqual(classify_unittest_result(0, "Ran 1 test\nOK\n"), "PASS")

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout=infra, stderr="")

        tests = collect_test_evidence("/tmp", command_runner=runner)
        self.assertEqual(tests["status"], "ERROR")
        self.assertIn("infrastructure", tests.get("detail", ""))

    def test_work_packet_fail_closed_when_body_trimmed(self):
        """Acceptance text only in trimmed Work Packet tail ⇒ INCOMPLETE."""
        marker = "ACCEPTANCE_ONLY_IN_TRIMMED_TAIL"
        long_body = ("y" * 8000) + marker
        self.assertGreater(len(long_body), 8000)

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            payload = {
                "number": 12,
                "title": "[AI Work] x",
                "state": "OPEN",
                "updatedAt": "2026-09-21T00:00:00Z",
                "body": long_body,
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )

        packet = collect_work_packet_evidence(
            repository="datarelay-labs/datarelay-atlas",
            issue_number=12,
            command_runner=runner,
            max_chars=8000,
        )
        self.assertEqual(packet["status"], "INCOMPLETE")
        self.assertTrue(packet["truncated"])
        self.assertIn("truncated under max_chars", packet["detail"])
        self.assertNotIn(marker, packet["body"])
        self.assertIn("...[truncated]...", packet["body"])

    def test_git_evidence_fail_closed_when_diff_trimmed(self):
        """Actionable text only after max_diff_chars ⇒ evidence_status INCOMPLETE."""
        marker = "P1_ACTIONABLE_ONLY_IN_TRIMMED_DIFF_TAIL"
        long_diff = ("z" * 500) + marker
        self.assertGreater(len(long_diff), 500)

        def fake_git(argv: list[str], cwd: str) -> str:
            mapping = {
                ("git", "status", "--short", "--branch"): "## feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "diff", "--stat", "origin/main...HEAD"): "1 file changed",
                ("git", "diff", "--find-renames", "origin/main...HEAD"): long_diff,
                ("git", "diff", "--stat", "HEAD"): "",
                ("git", "diff", "--find-renames", "HEAD"): "",
                ("git", "diff", "--cached", "--stat"): "",
                ("git", "diff", "--cached", "--find-renames"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            evidence = collect_git_evidence(
                tmp, git_runner=fake_git, max_diff_chars=500
            )
        self.assertEqual(evidence["evidence_status"], "INCOMPLETE")
        self.assertIn("diff", evidence["truncated_fields"])
        self.assertNotIn(marker, evidence["diff"])
        self.assertIn("...[truncated]...", evidence["diff"])
        # Porcelain status field remains git status output, not completeness.
        self.assertEqual(evidence["status"], "## feature/x")

    def test_untracked_file_marks_git_incomplete_and_skips_codex(self):
        """?? new_file.py ⇒ INCOMPLETE (names only); Codex runner not invoked."""
        secret_contents = "SECRET_SHOULD_NEVER_APPEAR_IN_EVIDENCE = 1\n"

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "status", "--short", "--branch"): (
                    "## feature/x\n?? new_file.py\n"
                ),
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "diff", "--stat", "origin/main...HEAD"): "",
                ("git", "diff", "--find-renames", "origin/main...HEAD"): "",
                ("git", "diff", "--stat", "HEAD"): "",
                ("git", "diff", "--find-renames", "HEAD"): "",
                ("git", "diff", "--cached", "--stat"): "",
                ("git", "diff", "--cached", "--find-renames"): "",
            }
            try:
                return mapping[tuple(argv)]
            except KeyError as exc:
                raise AssertionError(argv) from exc

        with tempfile.TemporaryDirectory() as tmp:
            # Untracked file exists on disk but must not be read into evidence.
            Path(tmp, "new_file.py").write_text(secret_contents, encoding="utf-8")
            evidence = collect_git_evidence(tmp, git_runner=fake_git)
            self.assertEqual(evidence["evidence_status"], "INCOMPLETE")
            self.assertEqual(evidence["untracked_files"], ["new_file.py"])
            self.assertIn("untracked files present", evidence["detail"])
            self.assertNotIn(secret_contents.strip(), json.dumps(evidence))
            self.assertNotIn("SECRET_SHOULD_NEVER_APPEAR", json.dumps(evidence))

            def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
                raise AssertionError("codex runner must not be called")

            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": evidence,
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("git evidence status=INCOMPLETE", result.findings)
            self.assertIsNone(provider.last_command)
            self.assertIsNone(provider.last_prompt)

    def test_git_status_is_bounded_for_many_untracked_paths(self):
        """Many ?? paths: evidence status stays bounded; INCOMPLETE; no contents."""
        lines = ["## feature/x"]
        for i in range(200):
            lines.append(f"?? generated/path_{i:04d}_" + ("x" * 40) + ".py")
        raw = "\n".join(lines) + "\n"
        self.assertGreater(len(raw), 2000)

        def fake_git(argv: list[str], cwd: str) -> str:
            mapping = {
                ("git", "status", "--short", "--branch"): raw,
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "diff", "--stat", "origin/main...HEAD"): "",
                ("git", "diff", "--find-renames", "origin/main...HEAD"): "",
                ("git", "diff", "--stat", "HEAD"): "",
                ("git", "diff", "--find-renames", "HEAD"): "",
                ("git", "diff", "--cached", "--stat"): "",
                ("git", "diff", "--cached", "--find-renames"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            evidence = collect_git_evidence(
                tmp, git_runner=fake_git, max_status_chars=2000
            )
        self.assertEqual(evidence["evidence_status"], "INCOMPLETE")
        self.assertIn("status", evidence["truncated_fields"])
        self.assertLessEqual(len(evidence["status"]), 2000 + 40)
        self.assertIn("...[truncated]...", evidence["status"])
        self.assertIn("untracked_files", evidence)
        self.assertLess(len(evidence["untracked_files"]), 200)
        # Raw unbounded status must not appear in the evidence payload.
        self.assertNotEqual(evidence["status"], raw.strip())
        self.assertLess(len(json.dumps(evidence)), len(raw) + 5000)

    def test_head_change_after_evidence_blocks_codex(self):
        """HEAD mutation after evidence collection ⇒ HUMAN_REQUIRED, no Codex."""
        status = "## feature/x"
        head_reads = {"n": 0}

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
                head_reads["n"] += 1
                # First read: initial identity. Later reads: post-evidence gate.
                if head_reads["n"] >= 2:
                    return "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                return HEAD
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "status", "--short", "--branch"): status,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {
                        "head": HEAD,
                        "evidence_status": "OK",
                        "status": status,
                        "status_digest": hashlib.sha256(
                            status.encode("utf-8")
                        ).hexdigest(),
                    },
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("clean autonomous snapshot", result.findings)
            self.assertIsNone(provider.last_command)

    def test_head_change_after_codex_pass_rejects_terminal_pass(self):
        """HEAD mutation after Codex returns PASS ⇒ HUMAN_REQUIRED, never PASS."""
        heads = {"value": HEAD}
        status = "## feature/x"

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
                return heads["value"]
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "status", "--short", "--branch"): status,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        def pass_runner(command: list[str], prompt: str, cwd: str) -> str:
            heads["value"] = "cccccccccccccccccccccccccccccccccccccccc"
            out = Path(command[command.index("-o") + 1])
            out.write_text(
                '{"verdict":"PASS","findings":"looks good"}',
                encoding="utf-8",
            )
            return out.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=pass_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {
                        "head": HEAD,
                        "evidence_status": "OK",
                        "status": status,
                    },
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("PASS rejected", result.findings)

    def test_head_change_after_codex_rework_rejects_stale_dispatch(self):
        """HEAD mutation after Codex returns REWORK ⇒ HUMAN_REQUIRED, never REWORK."""
        heads = {"value": HEAD}
        status = "## feature/x"

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            if argv[:2] == ["git", "rev-parse"] and argv[-1] == "HEAD":
                return heads["value"]
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "status", "--short", "--branch"): status,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        def rework_runner(command: list[str], prompt: str, cwd: str) -> str:
            heads["value"] = "dddddddddddddddddddddddddddddddddddddddd"
            out = Path(command[command.index("-o") + 1])
            out.write_text(
                '{"verdict":"REWORK","findings":"fix something"}',
                encoding="utf-8",
            )
            return out.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=rework_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {
                        "head": HEAD,
                        "evidence_status": "OK",
                        "status": status,
                    },
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("REWORK rejected", result.findings)
            self.assertNotEqual(result.verdict, "REWORK")

    def test_dirty_porcelain_before_codex_is_human_required(self):
        """Dirty tracked/untracked state before Codex ⇒ HUMAN_REQUIRED, no Codex."""
        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--short", "--branch"): "## feature/x\n M dirty.py",
                ("git", "status", "--porcelain", "--untracked-files=all"): " M dirty.py\n",
            }
            return mapping[tuple(argv)]

        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "PASS"},
                    "ci": {"status": "ABSENT"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("dirty", result.findings.lower())
            self.assertIsNone(provider.last_command)

    def test_dirty_plus_tests_fail_is_human_required_not_rework(self):
        """Dirty porcelain + tests FAIL ⇒ HUMAN_REQUIRED; never REWORK; no Codex."""

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): " M dirty.py\n",
            }
            return mapping[tuple(argv)]

        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "FAIL", "transcript": "AssertionError"},
                    "ci": {"status": "OK"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertNotEqual(result.verdict, "REWORK")
            self.assertIn("dirty", result.findings.lower())
            self.assertNotIn("tests FAIL", result.findings)
            self.assertIsNone(provider.last_command)

    def test_untracked_dirty_skips_codex_even_when_plain_porcelain_empty(self):
        """Forced --untracked-files=all dirtiness ⇒ HUMAN_REQUIRED; no Codex."""

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            if argv == ["git", "status", "--porcelain"]:
                return ""
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): (
                    "?? sneak.py\n"
                ),
            }
            return mapping[tuple(argv)]

        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "PASS"},
                    "ci": {"status": "ABSENT"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("dirty", result.findings.lower())
            self.assertIsNone(provider.last_command)

    def test_dirty_porcelain_after_codex_rework_rejects_autonomous_rework(self):
        """Dirty tree after Codex REWORK ⇒ HUMAN_REQUIRED, never REWORK."""
        porcelain = {"value": ""}

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--short", "--branch"): "## feature/x",
                ("git", "status", "--porcelain", "--untracked-files=all"): porcelain["value"],
            }
            return mapping[tuple(argv)]

        def rework_runner(command: list[str], prompt: str, cwd: str) -> str:
            porcelain["value"] = "?? surprise.py\n"
            out = Path(command[command.index("-o") + 1])
            out.write_text(
                '{"verdict":"REWORK","findings":"fix something"}',
                encoding="utf-8",
            )
            return out.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=rework_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "PASS"},
                    "ci": {"status": "ABSENT"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("REWORK rejected", result.findings)
            self.assertIn("dirty", result.findings.lower())

    def test_deterministic_tests_fail_returns_rework_without_codex(self):
        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "FAIL", "transcript": "AssertionError"},
                    "ci": {"status": "OK"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("tests FAIL", result.findings)
            self.assertIsNone(provider.last_command)

    def test_deterministic_rework_preserves_verbose_failure_tail(self):
        """A long unittest transcript must keep the trailing failure identity."""
        from atlas.codex_audit import _deterministic_gate_before_codex
        from atlas.work_controller import sanitize_rework_findings

        noise = "test_ok (tests.test_noise.Noise) ... ok\n" * 40
        secret = "OPENAI_API_KEY" + "=" + "live-tail-secret"
        host_path = "/tmp/awc-host-path/file.py"
        transcript = (
            "HEAD_MARKER_start\n"
            + noise
            + "FAIL: test_boundary AssertionError: preserved-failure "
            + f"{secret} {host_path} TAIL_MARKER_9f3a\n"
        )
        self.assertGreater(len(transcript), 300)

        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "FAIL", "transcript": transcript},
                    "ci": {"status": "OK"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("HEAD_MARKER_start", result.findings)
            self.assertIn("TAIL_MARKER_9f3a", result.findings)
            self.assertIn("test_boundary", result.findings)
            self.assertNotIn("live-tail-secret", result.findings)
            self.assertNotIn(host_path, result.findings)
            self.assertIn("<local-path>", result.findings)
            self.assertLessEqual(len(result.findings), 400)
            persisted = sanitize_rework_findings(result.findings)
            self.assertIn("TAIL_MARKER_9f3a", persisted)
            self.assertNotIn("live-tail-secret", persisted)

        error = _deterministic_gate_before_codex(
            {
                "tests": {
                    "status": "ERROR",
                    "detail": ("x" * 400) + "\nIMPORT_TAIL_REASON missing_dep",
                }
            }
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.verdict, "HUMAN_REQUIRED")
        self.assertIn("IMPORT_TAIL_REASON", error.findings)
        self.assertLessEqual(len(error.findings), 400)

        ci_fail = _deterministic_gate_before_codex(
            {
                "ci": {
                    "status": "FAIL",
                    "detail": ("c" * 400) + "\nCHECK_TAIL affected-tests",
                }
            }
        )
        self.assertIsNotNone(ci_fail)
        assert ci_fail is not None
        self.assertEqual(ci_fail.verdict, "REWORK")
        self.assertIn("CHECK_TAIL", ci_fail.findings)
        self.assertLessEqual(len(ci_fail.findings), 400)

    def test_deterministic_gate_redacts_secrets_in_findings(self):
        assigned = "OPENAI_API_KEY" + "=" + "live-secret-value"
        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {
                        "status": "FAIL",
                        "transcript": f"boom\n{assigned}\n",
                    },
                    "ci": {"status": "OK"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("OPENAI_API_KEY=<redacted>", result.findings)
            self.assertNotIn("live-secret-value", result.findings)

    def test_deterministic_gate_redacts_aws_secret_assignments(self):
        aws_secret = "AWS_SECRET_ACCESS_KEY" + "=" + ("y" * 24)
        aws_key_id = "AWS_ACCESS_KEY_ID" + "=" + ("Z" * 20)

        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {
                        "status": "FAIL",
                        "transcript": f"boom\n{aws_secret}\n{aws_key_id}\n",
                    },
                    "ci": {"status": "OK"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("AWS_SECRET_ACCESS_KEY=<redacted>", result.findings)
            self.assertIn("AWS_ACCESS_KEY_ID=<redacted>", result.findings)
            self.assertNotIn("y" * 24, result.findings)
            self.assertNotIn("Z" * 20, result.findings)

    def test_deterministic_tests_error_returns_human_required_without_codex(self):
        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "ERROR", "detail": "unittest timed out"},
                    "ci": {"status": "OK"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertIn("tests ERROR", result.findings)
            self.assertIsNone(provider.last_command)

    def test_deterministic_tests_fail_with_ci_pending_is_human_required(self):
        """CI PENDING/ERROR must beat tests FAIL so REWORK is not dispatched."""
        from atlas.codex_audit import _deterministic_gate_before_codex

        gated = _deterministic_gate_before_codex(
            {
                "tests": {"status": "FAIL", "transcript": "AssertionError"},
                "ci": {"status": "PENDING", "checks": "unit\tpending"},
            }
        )
        self.assertIsNotNone(gated)
        assert gated is not None
        self.assertEqual(gated.verdict, "HUMAN_REQUIRED")
        self.assertIn("CI PENDING", gated.findings)
        self.assertNotIn("tests FAIL", gated.findings)

        gated_error = _deterministic_gate_before_codex(
            {
                "tests": {"status": "FAIL", "transcript": "AssertionError"},
                "ci": {"status": "ERROR", "detail": "gh auth failed"},
            }
        )
        self.assertIsNotNone(gated_error)
        assert gated_error is not None
        self.assertEqual(gated_error.verdict, "HUMAN_REQUIRED")
        self.assertIn("CI ERROR", gated_error.findings)

    def test_deterministic_ci_fail_returns_rework_without_codex(self):
        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "PASS"},
                    "ci": {"status": "FAIL", "checks": "unit\tfail"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "REWORK")
            self.assertIn("CI FAIL", result.findings)
            self.assertIsNone(provider.last_command)

    def test_deterministic_ci_pending_and_error_return_human_required_without_codex(
        self,
    ):
        def boom_runner(command: list[str], prompt: str, cwd: str) -> str:
            raise AssertionError("codex runner must not be called")

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        with tempfile.TemporaryDirectory() as tmp:
            for ci_status in ("PENDING", "ERROR"):
                provider = CodexAuditProvider(
                    runner=boom_runner,
                    git_runner=fake_git,
                    evidence_bundle={
                        "schema": "awc.codex_evidence_bundle.v1",
                        "git": {"head": HEAD, "evidence_status": "OK"},
                        "tests": {"status": "PASS"},
                        "ci": {"status": ci_status, "detail": "waiting"},
                    },
                )
                result = provider.audit(self._event(), self._record(tmp))
                self.assertEqual(result.verdict, "HUMAN_REQUIRED")
                self.assertIn(f"CI {ci_status}", result.findings)
                self.assertIsNone(provider.last_command)

    def test_deterministic_ci_absent_allows_codex(self):
        """Local/no-PR cycle: CI ABSENT continues to Codex."""

        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "datarelay-labs/datarelay-atlas"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): "",
            }
            return mapping[tuple(argv)]

        def pass_runner(command: list[str], prompt: str, cwd: str) -> str:
            out = Path(command[command.index("-o") + 1])
            out.write_text(
                '{"verdict":"PASS","findings":"local cycle ok"}',
                encoding="utf-8",
            )
            return out.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            provider = CodexAuditProvider(
                runner=pass_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "tests": {"status": "PASS"},
                    "ci": {"status": "ABSENT"},
                },
            )
            result = provider.audit(self._event(), self._record(tmp))
            self.assertEqual(result.verdict, "PASS")
            self.assertIsNotNone(provider.last_command)

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

    def test_ci_collector_maps_auth_and_transport_errors_to_error(self):
        from atlas.codex_audit import classify_gh_pr_checks_result

        self.assertEqual(
            classify_gh_pr_checks_result(4, "", "auth failed"),
            ("ERROR", "gh pr checks authentication failure"),
        )
        status, detail = classify_gh_pr_checks_result(
            1, "", "GraphQL: Resource not accessible"
        )
        self.assertEqual(status, "ERROR")
        self.assertIn("Resource not accessible", detail)

        # Exit 1 with parseable failed check rows remains FAIL.
        self.assertEqual(
            classify_gh_pr_checks_result(1, "unit\tfail\t1s\thttps://x\n", ""),
            ("FAIL", ""),
        )
        # Documented gh bucket value `cancel` maps to FAIL → REWORK.
        self.assertEqual(
            classify_gh_pr_checks_result(1, "job\tcancel\t1s\thttps://x\n", ""),
            ("FAIL", ""),
        )
        # PR with zero check runs is ABSENT, not a transport ERROR.
        absent_msg = "no checks reported on the 'feature/x' branch"
        self.assertEqual(
            classify_gh_pr_checks_result(1, "", absent_msg),
            ("ABSENT", absent_msg),
        )

        calls: list[list[str]] = []

        def fake(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:3] == ["gh", "pr", "list"]:
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
                    argv, 1, stdout="", stderr="HTTP 401: Bad credentials"
                )
            raise AssertionError(argv)

        ci = collect_ci_evidence(
            repository="datarelay-labs/datarelay-atlas",
            head=HEAD,
            command_runner=fake,
        )
        self.assertEqual(ci["status"], "ERROR")
        self.assertEqual(ci["checks_exit_code"], 1)

    def test_ci_collector_maps_no_checks_to_absent(self):
        def fake(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "pr", "list"]:
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
                    argv,
                    1,
                    stdout="",
                    stderr="no checks reported on the 'feature/x' branch",
                )
            raise AssertionError(argv)

        ci = collect_ci_evidence(
            repository="datarelay-labs/datarelay-atlas",
            head=HEAD,
            command_runner=fake,
        )
        self.assertEqual(ci["status"], "ABSENT")

    def test_absent_ci_with_pr_still_collects_reviews(self):
        """CI ABSENT must not skip review evidence when a PR number is known."""
        from atlas.codex_audit import collect_audit_evidence_bundle
        from atlas.work_controller import (
            CompletionEvent,
            WorkstreamRecord,
            WorktreeIdentity,
        )

        calls: list[list[str]] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:3] == ["gh", "pr", "list"]:
                payload = [
                    {
                        "number": 16,
                        "url": "https://example.invalid/pr/16",
                        "state": "OPEN",
                        "headRefOid": HEAD,
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="no checks reported on the 'feature/x' branch",
                )
            if argv[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps([[]]), stderr=""
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as tmp:
            identity = WorktreeIdentity(
                worktree_path=tmp,
                repository="datarelay-labs/datarelay-atlas",
                branch="feature/x",
                head=HEAD,
                toplevel=tmp,
            )
            record = WorkstreamRecord(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/x",
                worktree_path=tmp,
                expected_head=HEAD,
                state="IDLE",
                attempt=1,
                max_attempts=3,
            )
            event = CompletionEvent(
                event_id="evt-1",
                workstream="awc-poc",
                issue_number=12,
                branch="feature/x",
                head=HEAD,
                attempt=1,
            )

            def fake_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                if tuple(argv) == ("git", "rev-parse", "HEAD"):
                    return HEAD
                if tuple(argv) == ("git", "status", "--porcelain", "--untracked-files=all"):
                    return ""
                if argv[:2] == ["git", "diff"]:
                    return ""
                if argv[:2] == ["git", "log"]:
                    return ""
                return ""

            bundle = collect_audit_evidence_bundle(
                event,
                record,
                identity=identity,
                git_runner=fake_git,
                command_runner=runner,
                include_tests=False,
                include_work_packet=False,
            )
            self.assertEqual(bundle["ci"]["status"], "ABSENT")
            self.assertEqual(bundle["ci"]["pr"]["number"], 16)
            self.assertEqual(bundle["pr_reviews"]["status"], "OK")
            self.assertTrue(
                any(argv[:2] == ["gh", "api"] for argv in calls),
                msg=f"expected review API calls, got {calls!r}",
            )

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
        seen_paths: list[str] = []

        def slurp(pages: list) -> str:
            return json.dumps(pages)

        def ok_runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:4], ["gh", "api", "--paginate", "--slurp"])
            path = argv[4]
            seen_paths.append(path)
            if path.endswith("/reviews"):
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
                    argv, 0, stdout=slurp([payload]), stderr=""
                )
            if "/pulls/" in path and path.endswith("/comments"):
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
                    argv, 0, stdout=slurp([payload]), stderr=""
                )
            if "/issues/" in path and path.endswith("/comments"):
                payload = [
                    {
                        "id": 42,
                        "user": {"login": "reviewer"},
                        "body": "conversation P1 finding",
                        "created_at": "2026-09-21T00:00:00Z",
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=slurp([payload]), stderr=""
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
        self.assertEqual(
            ok["conversation_comments"][0]["body"], "conversation P1 finding"
        )
        self.assertEqual(
            seen_paths,
            [
                "repos/datarelay-labs/datarelay-atlas/pulls/15/reviews",
                "repos/datarelay-labs/datarelay-atlas/pulls/15/comments",
                "repos/datarelay-labs/datarelay-atlas/issues/15/comments",
            ],
        )

    def test_pr_review_evidence_preserves_later_page_findings(self):
        """Two slurped pages must flatten so page-2 actionable findings survive."""

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:4], ["gh", "api", "--paginate", "--slurp"])
            path = argv[4]
            if "/pulls/" in path and path.endswith("/comments"):
                page1 = [
                    {
                        "id": 1,
                        "user": {"login": "reviewer"},
                        "body": "page1 noise",
                        "path": "atlas/codex_audit.py",
                        "line": 1,
                        "commit_id": HEAD,
                        "created_at": "2026-09-21T00:00:00Z",
                    }
                ]
                page2 = [
                    {
                        "id": 2,
                        "user": {"login": "reviewer"},
                        "body": "P1 later-page actionable finding",
                        "path": "atlas/codex_audit.py",
                        "line": 99,
                        "commit_id": HEAD,
                        "created_at": "2026-09-21T00:01:00Z",
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps([page1, page2]), stderr=""
                )
            # Empty single slurped page for other sections.
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps([[]]), stderr=""
            )

        result = collect_pr_review_evidence(
            repository="datarelay-labs/datarelay-atlas",
            pr_number=15,
            command_runner=runner,
        )
        self.assertEqual(result["status"], "OK")
        bodies = [c["body"] for c in result["inline_comments"]]
        self.assertEqual(
            bodies,
            ["page1 noise", "P1 later-page actionable finding"],
        )
        self.assertEqual(result["inline_comments"][1]["id"], 2)

    def test_pr_review_evidence_fail_closed_on_non_list_slurp_page(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:4], ["gh", "api", "--paginate", "--slurp"])
            path = argv[4]
            if path.endswith("/reviews"):
                # Outer array present, but page 0 is an object — fail closed.
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps([{"id": 1, "body": "not a list page"}]),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps([[]]), stderr=""
            )

        err = collect_pr_review_evidence(
            repository="datarelay-labs/datarelay-atlas",
            pr_number=15,
            command_runner=runner,
        )
        self.assertEqual(err["status"], "ERROR")
        self.assertEqual(err["failed_section"], "reviews")
        self.assertIn("page 0 is not a JSON array", err["detail"])

    def test_pr_review_evidence_fail_closed_when_single_body_trimmed(self):
        """One long comment: actionable marker in trimmed tail ⇒ INCOMPLETE."""
        marker = "P1_ACTIONABLE_IN_TRIMMED_TAIL"
        # Per-item body cap is 1200; put the marker only after that prefix.
        long_body = ("x" * 1200) + marker
        self.assertGreater(len(long_body), 1200)

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:4], ["gh", "api", "--paginate", "--slurp"])
            path = argv[4]
            if "/pulls/" in path and path.endswith("/comments"):
                page = [
                    {
                        "id": 77,
                        "user": {"login": "reviewer"},
                        "body": long_body,
                        "path": "atlas/codex_audit.py",
                        "line": 10,
                        "commit_id": HEAD,
                        "created_at": "2026-09-21T00:00:00Z",
                    }
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps([page]), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps([[]]), stderr=""
            )

        result = collect_pr_review_evidence(
            repository="datarelay-labs/datarelay-atlas",
            pr_number=15,
            command_runner=runner,
        )
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("inline_comments", result["truncated_sections"])
        self.assertEqual(len(result["inline_comments"]), 1)
        bounded = result["inline_comments"][0]["body"]
        self.assertNotIn(marker, bounded)
        self.assertIn("...[truncated]...", bounded)

        from atlas.codex_audit import _bounded_review_items

        items, truncated = _bounded_review_items(
            [
                {
                    "id": 77,
                    "user": {"login": "reviewer"},
                    "body": long_body,
                }
            ],
            max_chars=8000,
        )
        self.assertTrue(truncated)
        self.assertEqual(len(items), 1)
        self.assertNotIn(marker, items[0]["body"])

    def test_pr_review_evidence_fail_closed_when_section_truncated(self):
        big_body = "P1 finding " + ("x" * 3000)

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:4], ["gh", "api", "--paginate", "--slurp"])
            path = argv[4]
            if path.endswith("/reviews"):
                payload = [
                    {
                        "id": i,
                        "user": {"login": "reviewer"},
                        "state": "COMMENTED",
                        "body": big_body,
                        "commit_id": HEAD,
                        "submitted_at": "2026-09-21T00:00:00Z",
                    }
                    for i in range(1, 4)
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps([payload]), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps([[]]), stderr=""
            )

        result = collect_pr_review_evidence(
            repository="datarelay-labs/datarelay-atlas",
            pr_number=15,
            command_runner=runner,
            max_chars=2400,
        )
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("reviews", result["truncated_sections"])
        self.assertIn("truncated under section budget", result["detail"])
        self.assertGreaterEqual(len(result["reviews"]), 1)
        self.assertLess(len(result["reviews"]), 3)

    def test_bounded_review_items_preserve_original_commit_and_line(self):
        from atlas.codex_audit import _bounded_review_items

        remapped = HEAD
        original = "2a502eec7f267814fed3de46da7db88b474446b1"
        items, truncated = _bounded_review_items(
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
        self.assertFalse(truncated)
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
            if argv[:2] == ["gh", "api"]:
                self.assertEqual(argv[2:4], ["--paginate", "--slurp"])
                path = argv[4]
                if path.endswith("/reviews"):
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps([[]]), stderr=""
                    )
                if "/pulls/" in path and path.endswith("/comments"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
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
                            ]
                        ),
                        stderr="",
                    )
                if "/issues/" in path and path.endswith("/comments"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                [
                                    {
                                        "id": 7,
                                        "user": {"login": "owner"},
                                        "body": "conversation note",
                                    }
                                ]
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
        self.assertEqual(
            bundle["pr_reviews"]["conversation_comments"][0]["body"],
            "conversation note",
        )

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

            provider_incomplete = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "pr_reviews": {
                        "status": "INCOMPLETE",
                        "detail": "review evidence truncated under section budget",
                    },
                },
            )
            incomplete = provider_incomplete.audit(self._event(), self._record(tmp))
            self.assertEqual(incomplete.verdict, "HUMAN_REQUIRED")
            self.assertIn(
                "PR review evidence status=INCOMPLETE", incomplete.findings
            )
            self.assertIsNone(provider_incomplete.last_command)

            provider_packet = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {"head": HEAD, "evidence_status": "OK"},
                    "work_packet": {
                        "status": "INCOMPLETE",
                        "detail": "work packet body truncated under max_chars budget",
                    },
                },
            )
            packet = provider_packet.audit(self._event(), self._record(tmp))
            self.assertEqual(packet.verdict, "HUMAN_REQUIRED")
            self.assertIn("Work Packet evidence status=INCOMPLETE", packet.findings)
            self.assertIsNone(provider_packet.last_command)

            provider_git = CodexAuditProvider(
                runner=boom_runner,
                git_runner=fake_git,
                evidence_bundle={
                    "schema": "awc.codex_evidence_bundle.v1",
                    "git": {
                        "head": HEAD,
                        "evidence_status": "INCOMPLETE",
                        "detail": "git diff truncated under max_diff_chars budget",
                    },
                },
            )
            git_incomplete = provider_git.audit(self._event(), self._record(tmp))
            self.assertEqual(git_incomplete.verdict, "HUMAN_REQUIRED")
            self.assertIn("git evidence status=INCOMPLETE", git_incomplete.findings)
            self.assertIsNone(provider_git.last_command)


if __name__ == "__main__":
    unittest.main()
