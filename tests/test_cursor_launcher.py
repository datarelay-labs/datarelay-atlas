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
    DispatchSpawnedButUnobservedError,
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
    def _clean_git(self, *, head: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", dirty: str = ""):
        def fake_git(argv: list[str], cwd: str) -> str:
            if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return cwd
            mapping = {
                ("git", "remote", "get-url", "origin"): (
                    "https://github.com/datarelay-labs/datarelay-atlas.git"
                ),
                ("git", "branch", "--show-current"): "feature/x",
                ("git", "rev-parse", "HEAD"): head,
                ("git", "status", "--porcelain", "--untracked-files=all"): dirty,
            }
            return mapping[tuple(argv)]

        return fake_git

    def _dispatch_request(self, worktree: str, **overrides) -> DispatchRequest:
        base = dict(
            workstream="awc",
            worktree_path=worktree,
            branch="feature/x",
            issue_number=12,
            attempt=2,
            repository="datarelay-labs/datarelay-atlas",
            expected_head="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        base.update(overrides)
        return DispatchRequest(**base)

    def test_build_persist_resume_command_canonical_argv(self):
        req = self._dispatch_request("/tmp/wt")
        self.assertEqual(
            build_persist_resume_command(req),
            ["agent", "persist", "--trust", "/work-resume"],
        )

    def test_build_persist_resume_command_rejects_divergent_prompt(self):
        req = self._dispatch_request("/tmp/wt", resume_prompt="/resume")
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
                git_runner=self._clean_git(),
                poll_interval_sec=0.01,
                poll_timeout_sec=1.0,
                sleeper=lambda _s: None,
            )
            result = dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertEqual(result.session_id, "target-new-1")
            self.assertEqual(
                result.command, ["agent", "persist", "--trust", "/work-resume"]
            )
            self.assertEqual(len(state["spawn_calls"]), 1)
            remaining_ids = {item.session_id for item in state["sessions"]}
            self.assertIn("unrelated-1", remaining_ids)
            self.assertEqual(state["stopped"], [])
            self.assertEqual(dispatcher.spawned_pids, [4242])

    def test_post_spawn_list_failure_raises_spawned_but_unobserved(self):
        """agent persist list failure after spawn ⇒ recoverable specialized error."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"spawn_calls": 0, "lists": 0}

            def list_sessions() -> list[PersistSession]:
                state["lists"] += 1
                if state["lists"] == 1:
                    return []  # baseline before spawn
                raise ValidationError("agent persist list failed: boom")

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                return 7777

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=self._clean_git(),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(DispatchSpawnedButUnobservedError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertEqual(ctx.exception.session_hint, "proc:7777")
            self.assertEqual(state["spawn_calls"], 1)
            self.assertEqual(dispatcher.spawned_pids, [7777])

    def test_pre_spawn_oserror_is_validation_error(self):
        """OSError before a live process exists must stay a boundary ValidationError."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()

            def spawn(command: list[str], worktree_path: str) -> int:
                raise FileNotFoundError("script not found")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=self._clean_git(),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertNotIsInstance(ctx.exception, DispatchSpawnedButUnobservedError)
            self.assertIn("spawn failed before start", str(ctx.exception))
            self.assertEqual(dispatcher.spawned_pids, [])

    def test_pre_spawn_list_sessions_oserror_is_validation_error(self):
        """Missing agent during baseline session discovery must stay ValidationError."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"spawn_calls": 0}

            def list_sessions() -> list[PersistSession]:
                raise FileNotFoundError("agent not found")

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                return 9999

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=self._clean_git(),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertNotIsInstance(ctx.exception, DispatchSpawnedButUnobservedError)
            self.assertIn("spawn failed before start", str(ctx.exception))
            self.assertEqual(state["spawn_calls"], 0)
            self.assertEqual(dispatcher.spawned_pids, [])

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
                git_runner=self._clean_git(),
                poll_interval_sec=0.01,
                poll_timeout_sec=1.0,
                sleeper=lambda _s: None,
            )
            result = dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertEqual(result.session_id, "proc:222")
            self.assertEqual(
                [item.session_id for item in state["sessions"]], ["unrelated-1"]
            )

    def test_dispatch_boundary_rejects_head_drift_without_spawn(self):
        """HEAD changes after audit/before dispatch ⇒ zero spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"spawn_calls": 0}

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                raise AssertionError("spawn must not run on head mismatch")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=self._clean_git(
                    head="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertIn("head mismatch", str(ctx.exception))
            self.assertEqual(state["spawn_calls"], 0)
            self.assertEqual(dispatcher.spawned_pids, [])

    def test_dispatch_boundary_rejects_dirty_tree_without_spawn(self):
        """Dirty porcelain at dispatch boundary ⇒ zero spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"spawn_calls": 0}

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                raise AssertionError("spawn must not run on dirty tree")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=self._clean_git(dirty=" M dirty.py\n"),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertIn("dirty", str(ctx.exception).lower())
            self.assertEqual(state["spawn_calls"], 0)
            self.assertEqual(dispatcher.spawned_pids, [])

    def test_dispatch_boundary_revalidates_after_session_observation(self):
        """list_sessions mutates HEAD after capture ⇒ ValidationError, zero spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            expected = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            drifted = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            git_state = {"head": expected, "dirty": ""}
            state = {"spawn_calls": 0, "listed": 0}

            def mutable_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "https://github.com/datarelay-labs/datarelay-atlas.git"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): git_state["head"],
                    ("git", "status", "--porcelain", "--untracked-files=all"): git_state["dirty"],
                }
                return mapping[tuple(argv)]

            def list_sessions() -> list[PersistSession]:
                state["listed"] += 1
                # Observation happens before final validation; mutate here.
                git_state["head"] = drifted
                return []

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                raise AssertionError("spawn must not run after observation drift")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=mutable_git,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(
                    self._dispatch_request(str(target), expected_head=expected)
                )
            self.assertIn("head mismatch", str(ctx.exception))
            self.assertGreaterEqual(state["listed"], 1)
            self.assertEqual(state["spawn_calls"], 0)
            self.assertEqual(dispatcher.spawned_pids, [])

    def test_dispatch_boundary_revalidates_after_proc_observation_dirtiness(self):
        """list_target_procs dirties porcelain after capture ⇒ zero spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            expected = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            git_state = {"head": expected, "dirty": ""}
            state = {"spawn_calls": 0, "procs": 0}

            def mutable_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "https://github.com/datarelay-labs/datarelay-atlas.git"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): git_state["head"],
                    ("git", "status", "--porcelain", "--untracked-files=all"): git_state["dirty"],
                }
                return mapping[tuple(argv)]

            def list_procs(worktree_path: str) -> list[tuple[int, str]]:
                state["procs"] += 1
                git_state["dirty"] = " M raced.py\n"
                return []

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                raise AssertionError("spawn must not run after dirty observation race")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=list_procs,
                spawn=spawn,
                git_runner=mutable_git,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(
                    self._dispatch_request(str(target), expected_head=expected)
                )
            self.assertIn("dirty", str(ctx.exception).lower())
            self.assertGreaterEqual(state["procs"], 1)
            self.assertEqual(state["spawn_calls"], 0)
            self.assertEqual(dispatcher.spawned_pids, [])

    def test_dispatch_boundary_rejects_untracked_when_plain_porcelain_empty(self):
        """Forced untracked reporting at spawn boundary ⇒ zero spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"spawn_calls": 0, "seen": []}

            def fake_git(argv: list[str], cwd: str) -> str:
                state["seen"].append(list(argv))
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                if argv == ["git", "status", "--porcelain"]:
                    return ""
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "https://github.com/datarelay-labs/datarelay-atlas.git"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): (
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    ),
                    ("git", "status", "--porcelain", "--untracked-files=all"): (
                        "?? sneak.py\n"
                    ),
                }
                return mapping[tuple(argv)]

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                raise AssertionError("spawn must not run on forced-untracked dirty")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=fake_git,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertIn("dirty", str(ctx.exception).lower())
            self.assertIn(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                state["seen"],
            )
            self.assertEqual(state["spawn_calls"], 0)

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
                        tmp_path = state_path.with_name(state_path.name + ".tmp")
                        tmp_path.write_text(json.dumps(sessions))
                        tmp_path.replace(state_path)
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
                    list_target_procs=lambda _wt: [],
                    git_runner=self._clean_git(),
                    poll_interval_sec=0.05,
                    poll_timeout_sec=2.0,
                )
                result = dispatcher.start_resume(self._dispatch_request(str(target)))
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
