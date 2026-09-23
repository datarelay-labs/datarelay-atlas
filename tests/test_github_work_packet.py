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

    def test_rejects_noncanonical_target_repo_clone_url(self):
        """Clone-URL TARGET_REPO normalizes equal but must not mutate/dispatch.

        /work-resume requires exact owner/repo slug match; accepting URL forms
        would leave REWORK_DISPATCHED with zero resume matches.
        """
        url_body = SAMPLE_BODY.replace(
            "TARGET_REPO=datarelay-labs/datarelay-atlas",
            "TARGET_REPO=https://github.com/datarelay-labs/datarelay-atlas.git",
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=url_body)
        self.assertIn("canonical owner/repo slug", str(ctx.exception))

        ssh_body = SAMPLE_BODY.replace(
            "TARGET_REPO=datarelay-labs/datarelay-atlas",
            "TARGET_REPO=git@github.com:datarelay-labs/datarelay-atlas.git",
        )
        with self.assertRaises(ValidationError) as ctx:
            _render(body=ssh_body)
        self.assertIn("canonical owner/repo slug", str(ctx.exception))

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

    def test_basic_auth_and_url_userinfo_are_detected_and_redacted(self):
        from atlas.work_controller import (
            _contains_unsafe_secret,
            _looks_like_secret,
        )

        basic = "Authorization: Basic YWxpY2U6aHVudGVyMg=="
        db_url = "DATABASE_URL=postgresql://alice:hunter2@example.com/db"
        https_url = "https://alice:hunter2@example.com/path"
        for sample in (basic, db_url, https_url):
            self.assertTrue(_looks_like_secret(sample), sample)
            self.assertTrue(_contains_unsafe_secret(sample), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertTrue(_contains_unsafe_secret(sample), sample)
            self.assertFalse(_contains_unsafe_secret(redacted), redacted)
            self.assertNotIn("hunter2", redacted)
            self.assertNotIn("YWxpY2U6aHVudGVyMg==", redacted)
            if "Basic" in sample:
                self.assertIn("Basic <redacted>", redacted)
            else:
                self.assertIn("://<redacted>@", redacted)

    def test_type_annotation_colon_values_are_not_secrets(self):
        from atlas.work_controller import (
            _contains_unsafe_secret,
            _looks_like_secret,
        )

        annotation = "token: str"
        self.assertFalse(_looks_like_secret(annotation), annotation)
        self.assertFalse(_contains_unsafe_secret(annotation), annotation)
        self.assertEqual(redact_sensitive_audit_text(annotation), annotation)
        # Credential-shaped colon values remain detected.
        secretish = "access_token: bare-secret-value-12345"
        self.assertTrue(_looks_like_secret(secretish), secretish)

    def test_colon_credential_annotation_matrix(self):
        """Credential-like colon values fail closed; explicit annotations do not."""
        from atlas.work_controller import (
            _contains_unsafe_secret,
            _looks_like_secret,
        )

        unsafe = (
            "password: hunter2",
            "token: letmein",
            "password: hunter",
            "client_secret: secret",
            "password: DummySecret",
            "token: SampleValue",
            "client_secret: Example123",
            "token: sampleValue",
            "api_key: NotARealType",
            "password: Abc123",
            "token: Optional[DummySecret]",
            "password: str,hunter2",
            "token: Optional[str],letmein",
            "password: str;letmein",
            "token: str.hunter2",
            "apiKey: SecretStr,hunter2",
            "token: str | None,letmein",
            "password: List[str],abc123",
            "DATABASE_URL=postgresql://<redacted>@evil:secret@host/db",
            "DATABASE_URL=mysql://<redacted>@user:pass@db.example/app",
            "https://<redacted>@alice:hunter2@example.com/x",
        )
        leaked = (
            "hunter2",
            "letmein",
            "hunter",
            "DummySecret",
            "SampleValue",
            "Example123",
            "sampleValue",
            "NotARealType",
            "Abc123",
            "abc123",
            "evil:secret",
            "user:pass",
            "alice:hunter2",
        )
        for sample in unsafe:
            prompt = f"audit evidence\n{sample}\n"
            self.assertTrue(_looks_like_secret(sample), sample)
            self.assertTrue(_contains_unsafe_secret(sample), sample)
            self.assertTrue(_contains_unsafe_secret(prompt), sample)
            with self.assertRaises(ValidationError):
                sanitize_rework_findings(sample)
            redacted = redact_sensitive_audit_text(sample)
            self.assertIn("<redacted>", redacted)
            self.assertFalse(_contains_unsafe_secret(redacted), redacted)
            for token in leaked:
                if token in sample:
                    self.assertNotIn(token, redacted, sample)
            self.assertNotRegex(redacted, r":\s*secret\b")

        safe = (
            "token: str",
            "token: Optional",
            "password: None",
            "apiKey: SecretStr",
            "token: list[str]",
            "token: Optional[str]",
            "token: str | None",
            "password: SecretStr | None",
            "api_key: dict[str, int]",
            "token: list[dict[str, int]]",
            "token: List[str]",
            "token: str}",
            "DATABASE_URL=postgresql://<redacted>@host/db",
            "https://<redacted>@example.com/path",
            "Authorization: Bearer <redacted>",
            "Authorization: Basic <redacted>",
        )
        for sample in safe:
            prompt = f"audit evidence\n{sample}\n"
            self.assertFalse(_looks_like_secret(sample), sample)
            self.assertFalse(_contains_unsafe_secret(sample), sample)
            self.assertFalse(_contains_unsafe_secret(prompt), sample)
            self.assertEqual(sanitize_rework_findings(sample), sample)
            self.assertEqual(redact_sensitive_audit_text(sample), sample)

        already_redacted = "OPENAI_API_KEY=<redacted>"
        self.assertFalse(_contains_unsafe_secret(already_redacted))
        self.assertFalse(_contains_unsafe_secret(f"audit evidence\n{already_redacted}\n"))
        self.assertEqual(sanitize_rework_findings(already_redacted), already_redacted)
        self.assertEqual(redact_sensitive_audit_text(already_redacted), already_redacted)

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
            "author": {"login": "packet-author"},
            "authorAssociation": "NONE",
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
                "user": {"login": "packet-author"},
            }
        ]

    def _scan_pages(self, *pages: list[dict]) -> str:
        """``gh api --paginate --slurp`` body: a JSON array of issue pages."""
        if not pages:
            pages = (self._list_payload(),)
        return json.dumps(list(pages))

    @staticmethod
    def _is_issue_scan(argv: list[str]) -> bool:
        return (
            len(argv) >= 5
            and argv[:4] == ["gh", "api", "--paginate", "--slurp"]
            and "/issues?" in argv[4]
            and "state=open" in argv[4]
            and "per_page=100" in argv[4]
        )

    @staticmethod
    def _is_author_permission(argv: list[str]) -> bool:
        return (
            len(argv) == 3
            and argv[:2] == ["gh", "api"]
            and "/collaborators/" in argv[2]
            and argv[2].endswith("/permission")
        )

    def _author_permission_response(
        self, argv: list[str]
    ) -> subprocess.CompletedProcess[str] | None:
        if not self._is_author_permission(argv):
            return None
        script = getattr(self, "_author_permission_script", None)
        if script:
            mode = script.pop(0)
        else:
            mode = getattr(self, "_author_permission_mode", "write")
        by_login = getattr(self, "_author_permission_by_login", None)
        if by_login is not None:
            login = argv[2].split("/collaborators/", 1)[1].split("/permission", 1)[0]
            mode = by_login.get(login, "api_failure")
        if mode == "api_failure":
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="HTTP 500"
            )
        if mode == "non_json":
            return subprocess.CompletedProcess(
                argv, 0, stdout="not-json", stderr=""
            )
        if mode == "missing":
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        if mode == "non_object":
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"permission": mode}),
            stderr="",
        )

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
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
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
        self.assertEqual(len(calls), 6)  # list, select permission, view, mutate permission, recheck, edit
        self.assertTrue(self._is_issue_scan(calls[0]))
        self.assertNotIn("--limit", calls[0])
        self.assertEqual(
            calls[1],
            [
                "gh",
                "api",
                "repos/datarelay-labs/datarelay-atlas/collaborators/packet-author/permission",
            ],
        )
        self.assertEqual(
            calls[2],
            [
                "gh",
                "issue",
                "view",
                "12",
                "--repo",
                "datarelay-labs/datarelay-atlas",
                "--json",
                "number,title,state,body,updatedAt,author",
            ],
        )
        self.assertEqual(calls[1], calls[3])
        self.assertEqual(calls[2], calls[4])
        self.assertEqual(
            calls[5][:6],
            ["gh", "issue", "edit", "12", "--repo", "datarelay-labs/datarelay-atlas"],
        )
        joined = " ".join(calls[5])
        self.assertNotIn("$(rm -rf /)", joined)
        self.assertEqual(len(body_files), 1)
        self.assertIn("fix gaps", body_files[0])
        self.assertIn("do not $(rm -rf /)", body_files[0])
        self.assertFalse(Path(calls[5][calls[5].index("--body-file") + 1]).exists())

    def test_author_permission_required_before_mutation(self):
        """Trusted permissions may edit; weaker, missing, or failed lookups must not."""
        edits: list[list[str]] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._view_body), stderr=""
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            self.fail(f"unexpected argv: {argv}")

        trusted = ("write", "maintain", "admin")
        for permission in trusted:
            edits.clear()
            self._author_permission_mode = permission
            self._view_body = self._payload(authorAssociation="NONE")
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
            self.assertEqual(len(edits), 1, permission)

        weaker = ("read", "triage", "none")
        for mode in weaker:
            edits.clear()
            self._author_permission_mode = mode
            self._view_body = self._payload(authorAssociation="OWNER")
            with self.assertRaises(ValidationError) as ctx:
                GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                    **self._rework_kwargs(findings="x")
                )
            self.assertIn("no trusted ACTIVE Work Packet", str(ctx.exception))
            self.assertEqual(edits, [], mode)

        unverifiable = ("missing", "unknown", "api_failure", "non_json", "non_object")
        for mode in unverifiable:
            edits.clear()
            self._author_permission_mode = mode
            self._view_body = self._payload(authorAssociation="OWNER")
            with self.assertRaises(ValidationError) as ctx:
                GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                    **self._rework_kwargs(findings="x")
                )
            self.assertIn("WORK_PACKET_AUTHOR_UNTRUSTED", str(ctx.exception))
            self.assertEqual(edits, [], mode)

        edits.clear()
        self._author_permission_mode = "admin"
        missing_author = self._payload(authorAssociation="OWNER")
        missing_author.pop("author")
        self._view_body = missing_author
        with self.assertRaises(ValidationError) as ctx:
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
        self.assertIn("WORK_PACKET_AUTHOR_UNTRUSTED", str(ctx.exception))
        self.assertEqual(edits, [])

    def test_trusted_author_selection_ignores_weaker_duplicates(self):
        """An outsider duplicate must not create ambiguity or authorize itself."""
        edits: list[str] = []
        trusted_body = SAMPLE_BODY
        outsider_body = SAMPLE_BODY.replace(
            "OWNER_INTENT=Complete the AWC PoC safely.",
            "OWNER_INTENT=Outsider duplicate.",
        )

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(*self._pages), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits.append("edit")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            self.fail(f"unexpected argv: {argv}")

        self._author_permission_by_login = {
            "packet-author": "write",
            "outsider": "read",
        }
        self._pages = (
            [
                {
                    "number": 12,
                    "title": "[AI Work] trusted",
                    "state": "open",
                    "body": trusted_body,
                    "user": {"login": "packet-author"},
                    "author_association": "NONE",
                },
                {
                    "number": 40,
                    "title": "[AI Work] outsider",
                    "state": "open",
                    "body": outsider_body,
                    "user": {"login": "outsider"},
                    "author_association": "OWNER",
                },
            ],
        )
        GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
            **self._rework_kwargs(findings="x")
        )
        self.assertEqual(edits, ["edit"])

        edits.clear()
        self._author_permission_by_login = {"outsider": "none"}
        self._pages = (
            [
                {
                    "number": 12,
                    "title": "[AI Work] outsider only",
                    "state": "open",
                    "body": SAMPLE_BODY,
                    "user": {"login": "outsider"},
                    "author_association": "OWNER",
                }
            ],
        )
        with self.assertRaises(ValidationError) as ctx:
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
        self.assertIn("no trusted ACTIVE Work Packet", str(ctx.exception))
        self.assertEqual(edits, [])

    def test_two_trusted_packets_and_unverifiable_candidates_fail_closed(self):
        edits: list[str] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(*self._pages), stderr=""
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits.append("edit")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            self.fail(f"must not mutate: {argv}")

        other = SAMPLE_BODY.replace(
            "OWNER_INTENT=Complete the AWC PoC safely.",
            "OWNER_INTENT=Second trusted packet.",
        )
        self._author_permission_by_login = {
            "packet-author": "admin",
            "other-author": "maintain",
        }
        self._pages = (
            [
                {
                    "number": 12,
                    "title": "[AI Work] one",
                    "state": "open",
                    "body": SAMPLE_BODY,
                    "user": {"login": "packet-author"},
                },
                {
                    "number": 99,
                    "title": "[AI Work] two",
                    "state": "open",
                    "body": other,
                    "user": {"login": "other-author"},
                },
            ],
        )
        with self.assertRaises(ValidationError) as ctx:
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
        self.assertIn("ambiguous ACTIVE Work Packets", str(ctx.exception))
        self.assertIn("#12", str(ctx.exception))
        self.assertIn("#99", str(ctx.exception))
        self.assertEqual(edits, [])

        self._author_permission_by_login = {"packet-author": "api_failure"}
        self._pages = (
            [
                {
                    "number": 12,
                    "title": "[AI Work] one",
                    "state": "open",
                    "body": SAMPLE_BODY,
                    "user": {"login": "packet-author"},
                }
            ],
        )
        for mode in ("api_failure", "non_json", "missing", "unknown"):
            self._author_permission_by_login = {"packet-author": mode}
            with self.assertRaises(ValidationError) as ctx:
                GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                    **self._rework_kwargs(findings="x")
                )
            self.assertIn("unverifiable", str(ctx.exception), mode)
            self.assertEqual(edits, [])

        self._pages = (
            [
                {
                    "number": 12,
                    "title": "[AI Work] one",
                    "state": "open",
                    "body": SAMPLE_BODY,
                    "author_association": "OWNER",
                }
            ],
        )
        with self.assertRaises(ValidationError) as ctx:
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
        self.assertIn("issue author missing", str(ctx.exception))
        self.assertEqual(edits, [])

    def test_permission_downgrade_blocks_mutation_after_selection(self):
        edits: list[str] = []
        self._author_permission_by_login = None
        self._author_permission_script = ["write", "read"]

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._payload()), stderr=""
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits.append("edit")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            self.fail(f"unexpected argv: {argv}")

        with self.assertRaises(ValidationError) as ctx:
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
        self.assertIn("WORK_PACKET_AUTHOR_UNTRUSTED", str(ctx.exception))
        self.assertEqual(edits, [])

    def test_later_page_trusted_duplicate_is_not_hidden_by_an_outsider(self):
        edits: list[str] = []
        other = SAMPLE_BODY.replace(
            "OWNER_INTENT=Complete the AWC PoC safely.",
            "OWNER_INTENT=Later page trusted packet.",
        )
        self._author_permission_by_login = {
            "packet-author": "write",
            "outsider": "triage",
            "other-author": "admin",
        }

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=self._scan_pages(
                        [
                            {
                                "number": 12,
                                "title": "[AI Work] trusted",
                                "state": "open",
                                "body": SAMPLE_BODY,
                                "user": {"login": "packet-author"},
                            },
                            {
                                "number": 40,
                                "title": "[AI Work] outsider",
                                "state": "open",
                                "body": SAMPLE_BODY.replace(
                                    "OWNER_INTENT=Complete the AWC PoC safely.",
                                    "OWNER_INTENT=Outsider on page one.",
                                ),
                                "user": {"login": "outsider"},
                                "author_association": "OWNER",
                            },
                        ],
                        [
                            {
                                "number": 99,
                                "title": "[AI Work] later",
                                "state": "open",
                                "body": other,
                                "user": {"login": "other-author"},
                            }
                        ],
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits.append("edit")
            self.fail(f"must not mutate: {argv}")

        with self.assertRaises(ValidationError) as ctx:
            GitHubWorkPacketAdapter(command_runner=runner).apply_rework_findings(
                **self._rework_kwargs(findings="x")
            )
        message = str(ctx.exception)
        self.assertIn("#12", message)
        self.assertIn("#99", message)
        self.assertNotIn("#40", message)
        self.assertEqual(edits, [])

    def test_concurrent_change_fails_closed(self):
        views = [
            self._payload(updatedAt="2026-09-22T01:00:00Z"),
            self._payload(
                body=SAMPLE_BODY + "\n<!-- concurrent -->\n",
                updatedAt="2026-09-22T01:00:05Z",
            ),
        ]

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
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
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
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
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                # Unique match points at #12, but configured issue is #10.
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
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
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=self._scan_pages(
                        self._list_payload(
                            {
                                "number": 12,
                                "title": "[AI Work] one",
                                "state": "OPEN",
                                "body": SAMPLE_BODY,
                                "user": {"login": "packet-author"},
                            },
                            {
                                "number": 99,
                                "title": "[AI Work] two",
                                "state": "OPEN",
                                "body": other,
                                "user": {"login": "other-author"},
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

    def test_uniqueness_scan_includes_later_pages_and_skips_pulls(self):
        """A match past the first page is visible; pull requests are not packets."""
        other = SAMPLE_BODY.replace(
            "OWNER_INTENT=Complete the AWC PoC safely.",
            "OWNER_INTENT=Later page packet.",
        )
        filler = {
            "number": 1,
            "title": "ordinary issue",
            "state": "open",
            "body": "not a work packet",
        }
        pull = {
            "number": 50,
            "title": "[AI Work] pull request",
            "state": "open",
            "body": SAMPLE_BODY,
            "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/50"},
        }

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=self._scan_pages(
                        [filler, pull],
                        [
                            {
                                "number": 12,
                                "title": "[AI Work] one",
                                "state": "open",
                                "body": SAMPLE_BODY,
                                "user": {"login": "packet-author"},
                            },
                            {
                                "number": 99,
                                "title": "[AI Work] two",
                                "state": "open",
                                "body": other,
                                "user": {"login": "other-author"},
                            },
                        ],
                    ),
                    stderr="",
                )
            self.fail(f"must not continue after later-page ambiguity: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertIn("#12", str(ctx.exception))
        self.assertIn("#99", str(ctx.exception))
        self.assertNotIn("#50", str(ctx.exception))

    def test_uniqueness_scan_fails_closed_on_malformed_page(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps([self._list_payload(), {"not": "a page"}]),
                    stderr="",
                )
            self.fail(f"must not continue after malformed scan: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        self.assertIn("not a JSON array", str(ctx.exception))

    def test_clone_url_target_repo_does_not_match_active_packet(self):
        """URL-form TARGET_REPO must not count as the unique ACTIVE packet."""
        url_body = SAMPLE_BODY.replace(
            "TARGET_REPO=datarelay-labs/datarelay-atlas",
            "TARGET_REPO=https://github.com/datarelay-labs/datarelay-atlas.git",
        )

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=self._scan_pages(
                        self._list_payload(
                            {
                                "number": 12,
                                "title": "[AI Work] url form",
                                "state": "OPEN",
                                "body": url_body,
                            }
                        )
                    ),
                    stderr="",
                )
            self.fail(f"must not mutate after noncanonical TARGET_REPO: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(**self._rework_kwargs(findings="x"))
        message = str(ctx.exception)
        self.assertIn("no ACTIVE Work Packet matches", message)

    def test_branchless_active_packet_counts_in_uniqueness(self):
        branchless = "\n".join(
            line for line in SAMPLE_BODY.splitlines() if not line.startswith("BRANCH=")
        )

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=self._scan_pages(
                        self._list_payload(
                            {
                                "number": 12,
                                "title": "[AI Work] branched",
                                "state": "OPEN",
                                "body": SAMPLE_BODY,
                                "user": {"login": "packet-author"},
                            },
                            {
                                "number": 77,
                                "title": "[AI Work] branchless",
                                "state": "OPEN",
                                "body": branchless,
                                "user": {"login": "other-author"},
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
            allowed = self._author_permission_response(argv)
            if allowed is not None:
                return allowed
            if self._is_issue_scan(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=self._scan_pages(), stderr=""
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

    def test_deeply_nested_json_escapes_detect_without_redos(self):
        import time

        from atlas.work_controller import _looks_like_secret

        # Five nested json.dumps layers previously hung detection (>15s).
        payload = {"password": "hunter2-deep-secret"}
        nested = json.dumps(payload)
        for _ in range(5):
            nested = json.dumps(nested)
        self.assertGreater(len(nested), 200)
        started = time.perf_counter()
        detected = _looks_like_secret(nested)
        elapsed = time.perf_counter() - started
        self.assertTrue(detected, nested[:200])
        self.assertLess(elapsed, 1.0, f"secret scan took {elapsed:.3f}s")


class CliWorkPacketAdapterSelectionTests(unittest.TestCase):
    def test_fixed_defaults_to_recording_adapter(self):
        from atlas import cli as atlas_cli
        from atlas.work_controller import (
            AuditOnlyCursorDispatcher,
            RecordingWorkPacketAdapter,
        )

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
            self.assertIsInstance(ctl.dispatcher, AuditOnlyCursorDispatcher)

    def test_audit_only_rework_does_not_claim_dispatch(self):
        """Recording + AuditOnlyCursorDispatcher must not claim REWORK_DISPATCHED."""
        from atlas.work_controller import (
            AuditOnlyCursorDispatcher,
            AuditResult,
            FixedAuditAdapter,
            RecordingWorkPacketAdapter,
            WorkController,
        )

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="gap")
                ),
                work_packet=RecordingWorkPacketAdapter(),
                dispatcher=AuditOnlyCursorDispatcher(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/x",
                worktree_path=str(worktree),
                expected_head="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                max_attempts=3,
            )
            outcome = ctl.handle_completion(
                {
                    "event_id": "evt-1",
                    "workstream": "awc-poc",
                    "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "branch": "feature/x",
                    "issue_number": 12,
                    "attempt": 1,
                }
            )
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertNotEqual(outcome["action"], "rework_dispatched")
            findings = str(ctl.show("awc-poc").get("last_findings") or "")
            self.assertIn("audit-only", findings.lower())

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

    def test_fixed_recording_with_spawn_dispatch_fails_closed(self):
        """Recording packet adapter must not pair with a real PTY dispatcher."""
        from atlas import cli as atlas_cli

        parser = build_parser()
        args = parser.parse_args(
            [
                "work-controller",
                "completion",
                "/tmp/event.json",
                "--audit-adapter",
                "fixed",
                "--audit-verdict",
                "REWORK",
                "--spawn-dispatch",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            args.data_root = tmp
            with self.assertRaises(ValidationError) as ctx:
                atlas_cli._controller_from_args(args)
            self.assertIn("spawn-dispatch", str(ctx.exception).lower())
            self.assertIn("github", str(ctx.exception).lower())

    def test_explicit_recording_with_spawn_dispatch_fails_closed(self):
        from atlas import cli as atlas_cli

        parser = build_parser()
        args = parser.parse_args(
            [
                "work-controller",
                "completion",
                "/tmp/event.json",
                "--audit-adapter",
                "codex",
                "--work-packet-adapter",
                "recording",
                "--spawn-dispatch",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            args.data_root = tmp
            with self.assertRaises(ValidationError) as ctx:
                atlas_cli._controller_from_args(args)
            self.assertIn("recording", str(ctx.exception).lower())

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
