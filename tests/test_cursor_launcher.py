"""Cursor persist launcher contract and isolation regressions."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from atlas.provenance import ValidationError
from atlas.work_controller import (
    DispatchRequest,
    PersistSession,
    PtyPersistCursorDispatcher,
    build_persist_resume_command,
    parse_persist_list,
    sessions_for_worktree,
)


SAMPLE_PERSIST_LIST = """\
4 persistent sessions:

Task: Other Work
  Status: Attached (1 client)
  Session: cursor-other-aaaa
  Chat ID: 11111111-1111-1111-1111-111111111111
  Workspace: /tmp/unrelated-worktree-a
  Attach: agent persist attach cursor-other-aaaa

Task: Target Work
  Status: Detached (running in background)
  Session: cursor-target-bbbb
  Chat ID: -
  Workspace: /tmp/target-worktree
  Attach: agent persist attach cursor-target-bbbb
"""


class CursorLauncherTests(unittest.TestCase):
    def test_build_persist_resume_command_canonical_argv(self):
        req = DispatchRequest(
            workstream="awc",
            worktree_path="/tmp/wt",
            branch="feature/x",
            issue_number=12,
            attempt=2,
        )
        self.assertEqual(
            build_persist_resume_command(req),
            ["agent", "persist", "--trust", "/work-resume"],
        )

    def test_build_persist_resume_command_rejects_divergent_prompt(self):
        req = DispatchRequest(
            workstream="awc",
            worktree_path="/tmp/wt",
            branch="feature/x",
            issue_number=12,
            attempt=2,
            resume_prompt="/resume",
        )
        with self.assertRaises(ValidationError):
            build_persist_resume_command(req)

    def test_parse_persist_list_and_worktree_filter(self):
        sessions = parse_persist_list(SAMPLE_PERSIST_LIST)
        self.assertEqual(len(sessions), 2)
        target = sessions_for_worktree(sessions, "/tmp/target-worktree")
        self.assertEqual([item.session_id for item in target], ["cursor-target-bbbb"])
        other = sessions_for_worktree(sessions, "/tmp/unrelated-worktree-a")
        self.assertEqual([item.session_id for item in other], ["cursor-other-aaaa"])

    def test_pty_dispatcher_observes_new_target_session_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            unrelated = Path(tmp) / "unrelated-wt"
            target.mkdir()
            unrelated.mkdir()
            state = {
                "sessions": [
                    PersistSession(
                        session_id="unrelated-1",
                        workspace=str(unrelated.resolve()),
                        status="Attached",
                        task="Keep Me",
                    )
                ],
                "spawn_calls": [],
                "stopped": [],
            }

            def list_sessions() -> list[PersistSession]:
                return list(state["sessions"])

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"].append(
                    {"command": list(command), "cwd": worktree_path}
                )
                self.assertEqual(
                    command, ["agent", "persist", "--trust", "/work-resume"]
                )
                self.assertEqual(worktree_path, str(target.resolve()))
                state["sessions"].append(
                    PersistSession(
                        session_id="target-new-1",
                        workspace=str(target.resolve()),
                        status="Detached",
                        task="Rework",
                    )
                )
                return 4242

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                poll_interval_sec=0.01,
                poll_timeout_sec=1.0,
                sleeper=lambda _s: None,
            )
            result = dispatcher.start_resume(
                DispatchRequest(
                    workstream="awc",
                    worktree_path=str(target),
                    branch="feature/x",
                    issue_number=12,
                    attempt=2,
                )
            )
            self.assertEqual(result.session_id, "target-new-1")
            self.assertEqual(
                result.command, ["agent", "persist", "--trust", "/work-resume"]
            )
            self.assertEqual(len(state["spawn_calls"]), 1)
            remaining_ids = {item.session_id for item in state["sessions"]}
            self.assertIn("unrelated-1", remaining_ids)
            self.assertEqual(state["stopped"], [])
            self.assertEqual(dispatcher.spawned_pids, [4242])

    def test_pty_dispatcher_falls_back_to_target_process_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            unrelated = Path(tmp) / "unrelated-wt"
            target.mkdir()
            unrelated.mkdir()
            state = {
                "sessions": [
                    PersistSession(
                        session_id="unrelated-1",
                        workspace=str(unrelated.resolve()),
                        status="Attached",
                        task="Keep Me",
                    )
                ],
                "procs": [
                    (111, f"agent persist --trust /work-resume cwd={unrelated}")
                ],
            }

            def list_sessions() -> list[PersistSession]:
                return list(state["sessions"])

            def list_procs(worktree_path: str) -> list[tuple[int, str]]:
                if Path(worktree_path).resolve() != target.resolve():
                    return []
                return list(state["procs"]) if state.get("target_ready") else []

            def spawn(command: list[str], worktree_path: str) -> int:
                self.assertEqual(
                    command, ["agent", "persist", "--trust", "/work-resume"]
                )
                state["target_ready"] = True
                state["procs"] = [
                    (111, "unrelated keep"),
                    (222, "agent persist --trust /work-resume"),
                ]
                return 222

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=list_procs,
                spawn=spawn,
                poll_interval_sec=0.01,
                poll_timeout_sec=1.0,
                sleeper=lambda _s: None,
            )
            result = dispatcher.start_resume(
                DispatchRequest(
                    workstream="awc",
                    worktree_path=str(target),
                    branch="feature/x",
                    issue_number=12,
                    attempt=2,
                )
            )
            self.assertEqual(result.session_id, "proc:222")
            self.assertEqual(
                [item.session_id for item in state["sessions"]], ["unrelated-1"]
            )

    def test_fake_agent_integration_creates_only_target_session(self):
        """End-to-end launcher against a fake `agent` on PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            unrelated = Path(tmp) / "unrelated-wt"
            bin_dir = Path(tmp) / "bin"
            state_file = Path(tmp) / "sessions.json"
            target.mkdir()
            unrelated.mkdir()
            bin_dir.mkdir()
            state_file.write_text(
                '[{"session_id":"unrelated-keep","workspace":"%s","status":"Attached","task":"Other"}]\n'
                % str(unrelated.resolve()),
                encoding="utf-8",
            )
            fake_agent = bin_dir / "agent"
            fake_agent.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json, sys
                    from pathlib import Path
                    state_path = Path({str(state_file)!r})
                    argv = sys.argv[1:]
                    sessions = json.loads(state_path.read_text())
                    if argv[:1] == ["persist"] and argv[1:2] == ["list"]:
                        print(f"{{len(sessions)}} persistent sessions:")
                        for item in sessions:
                            print(f"Task: {{item.get('task','')}}")
                            print(f"  Status: {{item.get('status','')}}")
                            print(f"  Session: {{item['session_id']}}")
                            print(f"  Chat ID: -")
                            print(f"  Workspace: {{item['workspace']}}")
                            print(f"  Attach: agent persist attach {{item['session_id']}}")
                            print()
                        raise SystemExit(0)
                    if argv[:3] == ["persist", "--trust", "/work-resume"]:
                        cwd = str(Path.cwd().resolve())
                        sessions.append(
                            {{
                                "session_id": "fake-target-1",
                                "workspace": cwd,
                                "status": "Detached",
                                "task": "Rework",
                            }}
                        )
                        state_path.write_text(json.dumps(sessions))
                        raise SystemExit(0)
                    print("unexpected argv", argv, file=sys.stderr)
                    raise SystemExit(2)
                    """
                ),
                encoding="utf-8",
            )
            fake_agent.chmod(fake_agent.stat().st_mode | stat.S_IEXEC)

            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{original_path}"
            try:
                before = json.loads(state_file.read_text(encoding="utf-8"))
                dispatcher = PtyPersistCursorDispatcher(
                    poll_interval_sec=0.05,
                    poll_timeout_sec=2.0,
                )
                result = dispatcher.start_resume(
                    DispatchRequest(
                        workstream="awc",
                        worktree_path=str(target),
                        branch="feature/x",
                        issue_number=12,
                        attempt=2,
                    )
                )
                after = json.loads(state_file.read_text(encoding="utf-8"))
            finally:
                os.environ["PATH"] = original_path

            self.assertEqual(result.session_id, "fake-target-1")
            self.assertEqual(
                [item["session_id"] for item in before], ["unrelated-keep"]
            )
            self.assertEqual(
                sorted(item["session_id"] for item in after),
                ["fake-target-1", "unrelated-keep"],
            )
            unrelated_row = next(
                item for item in after if item["session_id"] == "unrelated-keep"
            )
            self.assertEqual(unrelated_row["workspace"], str(unrelated.resolve()))


if __name__ == "__main__":
    unittest.main()
