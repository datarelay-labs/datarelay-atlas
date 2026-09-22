"""GitHub Work Packet mutation adapter regressions (ADR-0006 Decision 8)."""

from __future__ import annotations

import json
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


SAMPLE_BODY = """PACKET_VERSION=2
TARGET_REPO=datarelay-labs/datarelay-atlas
WORKSTREAM=autonomous-work-controller-poc
STATUS=ACTIVE
BRANCH=feature/autonomous-work-controller-final-hardening
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


class RenderReworkWorkPacketBodyTests(unittest.TestCase):
    def test_updates_sections_and_head(self):
        updated = render_rework_work_packet_body(
            SAMPLE_BODY,
            repository="datarelay-labs/datarelay-atlas",
            findings="fix gaps\nneed coverage",
            attempt=2,
            head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.assertIn(
            "LAST_VERIFIED_HEAD=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            updated,
        )
        self.assertIn("Controller audit verdict: REWORK", updated)
        self.assertIn("fix gaps", updated)
        self.assertIn("Address the REWORK findings", updated)
        self.assertIn("WORK_PACKET_MUTATION=PENDING_DISPATCH", updated)
        self.assertIn("## Constraints", updated)
        self.assertIn("Keep scope tight.", updated)
        self.assertIn("## Goal", updated)
        self.assertIn("Ship the PoC.", updated)

    def test_rejects_inactive_or_mismatched_target(self):
        inactive = SAMPLE_BODY.replace("STATUS=ACTIVE", "STATUS=PAUSED")
        with self.assertRaises(ValidationError):
            render_rework_work_packet_body(
                inactive,
                repository="datarelay-labs/datarelay-atlas",
                findings="x",
                attempt=2,
                head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )
        mismatched = SAMPLE_BODY.replace(
            "TARGET_REPO=datarelay-labs/datarelay-atlas",
            "TARGET_REPO=other-org/other-repo",
        )
        with self.assertRaises(ValidationError):
            render_rework_work_packet_body(
                mismatched,
                repository="datarelay-labs/datarelay-atlas",
                findings="x",
                attempt=2,
                head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )

    def test_sanitize_strips_controls_and_bounds(self):
        dirty = "ok\x00line\n" + ("a" * 5000)
        clean = sanitize_rework_findings(dirty, max_chars=100)
        self.assertNotIn("\x00", clean)
        self.assertIn("okline", clean.replace("\n", ""))
        self.assertTrue(clean.endswith("...[truncated]...\n") or "[truncated]" in clean)
        self.assertLessEqual(len(clean), 120)


class GitHubWorkPacketAdapterTests(unittest.TestCase):
    def test_view_then_edit_with_body_file_argv(self):
        calls: list[list[str]] = []
        body_files: list[str] = []

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            if argv[:3] == ["gh", "issue", "view"]:
                payload = {
                    "number": 12,
                    "title": "[AI Work] DRAtlas Autonomous Work Controller PoC",
                    "state": "OPEN",
                    "body": SAMPLE_BODY,
                }
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
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
            findings="fix gaps; do not $(rm -rf /)",
            attempt=2,
            head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.assertEqual(len(calls), 2)
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
                "number,title,state,body",
            ],
        )
        self.assertEqual(calls[1][:6], ["gh", "issue", "edit", "12", "--repo", "datarelay-labs/datarelay-atlas"])
        self.assertIn("--body-file", calls[1])
        # Findings appear only in the body file contents, never as shell text.
        joined = " ".join(calls[1])
        self.assertNotIn("$(rm -rf /)", joined)
        self.assertEqual(len(body_files), 1)
        self.assertIn("fix gaps", body_files[0])
        self.assertIn("do not $(rm -rf /)", body_files[0])
        self.assertFalse(Path(calls[1][calls[1].index("--body-file") + 1]).exists())

    def test_edit_failure_raises_validation_error(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                payload = {
                    "number": 12,
                    "title": "[AI Work] example",
                    "state": "OPEN",
                    "body": SAMPLE_BODY,
                }
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="edit denied"
            )

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError) as ctx:
            adapter.apply_rework_findings(
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                findings="x",
                attempt=2,
                head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )
        self.assertIn("edit denied", str(ctx.exception))

    def test_rejects_non_ai_work_issue(self):
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            payload = {
                "number": 10,
                "title": "[Roadmap] something else",
                "state": "OPEN",
                "body": SAMPLE_BODY,
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        with self.assertRaises(ValidationError):
            adapter.apply_rework_findings(
                repository="datarelay-labs/datarelay-atlas",
                issue_number=10,
                findings="x",
                attempt=2,
                head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
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
