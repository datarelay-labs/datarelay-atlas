"""Slice A host-worker regressions: descriptors, fixed argv, idle no-op."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from atlas.chat_audit import AuditControlPacket, MemoryCheckpointStore
from atlas.host_worker import (
    DEFAULT_CURSOR_RESUME_TIMEOUT_SEC,
    MAX_PROMPT_CHARS,
    HeadlessCursorDispatcher,
    HostWorkerConfig,
    ProjectDescriptor,
    build_headless_resume_argv,
    chat_domain_lock_path,
    chat_lock_path,
    host_worker_run_lock,
    load_host_worker_config,
    run_once,
    spawn_agent_argv,
)
from atlas.work_controller import PersistSession
from atlas.provenance import ValidationError


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
CHAT_ID = "cursorChat12345678"
REPO = "datarelay-labs/datarelay-atlas"
BRANCH = "feature/autonomous-local-supervisor-gpt56-audit"
HOST = "dev-atlas"
PROMPT = (
    "Packet datarelay-labs/datarelay-atlas "
    "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa. "
    "Apply only the bounded rework findings in this prompt. "
    "Do not treat prior chat history as authority."
)


def _git_runner(
    *,
    origin: str,
    head: str,
    porcelain: str,
    toplevel: str,
    branch: str = BRANCH,
):
    def runner(argv: list[str], cwd: str) -> str:
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return toplevel
        if argv == ["git", "remote", "get-url", "origin"]:
            return origin
        if argv == ["git", "branch", "--show-current"]:
            return branch
        if argv == ["git", "rev-parse", "HEAD"]:
            return head
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return porcelain
        raise AssertionError(argv)

    return runner


class HostWorkerSliceATests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.config = HostWorkerConfig(
            state_root=str(self.state_root.resolve()),
            host_id=HOST,
            projects=(
                ProjectDescriptor(
                    repository=REPO,
                    worktree=str(self.worktree),
                    cursor_chat_id=CHAT_ID,
                ),
            ),
        )

    def _descriptor_file(self, raw: dict | None = None) -> Path:
        path = self.root / "descriptors.json"
        payload = raw or {
            "schema_version": 1,
            "state_root": str(self.state_root),
            "host_id": HOST,
            "projects": [
                {
                    "repository": REPO,
                    "worktree": str(self.worktree),
                    "cursor_chat_id": CHAT_ID,
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_descriptor_projection_remains_content_bounded(self) -> None:
        loaded = load_host_worker_config(self._descriptor_file())
        projection = loaded.projects[0].github_projection()
        encoded = json.dumps(projection)
        self.assertNotIn(CHAT_ID, encoded)
        self.assertNotIn("cursor_chat_id", projection)
        self.assertNotIn("worktree", projection)

    def test_descriptor_rejects_unknown_execution_fields(self) -> None:
        payload = {
            "schema_version": 1,
            "state_root": str(self.state_root),
            "host_id": HOST,
            "projects": [
                {
                    "repository": REPO,
                    "worktree": str(self.worktree),
                    "cursor_chat_id": CHAT_ID,
                    "branch": BRANCH,
                }
            ],
        }
        with self.assertRaisesRegex(ValidationError, "unknown"):
            load_host_worker_config(self._descriptor_file(payload))

    def test_idle_noop_never_calls_cursor_or_git(self) -> None:
        def forbidden(*_args, **_kwargs):
            raise AssertionError("idle path must not call runtime effects")

        result = run_once(
            config=self.config,
            resume_requested=False,
            host_probe=lambda: HOST,
            git_runner=forbidden,
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
        )
        self.assertEqual(result["action"], "idle_noop")
        self.assertEqual(result["cursor_calls"], 0)

    def test_host_identity_mismatch_still_fails_closed_on_idle(self) -> None:
        with self.assertRaisesRegex(ValidationError, "host identity mismatch"):
            run_once(
                config=self.config,
                resume_requested=False,
                host_probe=lambda: "other-host",
            )

    def test_current_resume_is_retired_before_cursor_probe_or_spawn(self) -> None:
        calls = []

        def forbidden(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("retired runtime must not probe or spawn Cursor")

        with self.assertRaisesRegex(ValidationError, "CURSOR_RUNTIME_RETIRED"):
            run_once(
                config=self.config,
                resume_requested=True,
                repository=REPO,
                canonical_branch=BRANCH,
                expected_head=HEAD,
                prompt=PROMPT,
                git_runner=forbidden,
                spawn=forbidden,
                host_probe=forbidden,
                list_sessions=forbidden,
                list_processes=forbidden,
                cursor_opt_in=True,
            )
        self.assertEqual(calls, [])

    def test_headless_dispatcher_is_retired_before_git_or_spawn(self) -> None:
        calls = []

        def forbidden(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("retired dispatcher must not touch git/spawn")

        dispatcher = HeadlessCursorDispatcher(spawn=forbidden)
        with self.assertRaisesRegex(ValidationError, "CURSOR_RUNTIME_RETIRED"):
            dispatcher.resume(
                self.config.projects[0],
                branch=BRANCH,
                expected_head=HEAD,
                prompt=PROMPT,
                git_runner=forbidden,
                cursor_opt_in=True,
            )
        self.assertEqual(calls, [])
        self.assertEqual(dispatcher.invocations, [])

    def test_historical_argv_builder_is_non_effectful(self) -> None:
        argv = build_headless_resume_argv(
            CHAT_ID,
            workspace=str(self.worktree),
            prompt=PROMPT,
        )
        self.assertEqual(argv[0], "agent")
        self.assertIn("--resume", argv)
        self.assertNotIn("--create", argv)

    def test_resume_timeout_config_remains_bounded_metadata(self) -> None:
        payload = {
            "schema_version": 1,
            "state_root": str(self.state_root),
            "host_id": HOST,
            "cursor_resume_timeout_sec": 90,
            "projects": [
                {
                    "repository": REPO,
                    "worktree": str(self.worktree),
                    "cursor_chat_id": CHAT_ID,
                }
            ],
        }
        self.assertEqual(
            load_host_worker_config(self._descriptor_file(payload)).cursor_resume_timeout_sec,
            90,
        )
        payload["cursor_resume_timeout_sec"] = 1
        with self.assertRaises(ValidationError):
            load_host_worker_config(self._descriptor_file(payload))


if __name__ == "__main__":
    unittest.main()
