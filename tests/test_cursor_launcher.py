from __future__ import annotations

import tempfile
import unittest

from atlas.provenance import ValidationError
from atlas.work_controller import (
    DispatchRequest,
    PtyPersistCursorDispatcher,
    SubprocessCursorDispatcher,
    _is_agent_persist_trust_cmdline,
    build_persist_resume_command,
    parse_persist_list,
    sessions_for_worktree,
)


SAMPLE_PERSIST_LIST = """\
2 persistent sessions:

Task: Other Work
  Status: Attached (1 client)
  Session: cursor-other-aaaa
  Workspace: /tmp/unrelated-worktree

Task: Target Work
  Status: Detached (running in background)
  Session: cursor-target-bbbb
  Workspace: /tmp/target-worktree
"""


class CursorLauncherTests(unittest.TestCase):
    def _request(self, worktree: str, **overrides) -> DispatchRequest:
        fields = {
            "workstream": "awc",
            "worktree_path": worktree,
            "branch": "feature/x",
            "issue_number": 12,
            "attempt": 2,
            "repository": "datarelay-labs/datarelay-atlas",
            "expected_head": "a" * 40,
            "cursor_opt_in": True,
        }
        fields.update(overrides)
        return DispatchRequest(**fields)

    def test_current_cursor_dispatchers_retire_before_all_external_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def forbidden(*_args, **_kwargs):
                calls.append(True)
                raise AssertionError("retired dispatcher must not call external boundary")

            request = self._request(tmp)
            subprocess_dispatcher = SubprocessCursorDispatcher(runner=forbidden)
            with self.assertRaisesRegex(ValidationError, "CURSOR_RUNTIME_RETIRED"):
                subprocess_dispatcher.start_resume(request)

            pty_dispatcher = PtyPersistCursorDispatcher(
                list_sessions=forbidden,
                spawn=forbidden,
                list_target_procs=forbidden,
                git_runner=forbidden,
                resource_preflight=forbidden,
            )
            with self.assertRaisesRegex(ValidationError, "CURSOR_RUNTIME_RETIRED"):
                pty_dispatcher.start_resume(request)

            self.assertEqual(calls, [])
            self.assertEqual(subprocess_dispatcher.requests, [])
            self.assertEqual(pty_dispatcher.requests, [])
            self.assertEqual(pty_dispatcher.spawned_pids, [])

    def test_retirement_precedes_legacy_cursor_opt_in_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self._request(tmp, cursor_opt_in=False)
            with self.assertRaisesRegex(ValidationError, "CURSOR_RUNTIME_RETIRED"):
                PtyPersistCursorDispatcher().start_resume(request)

    def test_historical_persist_command_builder_is_non_effectful(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self._request(tmp)
            self.assertEqual(
                build_persist_resume_command(request),
                ["agent", "persist", "--force", "--trust", "/work-resume"],
            )

    def test_historical_command_builder_rejects_prompt_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = self._request(tmp, resume_prompt="different")
            with self.assertRaises(ValidationError):
                build_persist_resume_command(request)

    def test_historical_persist_list_parser_remains_read_only(self):
        sessions = parse_persist_list(SAMPLE_PERSIST_LIST)
        self.assertEqual(len(sessions), 2)
        target = sessions_for_worktree(sessions, "/tmp/target-worktree")
        self.assertEqual([item.session_id for item in target], ["cursor-target-bbbb"])

    def test_historical_cmdline_classifier_is_read_only(self):
        self.assertTrue(
            _is_agent_persist_trust_cmdline(
                "agent\x00persist\x00--force\x00--trust\x00/work-resume\x00"
            )
        )
        self.assertFalse(_is_agent_persist_trust_cmdline("agent\x00--version\x00"))


if __name__ == "__main__":
    unittest.main()
