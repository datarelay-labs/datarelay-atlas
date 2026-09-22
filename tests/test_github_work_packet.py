"""GitHub Work Packet mutation adapter regressions (ADR-0006 Decision 8)."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.cli import build_parser
from atlas.provenance import ValidationError
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    render_rework_work_packet_body,
    sanitize_rework_findings,
)


BRANCH = "feature/autonomous-work-controller-final-hardening"
HEAD_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

SAMPLE_BODY = f"""PACKET_VERSION=2
TARGET_REPO=datarelay-labs/datarelay-atlas
WORKSTREAM=autonomous-work-controller-poc
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

    def test_view_recheck_then_edit_with_body_file_argv(self):
        calls: list[list[str]] = []
        body_files: list[str] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
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
        adapter.apply_rework_findings(
            repository="datarelay-labs/datarelay-atlas",
            issue_number=12,
            branch=BRANCH,
            findings="fix gaps; do not $(rm -rf /)",
            attempt=2,
            head=HEAD_B,
        )
        self.assertEqual(len(calls), 3)  # view, recheck view, edit
        self.assertEqual(
            calls[0],
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
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(
            calls[2][:6],
            ["gh", "issue", "edit", "12", "--repo", "datarelay-labs/datarelay-atlas"],
        )
        joined = " ".join(calls[2])
        self.assertNotIn("$(rm -rf /)", joined)
        self.assertEqual(len(body_files), 1)
        self.assertIn("fix gaps", body_files[0])
        self.assertIn("do not $(rm -rf /)", body_files[0])
        self.assertFalse(Path(calls[2][calls[2].index("--body-file") + 1]).exists())

    def test_concurrent_change_fails_closed(self):
        views = [
            self._payload(updatedAt="2026-09-22T01:00:00Z"),
            self._payload(
                body=SAMPLE_BODY + "\n<!-- concurrent -->\n",
                updatedAt="2026-09-22T01:00:05Z",
            ),
        ]

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                payload = views.pop(0)
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            self.fail(f"edit must not run on conflict: {argv}")

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch=BRANCH,
                findings="x",
                attempt=2,
                head=HEAD_B,
            )
        self.assertIn("changed during mutation", str(ctx.exception))

    def test_edit_failure_raises_validation_error(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(self._payload()), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="edit denied"
            )

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch=BRANCH,
                findings="x",
                attempt=2,
                head=HEAD_B,
            )
        self.assertIn("edit denied", str(ctx.exception))

    def test_rejects_non_ai_work_issue(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
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
        with self.assertRaises(ValidationError):
            adapter.apply_rework_findings(
                repository="datarelay-labs/datarelay-atlas",
                issue_number=10,
                branch=BRANCH,
                findings="x",
                attempt=2,
                head=HEAD_B,
            )


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

    def test_codex_defaults_to_github_adapter(self):
        from atlas import cli as atlas_cli
        from atlas.work_controller import GitHubWorkPacketAdapter

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
            ctl = atlas_cli._controller_from_args(args)
            self.assertIsInstance(ctl.work_packet, GitHubWorkPacketAdapter)


if __name__ == "__main__":
    unittest.main()
