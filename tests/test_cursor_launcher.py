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
    DispatchSpawnCleanupUncertainError,
    PersistSession,
    PtyPersistCursorDispatcher,
    _is_agent_persist_trust_cmdline,
    build_persist_resume_command,
    parse_persist_list,
    sessions_for_worktree,
)


def _pass_resource_preflight() -> tuple[int, str]:
    return 0, "RESULT=PASS\nEXIT_CODE=0\nREASON=within thresholds\n"


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
        command = build_persist_resume_command(req)
        self.assertEqual(
            command,
            ["agent", "persist", "--force", "--trust", "/work-resume"],
        )
        self.assertNotEqual(
            command,
            ["agent", "--force", "persist", "--trust", "/work-resume"],
        )
        self.assertNotIn("--print", command)
        self.assertNotIn("-p", command)
        self.assertIn("--trust", command)
        self.assertEqual(command[-1], "/work-resume")

    def test_build_persist_resume_command_rejects_divergent_prompt(self):
        req = self._dispatch_request("/tmp/wt", resume_prompt="/resume")
        with self.assertRaises(ValidationError):
            build_persist_resume_command(req)

    def test_resource_preflight_pass_and_warn_allow_spawn(self):
        from atlas.work_controller import interpret_resource_preflight

        allowed, result, _reason = interpret_resource_preflight(
            0, "RESULT=PASS\nEXIT_CODE=0\nREASON=within thresholds\n"
        )
        self.assertTrue(allowed)
        self.assertEqual(result, "PASS")
        allowed, result, _reason = interpret_resource_preflight(
            0, "RESULT=WARN\nEXIT_CODE=0\nREASON=session warning\n"
        )
        self.assertTrue(allowed)
        self.assertEqual(result, "WARN")

        for label, output, code in (
            ("warn", "RESULT=WARN\nEXIT_CODE=0\nREASON=session warning\n", 0),
            ("pass", "RESULT=PASS\nEXIT_CODE=0\nREASON=within thresholds\n", 0),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "target-wt"
                    target.mkdir()
                    state = {"spawn": 0, "stopped": []}

                    def preflight() -> tuple[int, str]:
                        return code, output

                    def spawn(command: list[str], worktree_path: str) -> int:
                        state["spawn"] += 1
                        self.assertEqual(
                            command,
                            ["agent", "persist", "--force", "--trust", "/work-resume"],
                        )
                        return 4242

                    dispatcher = PtyPersistCursorDispatcher(
                        list_sessions=lambda: [
                            PersistSession(
                                session_id="keep-me",
                                workspace="/tmp/unrelated",
                            )
                        ],
                        list_target_procs=lambda _wt: [],
                        spawn=spawn,
                        git_runner=self._clean_git(),
                        resource_preflight=preflight,
                        terminate_process_group=lambda pid: state["stopped"].append(pid),
                        poll_interval_sec=0.01,
                        poll_timeout_sec=0.05,
                        sleeper=lambda _s: None,
                    )
                    with self.assertRaises(DispatchSpawnedButUnobservedError):
                        dispatcher.start_resume(self._dispatch_request(str(target)))
                    self.assertEqual(state["spawn"], 1)
                    self.assertEqual(dispatcher.last_resource_preflight["result"], label.upper())
                    self.assertEqual(state["stopped"], [4242])

    def test_resource_preflight_block_and_failure_spawn_nothing(self):
        from atlas.work_controller import (
            ResourcePreflightBlocked,
            interpret_resource_preflight,
        )

        blocked, result, reason = interpret_resource_preflight(
            2, "RESULT=BLOCK\nEXIT_CODE=2\nREASON=session pressure\n"
        )
        self.assertFalse(blocked)
        self.assertEqual(result, "BLOCK")
        self.assertIn("session pressure", reason)
        unknown, result, _reason = interpret_resource_preflight(0, "not a report")
        self.assertFalse(unknown)
        self.assertEqual(result, "BLOCK")

        cases = (
            ("block", 2, "RESULT=BLOCK\nEXIT_CODE=2\nREASON=session pressure\n"),
            ("tool-failure", 3, ""),
            ("raises", None, ""),
        )
        for label, code, output in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "target-wt"
                    target.mkdir()
                    existing = [
                        PersistSession(session_id="keep-me", workspace="/tmp/unrelated")
                    ]
                    state = {"spawn": 0, "stopped": []}

                    def _refuse_spawn(_command: list[str], _worktree: str) -> int:
                        state["spawn"] += 1
                        return 1

                    def preflight(
                        code: int | None = code, output: str = output
                    ) -> tuple[int, str]:
                        if code is None:
                            raise OSError("preflight tool missing")
                        return code, output

                    dispatcher = PtyPersistCursorDispatcher(
                        list_sessions=lambda: list(existing),
                        list_target_procs=lambda _wt: [(111, "unrelated")],
                        spawn=_refuse_spawn,
                        git_runner=self._clean_git(),
                        resource_preflight=preflight,
                        terminate_process_group=lambda pid: state["stopped"].append(pid),
                        poll_interval_sec=0.01,
                        poll_timeout_sec=0.05,
                        sleeper=lambda _s: None,
                    )
                    with self.assertRaises(ResourcePreflightBlocked):
                        dispatcher.start_resume(self._dispatch_request(str(target)))
                    self.assertEqual(state["spawn"], 0)
                    self.assertEqual(state["stopped"], [])
                    self.assertEqual(dispatcher.spawned_pids, [])
                    self.assertEqual(existing[0].session_id, "keep-me")
                    self.assertEqual(
                        dispatcher.last_resource_preflight["result"], "BLOCK"
                    )

    def test_identity_recheck_still_runs_immediately_before_spawn(self):
        """HEAD drift during preflight is caught before spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            expected = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            drifted = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            git_state = {"head": expected}
            state = {"spawn": 0}

            def mutable_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "https://github.com/datarelay-labs/datarelay-atlas.git"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): git_state["head"],
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
                }
                return mapping[tuple(argv)]

            def preflight() -> tuple[int, str]:
                git_state["head"] = drifted
                return 0, "RESULT=PASS\nEXIT_CODE=0\nREASON=within thresholds\n"

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=lambda _cmd, _wt: state.__setitem__("spawn", 1) or 1,
                git_runner=mutable_git,
                resource_preflight=preflight,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(
                    self._dispatch_request(str(target), expected_head=expected)
                )
            self.assertIn("head mismatch", str(ctx.exception))
            self.assertEqual(state["spawn"], 0)

    def test_preflight_script_resolution_is_configurable(self):
        import inspect

        from atlas.work_controller import resolve_cursor_resource_preflight_script

        source = inspect.getsource(resolve_cursor_resource_preflight_script)
        self.assertNotIn("/home/aella/engineering-system", source)
        saved = {
            name: os.environ.pop(name, None)
            for name in (
                "ENGINEERING_SYSTEM_ROOT",
                "ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD",
                "ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT",
            )
        }
        try:
            self.assertIsNone(resolve_cursor_resource_preflight_script())
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = root / "tools" / "cursor-resource-preflight.py"
                alias = root / "alias-preflight.py"
                script.parent.mkdir()
                script.write_text("# preflight\n", encoding="utf-8")
                alias.write_text("# alias\n", encoding="utf-8")
                os.environ["ENGINEERING_SYSTEM_ROOT"] = tmp
                self.assertEqual(
                    resolve_cursor_resource_preflight_script(), script
                )
                os.environ["ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT"] = str(
                    alias
                )
                self.assertEqual(
                    resolve_cursor_resource_preflight_script(), alias
                )
                missing = root / "missing.py"
                os.environ["ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT"] = str(
                    missing
                )
                self.assertIsNone(resolve_cursor_resource_preflight_script())
                os.environ.pop("ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT")
                os.environ.pop("ENGINEERING_SYSTEM_ROOT")
                os.environ["ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD"] = str(script)
                self.assertEqual(
                    resolve_cursor_resource_preflight_script(), script
                )
                os.environ["ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT"] = str(
                    alias
                )
                os.environ["ENGINEERING_SYSTEM_ROOT"] = tmp
                self.assertEqual(
                    resolve_cursor_resource_preflight_script(), script
                )
                os.environ["ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD"] = str(missing)
                self.assertIsNone(resolve_cursor_resource_preflight_script())
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

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
                    command, ["agent", "persist", "--force", "--trust", "/work-resume"]
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
                resource_preflight=_pass_resource_preflight,
                owned_session_ids=lambda pid, candidates: (
                    {"target-new-1"} if pid == 4242 and "target-new-1" in candidates else set()
                ),
                poll_interval_sec=0.01,
                poll_timeout_sec=1.0,
                sleeper=lambda _s: None,
            )
            result = dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertEqual(result.session_id, "target-new-1")
            self.assertEqual(
                result.command, ["agent", "persist", "--force", "--trust", "/work-resume"]
            )
            self.assertEqual(len(state["spawn_calls"]), 1)
            remaining_ids = {item.session_id for item in state["sessions"]}
            self.assertIn("unrelated-1", remaining_ids)
            self.assertEqual(state["stopped"], [])
            self.assertEqual(dispatcher.spawned_pids, [4242])

    def test_unattributed_same_worktree_session_is_not_dispatch_success(self):
        """A new same-worktree session that is not the owned spawn is not success."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {
                "sessions": [
                    PersistSession(
                        session_id="already-there",
                        workspace=str(target.resolve()),
                        status="Attached",
                        task="Keep Me",
                    )
                ],
                "terminated": [],
            }

            def list_sessions() -> list[PersistSession]:
                return list(state["sessions"])

            def spawn(command: list[str], worktree_path: str) -> int:
                state["sessions"].append(
                    PersistSession(
                        session_id="intruder-1",
                        workspace=str(target.resolve()),
                        status="Detached",
                        task="Someone Else",
                    )
                )
                return 5150

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=self._clean_git(),
                resource_preflight=_pass_resource_preflight,
                owned_session_ids=lambda _pid, _candidates: set(),
                terminate_process_group=lambda pid: state["terminated"].append(pid),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(DispatchSpawnedButUnobservedError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertIn("not the owned spawn", str(ctx.exception))
            self.assertIn("intruder-1", str(ctx.exception))
            self.assertEqual(state["terminated"], [5150])
            self.assertEqual(
                [item.session_id for item in state["sessions"]],
                ["already-there", "intruder-1"],
            )

    def test_spawn_tree_names_only_descendant_session_ids(self):
        import subprocess
        import sys

        from atlas.work_controller import persist_session_ids_in_spawn_tree

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "owned-session-1"]
        )
        try:
            found = persist_session_ids_in_spawn_tree(
                proc.pid, {"owned-session-1", "other-session"}
            )
            self.assertEqual(found, {"owned-session-1"})
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_post_spawn_list_failure_raises_spawned_but_unobserved(self):
        """agent persist list failure after spawn ⇒ recoverable specialized error."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"spawn_calls": 0, "lists": 0, "terminated": []}

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
                terminate_process_group=lambda pid: state["terminated"].append(pid),
                git_runner=self._clean_git(),
                resource_preflight=_pass_resource_preflight,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(DispatchSpawnedButUnobservedError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertEqual(ctx.exception.session_hint, "proc:7777")
            self.assertEqual(state["spawn_calls"], 1)
            self.assertEqual(dispatcher.spawned_pids, [7777])
            self.assertEqual(state["terminated"], [7777])

    def test_observation_timeout_terminates_spawn_group(self):
        """Unobserved timeout must stop the owned process group before raise."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            state = {"terminated": []}

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=lambda _cmd, _wt: 4242,
                terminate_process_group=lambda pid: state["terminated"].append(pid),
                git_runner=self._clean_git(),
                resource_preflight=_pass_resource_preflight,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(DispatchSpawnedButUnobservedError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertEqual(ctx.exception.session_hint, "proc:4242")
            self.assertEqual(state["terminated"], [4242])

    def test_cleanup_permission_failure_stays_uncertain(self):
        """A failed owned-group termination is not a confirmed unobserved stop."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()

            def terminate(pid: int) -> None:
                raise PermissionError("operation not permitted")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=lambda _cmd, _wt: 4242,
                terminate_process_group=terminate,
                git_runner=self._clean_git(),
                resource_preflight=_pass_resource_preflight,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(DispatchSpawnCleanupUncertainError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertNotIsInstance(
                ctx.exception, DispatchSpawnedButUnobservedError
            )
            self.assertEqual(ctx.exception.session_hint, "proc:4242")
            self.assertIn("cleanup uncertain", str(ctx.exception))
            self.assertIn("operation not permitted", ctx.exception.cleanup_error)

    def test_terminate_permission_failure_does_not_claim_exit(self):
        from unittest.mock import patch

        from atlas.work_controller import (
            SpawnCleanupUncertainError,
            terminate_spawned_process_group,
        )

        with patch(
            "atlas.work_controller.os.killpg", side_effect=PermissionError("denied")
        ), patch(
            "atlas.work_controller.os.kill", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(SpawnCleanupUncertainError):
                terminate_spawned_process_group(4242, wait_sec=0.01)

    def test_terminate_returns_when_owned_pid_is_already_gone(self):
        from unittest.mock import patch

        from atlas.work_controller import terminate_spawned_process_group

        with patch(
            "atlas.work_controller.os.killpg", side_effect=ProcessLookupError()
        ), patch(
            "atlas.work_controller.os.kill", side_effect=ProcessLookupError()
        ):
            terminate_spawned_process_group(4242, wait_sec=0.01)

    def test_script_wrapper_cmdline_is_not_confirmed_agent(self):
        script_cmdline = (
            "script\x00-qec\x00agent persist --force --trust /work-resume\x00/dev/null\x00"
        )
        agent_cmdline = "agent\x00persist\x00--force\x00--trust\x00/work-resume\x00"
        wrong_order = "agent\x00--force\x00persist\x00--trust\x00/work-resume\x00"
        approval_mode = "agent\x00persist\x00--trust\x00/work-resume\x00"
        print_mode = "agent\x00persist\x00--force\x00-p\x00--trust\x00/work-resume\x00"
        self.assertFalse(_is_agent_persist_trust_cmdline(script_cmdline))
        self.assertTrue(_is_agent_persist_trust_cmdline(agent_cmdline))
        self.assertFalse(_is_agent_persist_trust_cmdline(wrong_order))
        self.assertFalse(_is_agent_persist_trust_cmdline(approval_mode))
        self.assertFalse(_is_agent_persist_trust_cmdline(print_mode))

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
                resource_preflight=_pass_resource_preflight,
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
                resource_preflight=_pass_resource_preflight,
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

    def test_process_only_observation_is_not_dispatch_success(self):
        """A transient target process without a persist-list session is not success."""
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
                "procs": [],
                "terminated": [],
            }

            def list_sessions() -> list[PersistSession]:
                return list(state["sessions"])

            def list_procs(worktree_path: str) -> list[tuple[int, str]]:
                if Path(worktree_path).resolve() != target.resolve():
                    return []
                observed = list(state["procs"])
                state["procs"] = []
                return observed

            def spawn(command: list[str], worktree_path: str) -> int:
                self.assertEqual(
                    command, ["agent", "persist", "--force", "--trust", "/work-resume"]
                )
                state["procs"] = [
                    (222, "agent persist --force --trust /work-resume"),
                ]
                return 222

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=list_sessions,
                list_target_procs=list_procs,
                spawn=spawn,
                git_runner=self._clean_git(),
                resource_preflight=_pass_resource_preflight,
                terminate_process_group=lambda pid: state["terminated"].append(pid),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(DispatchSpawnedButUnobservedError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertIn("persist list", str(ctx.exception))
            self.assertIn("diagnostic only", str(ctx.exception))
            self.assertEqual(state["terminated"], [222])
            self.assertIn("proc:222", dispatcher.last_process_observation)
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
                resource_preflight=_pass_resource_preflight,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(self._dispatch_request(str(target)))
            self.assertIn("head mismatch", str(ctx.exception))
            self.assertEqual(state["spawn_calls"], 0)
            self.assertEqual(dispatcher.spawned_pids, [])

    def test_dispatch_boundary_rejects_head_change_during_clean_check(self):
        """HEAD commit between identity and porcelain ⇒ zero spawn."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target-wt"
            target.mkdir()
            expected = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            drifted = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            git_state = {"head": expected}
            state = {"spawn_calls": 0}

            def mutable_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
                    git_state["head"] = drifted
                    return ""
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "https://github.com/datarelay-labs/datarelay-atlas.git"
                    ),
                    ("git", "branch", "--show-current"): "feature/x",
                    ("git", "rev-parse", "HEAD"): git_state["head"],
                }
                return mapping[tuple(argv)]

            def spawn(command: list[str], worktree_path: str) -> int:
                state["spawn_calls"] += 1
                raise AssertionError("spawn must not run after mid-check HEAD change")

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: [],
                list_target_procs=lambda _wt: [],
                spawn=spawn,
                git_runner=mutable_git,
                resource_preflight=_pass_resource_preflight,
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            with self.assertRaises(ValidationError) as ctx:
                dispatcher.start_resume(
                    self._dispatch_request(str(target), expected_head=expected)
                )
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
                resource_preflight=_pass_resource_preflight,
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
                resource_preflight=_pass_resource_preflight,
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
                resource_preflight=_pass_resource_preflight,
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
                resource_preflight=_pass_resource_preflight,
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
                    import json, os, sys
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
                    if argv[:4] == ["persist", "--force", "--trust", "/work-resume"]:
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
                        os.execv(
                            sys.executable,
                            [sys.executable, "-c", "import time; time.sleep(60)", "fake-target-1"],
                        )
                    print("unexpected argv", argv, file=sys.stderr)
                    raise SystemExit(2)
                    """
                ),
                encoding="utf-8",
            )
            fake_agent.chmod(fake_agent.stat().st_mode | stat.S_IEXEC)

            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{original_path}"
            dispatcher = None
            try:
                before = json.loads(state_file.read_text(encoding="utf-8"))
                dispatcher = PtyPersistCursorDispatcher(
                    list_target_procs=lambda _wt: [],
                    git_runner=self._clean_git(),
                    resource_preflight=_pass_resource_preflight,
                    poll_interval_sec=0.05,
                    poll_timeout_sec=2.0,
                )
                result = dispatcher.start_resume(self._dispatch_request(str(target)))
                after = json.loads(state_file.read_text(encoding="utf-8"))
            finally:
                os.environ["PATH"] = original_path
                if dispatcher is not None:
                    from atlas.work_controller import terminate_spawned_process_group

                    for pid in dispatcher.spawned_pids:
                        terminate_spawned_process_group(pid, wait_sec=0.2)

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
