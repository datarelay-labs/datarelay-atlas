"""GitHub Work Packet mutation adapter regressions (ADR-0006 Decision 8)."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atlas.cli import build_parser
from atlas.provenance import ValidationError
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    redact_absolute_paths,
    redact_sensitive_audit_text,
    render_rework_work_packet_body,
    sanitize_rework_findings,
)


BRANCH = "feature/autonomous-work-controller-final-hardening"
WORKSTREAM = "autonomous-work-controller-poc"
HEAD_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

SAMPLE_BODY = f"""PACKET_VERSION=2
TARGET_REPO=datarelay-labs/datarelay-atlas
WORKSTREAM={WORKSTREAM}
STATUS=ACTIVE
BRANCH={BRANCH}
TASK_KIND=DEVELOPMENT
OWNER_INTENT=Complete the AWC PoC safely.
LAST_VERIFIED_HEAD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

## Goal

Ship the PoC.

## Current State

- Prior state.

## Next Action

Prior next action.

## Constraints

- Keep scope tight.

## Canonical References

- Issue #12

## Latest Evidence

```text
HEAD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

## Blockers

NONE
"""


def _render(**overrides):
    kwargs = {
        "body": SAMPLE_BODY,
        "repository": "datarelay-labs/datarelay-atlas",
        "branch": BRANCH,
        "workstream": WORKSTREAM,
        "findings": "fix gaps\nneed coverage",
        "attempt": 2,
        "head": HEAD_B,
    }
    kwargs.update(overrides)
    body = kwargs.pop("body")
    return render_rework_work_packet_body(body, **kwargs)


class RenderReworkWorkPacketBodyTests(unittest.TestCase):
    def test_updates_sections_and_head(self):
        updated = _render()
        self.assertIn(f"LAST_VERIFIED_HEAD={HEAD_B}", updated)
        self.assertIn("Controller audit verdict: REWORK", updated)
        self.assertIn("fix gaps", updated)
        self.assertIn("Address the REWORK findings", updated)
        self.assertIn("WORK_PACKET_MUTATION=PENDING_DISPATCH", updated)
        self.assertIn("## Constraints", updated)
        self.assertIn("Keep scope tight.", updated)
        self.assertIn("## Goal", updated)
        self.assertIn("Ship the PoC.", updated)

    def test_rejects_inactive_mismatched_target_or_branch(self):
        with self.assertRaises(ValidationError):
            _render(body=SAMPLE_BODY.replace("STATUS=ACTIVE", "STATUS=PAUSED"))
        with self.assertRaises(ValidationError):
            _render(
                body=SAMPLE_BODY.replace(
                    "TARGET_REPO=datarelay-labs/datarelay-atlas",
                    "TARGET_REPO=other-org/other-repo",
                )
            )
        missing_target = "\n".join(
            line
            for line in SAMPLE_BODY.splitlines()
            if not line.startswith("TARGET_REPO=")
        )
        with self.assertRaises(ValidationError):
            _render(body=missing_target)
        with self.assertRaises(ValidationError):
            _render(branch="feature/other-branch")

    def test_rejects_duplicate_managed_packet_sections(self):
        duplicated = SAMPLE_BODY + "\n## Next Action\n\nStale second next action.\n"
        with self.assertRaises(ValidationError) as ctx:
            _render(body=duplicated)
        self.assertIn("duplicate work packet section: Next Action", str(ctx.exception))

    def test_status_and_branch_ignore_body_evidence_lines(self):
        paused = SAMPLE_BODY.replace("STATUS=ACTIVE", "STATUS=PAUSED")
        poisoned = paused.replace(
            "```text\nHEAD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n```",
            "```text\nSTATUS=ACTIVE\nBRANCH=feature/other-branch\n```",
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=poisoned)
        self.assertIn("STATUS must be ACTIVE", str(ctx.exception))
        missing_branch = "\n".join(
            line for line in SAMPLE_BODY.splitlines() if not line.startswith("BRANCH=")
        )
        # Put BRANCH only inside an existing evidence fence (not leading metadata).
        missing_branch = missing_branch.replace(
            "```text\nHEAD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n```",
            f"```text\nBRANCH={BRANCH}\nHEAD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n```",
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=missing_branch)
        self.assertIn("missing BRANCH metadata", str(ctx.exception))

    def test_rejects_duplicate_leading_metadata_fields(self):
        dup = SAMPLE_BODY.replace(
            "STATUS=ACTIVE\n",
            "STATUS=ACTIVE\nSTATUS=PAUSED\n",
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=dup)
        self.assertIn("duplicate work packet metadata field: STATUS", str(ctx.exception))

    def test_sanitize_rejects_secret_shaped_findings(self):
        assigned = "OPENAI_API_KEY" + "=" + "redacted-value"
        with self.assertRaises(ValidationError) as ctx:
            sanitize_rework_findings(f"failure transcript\n{assigned}\n")
        self.assertIn("secrets", str(ctx.exception).lower())
        with self.assertRaises(ValidationError):
            _render(findings=f"boom\n{assigned}\n")
        github_token = "GITHUB_TOKEN" + "=" + "ghp_" + ("x" * 20)
        aws_secret = "AWS_SECRET_ACCESS_KEY" + "=" + ("y" * 24)
        with self.assertRaises(ValidationError):
            sanitize_rework_findings(github_token)
        with self.assertRaises(ValidationError):
            sanitize_rework_findings(aws_secret)

    def test_sanitize_redacts_absolute_local_paths(self):
        dirty = "worktree_path is not a directory: /home/runner/proj/target-wt"
        clean = sanitize_rework_findings(dirty)
        self.assertIn("<local-path>", clean)
        self.assertNotIn("/home/runner", clean)
        self.assertNotIn("target-wt", clean)
        workspace = sanitize_rework_findings(
            "cwd mismatch: /workspace/datarelay-atlas/feature-wt"
        )
        self.assertIn("<local-path>", workspace)
        self.assertNotIn("/workspace/", workspace)

    def test_sanitize_rejects_lowercase_secret_assignments(self):
        with self.assertRaises(ValidationError):
            sanitize_rework_findings("service_token=supersecretvalue123")
        with self.assertRaises(ValidationError):
            sanitize_rework_findings("client_secret=another-secret-value")

    def test_colon_and_json_credential_forms_are_detected_and_redacted(self):
        from atlas.work_controller import _looks_like_secret

        colon = "access_token: bare-secret-value-12345"
        quoted = 'client_secret: "quoted-secret-value-12345"'
        json_secret = '{"client_secret": "json-secret-value-12345"}'
        json_token = '{"access_token":"tokensecretvalue12345"}'
        for sample in (colon, quoted, json_secret, json_token):
            self.assertTrue(_looks_like_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted>", redacted)
            self.assertNotIn("secret-value", redacted)
            self.assertNotIn("tokensecretvalue", redacted)

    def test_quoted_credential_values_with_whitespace_are_redacted(self):
        from atlas.work_controller import _looks_like_secret

        spaced = '{"password":"correct horse"}'
        comma = '{"client_secret": "has space and, comma"}'
        log_form = 'password: "correct horse"'
        for sample in (spaced, comma, log_form):
            self.assertTrue(_looks_like_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted>", redacted)
            self.assertNotIn("correct horse", redacted)
            self.assertNotIn("has space and, comma", redacted)

    def test_quoted_credential_values_match_selected_delimiter(self):
        from atlas.work_controller import _looks_like_secret

        # Apostrophe inside double quotes must not truncate the value.
        apostrophe = "{\"password\":\"correct horse's battery\"}"
        # Escaped delimiter must keep the full credential in one match.
        escaped = r'password="correct \"horse\" battery"'
        single_with_double = "password='say \"hi\" secret'"
        for sample in (apostrophe, escaped, single_with_double):
            self.assertTrue(_looks_like_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted>", redacted)
            self.assertNotIn("correct horse", redacted)
            self.assertNotIn("horse", redacted)
            self.assertNotIn('say "hi" secret', redacted)

    def test_camel_case_api_key_assignments_are_redacted(self):
        from atlas.work_controller import _looks_like_secret

        camel = '{"apiKey":"correct horse battery"}'
        prefixed = '{"openaiApiKey":"correct horse battery"}'
        for sample in (camel, prefixed):
            self.assertTrue(_looks_like_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted>", redacted)
            self.assertNotIn("correct horse battery", redacted)

    def test_hyphenated_api_key_assignments_are_redacted(self):
        from atlas.work_controller import _looks_like_secret

        header = 'X-API-Key: "correct horse battery"'
        json_key = '{"api-key":"correct horse battery"}'
        for sample in (header, json_key):
            self.assertTrue(_looks_like_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted>", redacted)
            self.assertNotIn("correct horse battery", redacted)

    def test_audit_redaction_covers_aws_credential_assignments(self):
        aws_secret = "AWS_SECRET_ACCESS_KEY" + "=" + ("y" * 24)
        aws_key_id = "AWS_ACCESS_KEY_ID" + "=" + ("Z" * 20)
        secret_out = redact_sensitive_audit_text(f"boom\n{aws_secret}\n")
        key_out = redact_sensitive_audit_text(f"boom\n{aws_key_id}\n")
        self.assertIn("AWS_SECRET_ACCESS_KEY=<redacted>", secret_out)
        self.assertNotIn("y" * 24, secret_out)
        self.assertIn("AWS_ACCESS_KEY_ID=<redacted>", key_out)
        self.assertNotIn("Z" * 20, key_out)

    def test_path_redaction_preserves_https_urls(self):
        text = (
            "see https://github.com/datarelay-labs/datarelay-atlas/pull/16 "
            "and /home/runner/proj/target-wt"
        )
        cleaned = redact_absolute_paths(text)
        self.assertIn(
            "https://github.com/datarelay-labs/datarelay-atlas/pull/16", cleaned
        )
        self.assertNotIn("https:/<local-path>", cleaned)
        self.assertIn("<local-path>", cleaned)
        self.assertNotIn("/home/runner", cleaned)
        sanitized = sanitize_rework_findings(text)
        self.assertIn(
            "https://github.com/datarelay-labs/datarelay-atlas/pull/16", sanitized
        )
        self.assertNotIn("https:/<local-path>", sanitized)

    def test_findings_cannot_inject_packet_headings(self):
        updated = _render(
            findings="before\n## Next Action\nstolen\n## Blockers\nbad"
        )
        # Canonical Next Action section still carries the handoff text.
        self.assertIn("Address the REWORK findings below", updated)
        # Injected heading text is neutralized, not a real ATX heading.
        self.assertIn("› ## Next Action", updated)
        self.assertNotRegex(
            updated,
            r"(?m)^## Next Action\s*\n\nstolen",
        )
        # Exactly one Next Action / Blockers heading each.
        self.assertEqual(len(re.findall(r"(?m)^## Next Action\s*$", updated)), 1)
        self.assertEqual(len(re.findall(r"(?m)^## Blockers\s*$", updated)), 1)

    def test_section_replacement_keeps_backslash_sequences_literal(self):
        """Findings with \\d+ / \\1 must not raise or expand via re.sub templates."""
        findings = "regex hint uses \\d+ and group \\1 literally"
        updated = _render(findings=findings)
        self.assertIn("\\d+", updated)
        self.assertIn("\\1", updated)
        self.assertIn("Address the REWORK findings below", updated)
        # Ensure the literal sequences survived into Latest Evidence as well.
        self.assertRegex(
            updated,
            r"(?ms)^## Latest Evidence\n.*\\d\+.*\\1",
        )

    def test_sanitize_strips_controls_and_bounds(self):
        dirty = "ok\x00line\n" + ("a" * 5000)
        clean = sanitize_rework_findings(dirty, max_chars=100)
        self.assertNotIn("\x00", clean)
        self.assertIn("okline", clean.replace("\n", ""))
        self.assertTrue(clean.endswith("...[truncated]...\n") or "[truncated]" in clean)
        self.assertLessEqual(len(clean), 120)


class GitHubWorkPacketAdapterTests(unittest.TestCase):
    def _payload(self, **overrides) -> dict:
        payload = {
            "number": 12,
            "title": "[AI Work] DRAtlas Autonomous Work Controller PoC",
            "state": "OPEN",
            "body": SAMPLE_BODY,
            "updatedAt": "2026-09-22T01:00:00Z",
        }
        payload.update(overrides)
        return payload

    def _list_payload(self, *issues: dict) -> list[dict]:
        if issues:
            return list(issues)
        return [
            {
                "number": 12,
                "title": "[AI Work] DRAtlas Autonomous Work Controller PoC",
                "state": "OPEN",
                "body": SAMPLE_BODY,
            }
        ]

    def _rework_kwargs(self, **overrides):
        kwargs = {
            "repository": "datarelay-labs/datarelay-atlas",
            "issue_number": 12,
            "branch": BRANCH,
            "workstream": WORKSTREAM,
            "findings": "fix gaps; do not $(rm -rf /)",
            "attempt": 2,
            "head": HEAD_B,
        }
        kwargs.update(overrides)
        return kwargs

    def test_view_recheck_then_edit_with_body_file_argv(self):
        calls: list[list[str]] = []
        body_files: list[str] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._list_payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                self.assertIn("--body-file", argv)
                path = argv[argv.index("--body-file") + 1]
                body_files.append(Path(path).read_text(encoding="utf-8"))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            self.fail(f"unexpected argv: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        adapter.apply_rework_findings(**self._rework_kwargs())
        self.assertEqual(len(calls), 4)  # list, view, recheck view, edit
        self.assertEqual(calls[0][:4], ["gh", "issue", "list", "--repo"])
        self.assertEqual(
            calls[1],
            [
                "gh",
                "issue",
                "view",
                "12",
                "--repo",
                "datarelay-labs/datarelay-atlas",
                "--json",
                "number,title,state,body,updatedAt",
            ],
        )
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(
            calls[3][:6],
            ["gh", "issue", "edit", "12", "--repo", "datarelay-labs/datarelay-atlas"],
        )
        joined = " ".join(calls[3])
        self.assertNotIn("$(rm -rf /)", joined)
        self.assertEqual(len(body_files), 1)
        self.assertIn("fix gaps", body_files[0])
        self.assertIn("do not $(rm -rf /)", body_files[0])
        self.assertFalse(Path(calls[3][calls[3].index("--body-file") + 1]).exists())

    def test_concurrent_change_fails_closed(self):
        views = [
            self._payload(updatedAt="2026-09-22T01:00:00Z"),
            self._payload(
                body=SAMPLE_BODY + "\n<!-- concurrent -->\n",
                updatedAt="2026-09-22T01:00:05Z",
            ),
        ]

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._list_payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                payload = views.pop(0)
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            self.fail(f"edit must not run on conflict: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertIn("changed during mutation", str(ctx.exception))

    def test_edit_failure_raises_validation_error(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._list_payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._payload()), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="edit denied"
            )

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertIn("edit denied", str(ctx.exception))

    def test_run_permission_error_is_validation_error(self):
        adapter = GitHubWorkPacketAdapter()
        with mock.patch(
            "subprocess.run", side_effect=PermissionError("exec denied")
        ):
            with self.assertRaises(ValidationError) as ctx:
                adapter._run(["gh", "issue", "view", "12", "--json", "body"])
        self.assertNotIsInstance(ctx.exception, PermissionError)
        self.assertIn("failed to start", str(ctx.exception))

    def test_rejects_non_ai_work_issue(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "list"]:
                # Unique match points at #12, but configured issue is #10.
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._list_payload()), stderr=""
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    self._payload(
                        number=10,
                        title="[Roadmap] something else",
                    )
                ),
                stderr="",
            )

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(
                **self._rework_kwargs(issue_number=10, findings="x")
            )
        self.assertIn("not the unique ACTIVE", str(ctx.exception))

    def test_rejects_workstream_mismatch(self):
        with self.assertRaises(ValidationError) as ctx:
            _render(workstream="other-workstream")
        self.assertIn("WORKSTREAM mismatch", str(ctx.exception))

    def test_rejects_ambiguous_active_packets(self):
        other = SAMPLE_BODY.replace(
            "OWNER_INTENT=Complete the AWC PoC safely.",
            "OWNER_INTENT=Other packet.",
        )

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        self._list_payload(
                            {
                                "number": 12,
                                "title": "[AI Work] one",
                                "state": "OPEN",
                                "body": SAMPLE_BODY,
                            },
                            {
                                "number": 99,
                                "title": "[AI Work] two",
                                "state": "OPEN",
                                "body": other,
                            },
                        )
                    ),
                    stderr="",
                )
            self.fail(f"must not continue after ambiguity: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertIn("ambiguous ACTIVE Work Packets", str(ctx.exception))

    def test_branchless_active_packet_counts_in_uniqueness(self):
        branchless = "\n".join(
            line for line in SAMPLE_BODY.splitlines() if not line.startswith("BRANCH=")
        )

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        self._list_payload(
                            {
                                "number": 12,
                                "title": "[AI Work] branched",
                                "state": "OPEN",
                                "body": SAMPLE_BODY,
                            },
                            {
                                "number": 77,
                                "title": "[AI Work] branchless",
                                "state": "OPEN",
                                "body": branchless,
                            },
                        )
                    ),
                    stderr="",
                )
            self.fail(f"must not continue after ambiguity: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertIn("ambiguous ACTIVE Work Packets", str(ctx.exception))

    def test_pem_private_keys_are_detected_and_redacted(self):
        from atlas.work_controller import _looks_like_secret

        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
            "-----END PRIVATE KEY-----"
        )
        rsa = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn\n"
            "-----END RSA PRIVATE KEY-----"
        )
        dsa = (
            "-----BEGIN DSA PRIVATE KEY-----\n"
            "MIIBuwIBAAKBgQDF\n"
            "-----END DSA PRIVATE KEY-----"
        )
        hyphenated = (
            "-----BEGIN FOO-BAR PRIVATE KEY-----\n"
            "MIIBuwIBAAKBgQDF\n"
            "-----END FOO-BAR PRIVATE KEY-----"
        )
        for sample in (pem, rsa, dsa, hyphenated):
            self.assertTrue(_looks_like_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted-private-key>", redacted)
            self.assertNotIn("BEGIN", redacted)
            self.assertNotIn("MII", redacted)

    def test_safe_redacted_placeholders_are_allowed(self):
        safe = "OPENAI_API_KEY=<redacted>\npassword=<redacted>"
        cleaned = sanitize_rework_findings(safe)
        self.assertIn("<redacted>", cleaned)
        updated = _render(findings=safe)
        self.assertIn("<redacted>", updated)

    def test_rejects_missing_v2_task_kind_or_owner_intent(self):
        missing_task = "\n".join(
            line for line in SAMPLE_BODY.splitlines() if not line.startswith("TASK_KIND=")
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=missing_task)
        self.assertIn("TASK_KIND", str(ctx.exception))
        missing_intent = "\n".join(
            line
            for line in SAMPLE_BODY.splitlines()
            if not line.startswith("OWNER_INTENT=")
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=missing_intent)
        self.assertIn("OWNER_INTENT", str(ctx.exception))

    def test_edit_timeout_reconciles_when_body_landed(self):
        views = [
            self._payload(),  # uniqueness list uses separate path
        ]
        # After timeout, reconcile view returns the intended new body.
        landed_bodies: list[str] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._list_payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                if landed_bodies:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(self._payload(body=landed_bodies[-1])),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                path = argv[argv.index("--body-file") + 1]
                landed_bodies.append(Path(path).read_text(encoding="utf-8"))
                raise ValidationError("gh timed out after 60s")
            self.fail(f"unexpected argv: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        # Must not raise: timeout + landed body == success.
        adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertEqual(len(landed_bodies), 1)

    def test_hyphenated_api_key_json_escaped_in_prompt_is_detected(self):
        from atlas.codex_audit import build_codex_audit_prompt
        from atlas.work_controller import (
            CompletionEvent,
            WorkstreamRecord,
            WorktreeIdentity,
            _looks_like_secret,
        )

        secret = '{"api-key":"correct horse battery"}'
        self.assertTrue(_looks_like_secret(secret))
        event = CompletionEvent(
            event_id="e1",
            workstream=WORKSTREAM,
            issue_number=12,
            branch=BRANCH,
            head=HEAD_B,
            attempt=1,
        )
        record = WorkstreamRecord(
            workstream=WORKSTREAM,
            repository="datarelay-labs/datarelay-atlas",
            worktree_path="/tmp/wt",
            branch=BRANCH,
            issue_number=12,
            max_attempts=3,
            state="AUDITING",
            attempt=1,
            expected_head=HEAD_B,
        )
        identity = WorktreeIdentity(
            worktree_path="/tmp/wt",
            repository="datarelay-labs/datarelay-atlas",
            branch=BRANCH,
            head=HEAD_B,
            toplevel="/tmp/wt",
        )
        prompt = build_codex_audit_prompt(
            event,
            record,
            identity=identity,
            evidence_bundle={"tests": {"output": secret}},
        )
        self.assertIn('\\"api-key\\"', prompt)
        self.assertTrue(_looks_like_secret(prompt), prompt[:500])
        redacted = redact_sensitive_audit_text(prompt)
        self.assertNotIn("correct horse battery", redacted)

    def test_double_json_escaped_credential_in_prompt_is_detected(self):
        from atlas.codex_audit import build_codex_audit_prompt
        from atlas.work_controller import (
            CompletionEvent,
            WorkstreamRecord,
            WorktreeIdentity,
            _contains_unsafe_secret,
            _looks_like_secret,
        )

        # Evidence already contains one JSON-escape layer; prompt dumps again.
        already_escaped = r'{\"password\":\"hunter2-secret-value\"}'
        self.assertTrue(_looks_like_secret(already_escaped), already_escaped)
        event = CompletionEvent(
            event_id="e1",
            workstream=WORKSTREAM,
            issue_number=12,
            branch=BRANCH,
            head=HEAD_B,
            attempt=1,
        )
        record = WorkstreamRecord(
            workstream=WORKSTREAM,
            repository="datarelay-labs/datarelay-atlas",
            worktree_path="/tmp/wt",
            branch=BRANCH,
            issue_number=12,
            max_attempts=3,
            state="AUDITING",
            attempt=1,
            expected_head=HEAD_B,
        )
        identity = WorktreeIdentity(
            worktree_path="/tmp/wt",
            repository="datarelay-labs/datarelay-atlas",
            branch=BRANCH,
            head=HEAD_B,
            toplevel="/tmp/wt",
        )
        prompt = build_codex_audit_prompt(
            event,
            record,
            identity=identity,
            evidence_bundle={"tests": {"output": already_escaped}},
        )
        self.assertIn("hunter2-secret-value", prompt)
        self.assertTrue(_looks_like_secret(prompt), prompt[:500])
        self.assertTrue(_contains_unsafe_secret(prompt), prompt[:500])


class CliWorkPacketAdapterSelectionTests(unittest.TestCase):
    def test_fixed_defaults_to_recording_adapter(self):
        from atlas import cli as atlas_cli
        from atlas.work_controller import RecordingWorkPacketAdapter

        parser = build_parser()
        args = parser.parse_args(
            [
                "work-controller",
                "completion",
                "/tmp/event.json",
                "--audit-adapter",
                "fixed",
            ]
        )
        self.assertIsNone(args.work_packet_adapter)
        with tempfile.TemporaryDirectory() as tmp:
            args.data_root = tmp
            args.spawn_dispatch = False
            ctl = atlas_cli._controller_from_args(args)
            self.assertIsInstance(ctl.work_packet, RecordingWorkPacketAdapter)

    def test_codex_with_spawn_defaults_to_github_adapter(self):
        from atlas import cli as atlas_cli
        from atlas.work_controller import (
            GitHubWorkPacketAdapter,
            PtyPersistCursorDispatcher,
        )

        parser = build_parser()
        args = parser.parse_args(
            [
                "work-controller",
                "completion",
                "/tmp/event.json",
                "--audit-adapter",
                "codex",
                "--spawn-dispatch",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            args.data_root = tmp
            ctl = atlas_cli._controller_from_args(args)
            self.assertIsInstance(ctl.work_packet, GitHubWorkPacketAdapter)
            self.assertIsInstance(ctl.dispatcher, PtyPersistCursorDispatcher)

    def test_github_without_spawn_fails_closed(self):
        from atlas import cli as atlas_cli

        parser = build_parser()
        args = parser.parse_args(
            [
                "work-controller",
                "completion",
                "/tmp/event.json",
                "--audit-adapter",
                "codex",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            args.data_root = tmp
            args.spawn_dispatch = False
            with self.assertRaises(ValidationError):
                atlas_cli._controller_from_args(args)

    def test_register_show_list_use_offline_safe_adapters(self):
        from atlas import cli as atlas_cli
        from atlas.work_controller import RecordingWorkPacketAdapter

        parser = build_parser()
        for argv in (
            ["work-controller", "list"],
            ["work-controller", "show", "awc"],
            [
                "work-controller",
                "register",
                "awc",
                "--repository",
                "datarelay-labs/datarelay-atlas",
                "--issue-number",
                "12",
                "--branch",
                "feature/x",
                "--worktree",
                "/tmp",
                "--expected-head",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
        ):
            args = parser.parse_args(argv)
            with tempfile.TemporaryDirectory() as tmp:
                args.data_root = tmp
                ctl = atlas_cli._controller_from_args(args)
                self.assertIsInstance(ctl.work_packet, RecordingWorkPacketAdapter)
                self.assertFalse(hasattr(args, "audit_adapter"))


if __name__ == "__main__":
    unittest.main()
