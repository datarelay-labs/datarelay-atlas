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
        self.addCleanup(self._cleanup_domain_lock)

    def _cleanup_domain_lock(self) -> None:
        path = chat_domain_lock_path(CHAT_ID)
        if path.exists():
            path.unlink()

    def _write_descriptor_file(self, raw: dict | None = None) -> Path:
        path = self.root / "descriptors.json"
        payload = raw or {
            "schema_version": 1,
            "state_root": str(self.state_root),
            "host_id": HOST,
            "projects": [
                {
                    "repository": f"https://github.com/{REPO}.git",
                    "worktree": str(self.worktree),
                    "cursor_chat_id": CHAT_ID,
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _git(self, **kwargs):
        fields = {
            "origin": f"https://github.com/{REPO}.git",
            "head": HEAD,
            "porcelain": "",
            "toplevel": str(self.worktree.resolve()),
        }
        fields.update(kwargs)
        return _git_runner(**fields)

    def _run(self, **kwargs):
        params = {
            "config": self.config,
            "resume_requested": False,
            "host_probe": lambda: HOST,
            "list_sessions": lambda: [],
            "list_processes": lambda _worktree: [],
        }
        params.update(kwargs)
        return run_once(**params)

    def _resume(self, **kwargs):
        params = {
            "resume_requested": True,
            "repository": REPO,
            "canonical_branch": BRANCH,
            "expected_head": HEAD,
            "git_runner": self._git(),
            "spawn": lambda argv, cwd: 0,
            "prompt": PROMPT,
        }
        params.update(kwargs)
        return self._run(**params)

    def test_descriptor_omits_chat_id_from_github_projection(self) -> None:
        loaded = load_host_worker_config(self._write_descriptor_file())
        self.assertEqual(loaded.projects[0].cursor_chat_id, CHAT_ID)
        self.assertEqual(loaded.host_id, HOST)
        projection = loaded.projects[0].github_projection()
        encoded = json.dumps(projection)
        self.assertNotIn("cursor_chat_id", projection)
        self.assertNotIn("host_id", projection)
        self.assertNotIn("worktree", projection)
        self.assertNotIn("branch", projection)
        self.assertNotIn(CHAT_ID, encoded)

    def test_descriptor_allowlist_rejects_unknown_and_branch(self) -> None:
        with self.assertRaises(ValidationError) as packet_number:
            load_host_worker_config(
                self._write_descriptor_file(
                    {
                        "schema_version": 1,
                        "state_root": str(self.state_root),
                        "host_id": HOST,
                        "projects": [
                            {
                                "repository": REPO,
                                "worktree": str(self.worktree),
                                "cursor_chat_id": CHAT_ID,
                                "issue_number": 47,
                            }
                        ],
                    }
                )
            )
        self.assertIn("unknown", str(packet_number.exception))
        with self.assertRaises(ValidationError) as durable_branch:
            load_host_worker_config(
                self._write_descriptor_file(
                    {
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
                )
            )
        self.assertIn("unknown", str(durable_branch.exception))
        with self.assertRaises(ValidationError) as extra_root:
            load_host_worker_config(
                self._write_descriptor_file(
                    {
                        "schema_version": 1,
                        "state_root": str(self.state_root),
                        "host_id": HOST,
                        "lock_path": "/tmp/caller.lock",
                        "projects": [
                            {
                                "repository": REPO,
                                "worktree": str(self.worktree),
                                "cursor_chat_id": CHAT_ID,
                            }
                        ],
                    }
                )
            )
        self.assertIn("unknown", str(extra_root.exception))

    def test_fixed_argv_binds_workspace_and_prompt_without_persist(self) -> None:
        workspace = str(self.worktree.resolve())
        argv = build_headless_resume_argv(
            CHAT_ID, workspace=workspace, prompt=PROMPT
        )
        self.assertEqual(
            argv,
            [
                "agent",
                "--print",
                "--resume",
                CHAT_ID,
                "--force",
                "--trust",
                "--workspace",
                workspace,
                PROMPT,
            ],
        )
        self.assertNotIn("persist", argv)
        self.assertNotIn("create-chat", argv)
        self.assertNotIn("-p", argv)
        self.assertIn("--resume", argv)

    def test_prompt_must_stay_bounded(self) -> None:
        workspace = str(self.worktree.resolve())
        inbound = "bounded packet rework " + ("y" * 4000)
        self.assertLessEqual(len(inbound), MAX_PROMPT_CHARS)
        argv = build_headless_resume_argv(CHAT_ID, workspace=workspace, prompt=inbound)
        self.assertEqual(argv[-1], inbound)
        with self.assertRaises(ValidationError):
            build_headless_resume_argv(
                CHAT_ID, workspace=workspace, prompt="x" * (MAX_PROMPT_CHARS + 1)
            )
        with self.assertRaises(ValidationError):
            build_headless_resume_argv(
                CHAT_ID, workspace=workspace, prompt="line\nbreak"
            )

    def test_spawn_uses_argv_list_without_shell(self) -> None:
        argv = build_headless_resume_argv(
            CHAT_ID,
            workspace=str(self.worktree.resolve()),
            prompt=PROMPT,
        )
        completed = subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with patch("atlas.host_worker.subprocess.run", return_value=completed) as run:
            self.assertEqual(spawn_agent_argv(argv, str(self.worktree.resolve())), 0)
        run.assert_called_once_with(
            argv,
            cwd=str(self.worktree.resolve()),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=DEFAULT_CURSOR_RESUME_TIMEOUT_SEC,
        )

    def test_nonzero_cursor_exit_is_not_resumed(self) -> None:
        spawned: list[list[str]] = []

        def spawn(argv: list[str], cwd: str) -> int:
            spawned.append(argv)
            return 7

        with self.assertRaises(ValidationError) as caught:
            self._resume(spawn=spawn)
        self.assertIn("exited 7", str(caught.exception))
        self.assertNotIn("resumed", str(caught.exception))
        argv = build_headless_resume_argv(
            CHAT_ID,
            workspace=str(self.worktree.resolve()),
            prompt=PROMPT,
        )
        completed = subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="OPENAI_API_KEY=sk-testsecretvalue",
        )
        with patch("atlas.host_worker.subprocess.run", return_value=completed):
            with self.assertRaises(ValidationError) as direct:
                spawn_agent_argv(argv, str(self.worktree.resolve()))
        self.assertIn("exited 1", str(direct.exception))
        self.assertNotIn("sk-testsecretvalue", str(direct.exception))

    def test_cursor_timeout_and_start_failure_fail_closed(self) -> None:
        argv = build_headless_resume_argv(
            CHAT_ID,
            workspace=str(self.worktree.resolve()),
            prompt=PROMPT,
        )
        timeout = subprocess.TimeoutExpired(
            argv, 120, stderr=b"OPENAI_API_KEY=sk-testsecretvalue"
        )
        with patch("atlas.host_worker.subprocess.run", side_effect=timeout):
            with self.assertRaises(ValidationError) as timed:
                spawn_agent_argv(argv, str(self.worktree.resolve()))
        self.assertIn("timed out", str(timed.exception))
        self.assertNotIn("sk-testsecretvalue", str(timed.exception))
        with patch(
            "atlas.host_worker.subprocess.run",
            side_effect=FileNotFoundError(2, "missing"),
        ):
            with self.assertRaises(ValidationError) as started:
                spawn_agent_argv(argv, str(self.worktree.resolve()))
        self.assertIn("failed to start", str(started.exception))

    def test_idle_noop_makes_no_cursor_or_model_call(self) -> None:
        def fail_git(argv: list[str], cwd: str) -> str:
            raise AssertionError("idle path must not read git")

        def fail_spawn(argv: list[str], cwd: str) -> int:
            raise AssertionError("idle path must not invoke Cursor")

        class BoomStore(MemoryCheckpointStore):
            def load(self):
                raise AssertionError("idle path must not load a checkpoint")

            def save(self, packet):
                raise AssertionError("idle path must not write a checkpoint")

        outcome = self._run(
            store=BoomStore(),
            git_runner=fail_git,
            spawn=fail_spawn,
        )
        self.assertEqual(outcome["action"], "idle_noop")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(outcome["model_calls"], 0)
        self.assertEqual(outcome["checkpoint_writes"], 0)

    def test_host_identity_mismatch_blocks_before_cursor(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as caught:
            self._resume(
                host_probe=lambda: "other-host",
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("host identity mismatch", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_resume_uses_fixed_argv_and_does_not_create_a_chat(self) -> None:
        spawned: list[tuple[list[str], str]] = []

        def spawn(argv: list[str], cwd: str) -> int:
            spawned.append((list(argv), cwd))
            return 0

        class SaveForbidden(MemoryCheckpointStore):
            def save(self, packet):
                raise AssertionError("resume must not write a checkpoint")

        outcome = self._resume(store=SaveForbidden(), spawn=spawn)
        self.assertEqual(outcome["action"], "resumed")
        self.assertEqual(outcome["cursor_calls"], 1)
        self.assertEqual(outcome["model_calls"], 0)
        self.assertEqual(outcome["argv"][-1], PROMPT)
        self.assertIn("--workspace", outcome["argv"])
        self.assertIn(str(self.worktree.resolve()), outcome["argv"])
        self.assertNotIn("persist", outcome["argv"])
        self.assertEqual(spawned[0][0], outcome["argv"])
        self.assertNotIn(CHAT_ID, json.dumps(outcome["github_projection"]))
        self.assertNotIn("branch", outcome["github_projection"])

    def test_runtime_branch_mismatch_does_not_spawn(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as caught:
            self._resume(
                git_runner=self._git(branch="other-branch"),
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("branch mismatch", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_stale_checkpoint_repo_branch_and_sha_block_dispatch(self) -> None:
        def fail_git(argv: list[str], cwd: str) -> str:
            raise AssertionError("stale checkpoint must block before git")

        cases = (
            ("other/repo", BRANCH, HEAD, "stale repository"),
            (REPO, "other-branch", HEAD, "stale branch"),
            (REPO, BRANCH, OTHER_HEAD, "stale HEAD"),
        )
        for repository, branch, sha, message in cases:
            spawned: list[list[str]] = []
            packet = AuditControlPacket(
                target_repository=repository,
                target_branch=branch,
                current_target_sha=sha,
            )
            with self.subTest(message=message):
                with self.assertRaises(ValidationError) as caught:
                    self._resume(
                        store=MemoryCheckpointStore(packet),
                        git_runner=fail_git,
                        spawn=lambda argv, cwd: spawned.append(argv) or 0,
                    )
                self.assertIn(message, str(caught.exception))
                self.assertEqual(spawned, [])

    def test_wrong_repository_does_not_spawn(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as caught:
            self._resume(
                git_runner=self._git(origin="https://github.com/other/repo.git"),
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("repository mismatch", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_wrong_workspace_does_not_spawn(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as caught:
            self._resume(
                git_runner=self._git(toplevel=str((self.root / "other").resolve())),
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("toplevel mismatch", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_dirty_worktree_does_not_spawn(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as caught:
            self._resume(
                git_runner=self._git(porcelain=" M atlas/host_worker.py"),
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("dirty", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_stale_head_does_not_spawn(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as caught:
            self._resume(
                git_runner=self._git(head=OTHER_HEAD),
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("head mismatch", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_live_slice_claim_refuses_duplicate_run(self) -> None:
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD,
            audit_status="IN_SLICE",
            current_unit="changed_code",
            slice_claim={
                "state": "executing",
                "claimed_at": __import__("time").time(),
                "lease_seconds": 900,
            },
        )
        spawned: list[list[str]] = []

        def fail_git(argv: list[str], cwd: str) -> str:
            raise AssertionError("live claim must block before git")

        with self.assertRaises(ValidationError) as caught:
            self._resume(
                store=MemoryCheckpointStore(packet),
                git_runner=fail_git,
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("duplicate invocation refused", str(caught.exception))
        self.assertEqual(spawned, [])

    def test_same_chat_id_has_one_lock_domain(self) -> None:
        first = chat_lock_path(self.state_root, CHAT_ID)
        second = chat_lock_path(self.state_root / ".." / self.state_root.name, CHAT_ID)
        self.assertEqual(first, second)
        other_root = chat_lock_path(self.root / "elsewhere", CHAT_ID)
        self.assertNotEqual(first, other_root)
        with self.assertRaises(TypeError):
            run_once(config=self.config, lock_path=self.root / "caller.lock")  # type: ignore[call-arg]

        held = threading.Event()
        release = threading.Event()
        spawned: list[list[str]] = []

        def holder() -> None:
            with host_worker_run_lock(first):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(2))
        try:
            with self.assertRaises(ValidationError) as caught:
                self._resume(spawn=lambda argv, cwd: spawned.append(argv) or 0)
            self.assertIn("already running", str(caught.exception))
            self.assertEqual(spawned, [])
        finally:
            release.set()
            thread.join(2)

    def test_concurrent_resume_of_one_chat_spawns_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spawned: list[list[str]] = []
        errors: list[BaseException] = []

        def spawn(argv: list[str], cwd: str) -> int:
            spawned.append(list(argv))
            entered.set()
            self.assertTrue(release.wait(5))
            return 0

        def invoke() -> None:
            try:
                self._resume(spawn=spawn)
            except BaseException as exc:  # pragma: no cover - assertion path
                errors.append(exc)

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        second.join(2)
        release.set()
        first.join(2)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(any("already running" in str(exc) for exc in errors))

    def test_resume_requires_explicit_self_contained_prompt(self) -> None:
        spawned: list[list[str]] = []
        with self.assertRaises(ValidationError) as missing:
            self._resume(prompt=None, spawn=lambda argv, cwd: spawned.append(argv) or 0)
        self.assertIn("self-contained", str(missing.exception))
        with self.assertRaises(ValidationError) as history:
            self._resume(
                prompt="/work-resume",
                spawn=lambda argv, cwd: spawned.append(argv) or 0,
            )
        self.assertIn("chat history", str(history.exception))
        self.assertEqual(spawned, [])

    def test_timeout_is_host_configurable_and_bounded(self) -> None:
        loaded = load_host_worker_config(self._write_descriptor_file())
        self.assertEqual(
            loaded.cursor_resume_timeout_sec, DEFAULT_CURSOR_RESUME_TIMEOUT_SEC
        )
        bounded = self._write_descriptor_file(
            {
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
        )
        self.assertEqual(load_host_worker_config(bounded).cursor_resume_timeout_sec, 90)
        for illegal in (29, 7201, True):
            with self.subTest(illegal=illegal):
                path = self._write_descriptor_file(
                    {
                        "schema_version": 1,
                        "state_root": str(self.state_root),
                        "host_id": HOST,
                        "cursor_resume_timeout_sec": illegal,
                        "projects": [
                            {
                                "repository": REPO,
                                "worktree": str(self.worktree),
                                "cursor_chat_id": CHAT_ID,
                            }
                        ],
                    }
                )
                with self.assertRaises(ValidationError):
                    load_host_worker_config(path)

    def test_configured_timeout_is_passed_to_cursor(self) -> None:
        config = HostWorkerConfig(
            state_root=self.config.state_root,
            host_id=HOST,
            projects=self.config.projects,
            cursor_resume_timeout_sec=90,
        )
        argv = build_headless_resume_argv(
            CHAT_ID, workspace=str(self.worktree.resolve()), prompt=PROMPT
        )
        completed = subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with patch("atlas.host_worker.subprocess.run", return_value=completed) as run:
            outcome = self._resume(config=config, spawn=None)
        self.assertEqual(outcome["action"], "resumed")
        self.assertEqual(run.call_args.kwargs["timeout"], 90)
        self.assertNotIn("persist", run.call_args.args[0])
        self.assertNotIn("create-chat", run.call_args.args[0])

    def test_live_persist_session_is_noop_without_stopping(self) -> None:
        spawned: list[list[str]] = []

        def fail_git(argv: list[str], cwd: str) -> str:
            raise AssertionError("active persist session must skip resume")

        session = PersistSession(
            session_id="sess-existing",
            workspace=str(self.worktree.resolve()),
            status="running",
        )
        outcome = self._resume(
            git_runner=fail_git,
            spawn=lambda argv, cwd: spawned.append(argv) or 0,
            list_sessions=lambda: [session],
        )
        self.assertEqual(outcome["action"], "cursor_active_noop")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(outcome["sessions_stopped"], 0)
        self.assertEqual(spawned, [])

    def test_live_persist_process_on_worktree_is_noop(self) -> None:
        spawned: list[list[str]] = []
        outcome = self._resume(
            git_runner=lambda argv, cwd: (_ for _ in ()).throw(
                AssertionError("active process must skip resume")
            ),
            spawn=lambda argv, cwd: spawned.append(argv) or 0,
            list_sessions=lambda: [],
            list_processes=lambda _worktree: [(99, "agent persist --force --trust")],
        )
        self.assertEqual(outcome["action"], "cursor_active_noop")
        self.assertEqual(outcome["sessions_stopped"], 0)
        self.assertEqual(spawned, [])

    def test_chat_id_domain_lock_covers_different_state_roots(self) -> None:
        other_root = self.root / "other-state"
        other_root.mkdir()
        other = HostWorkerConfig(
            state_root=str(other_root.resolve()),
            host_id=HOST,
            projects=self.config.projects,
        )
        held = threading.Event()
        release = threading.Event()
        spawned: list[list[str]] = []

        def holder() -> None:
            with host_worker_run_lock(chat_domain_lock_path(CHAT_ID)):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(2))
        try:
            with self.assertRaises(ValidationError) as caught:
                self._resume(
                    config=other,
                    spawn=lambda argv, cwd: spawned.append(argv) or 0,
                )
            self.assertIn("already running", str(caught.exception))
            self.assertEqual(spawned, [])
            self.assertNotEqual(
                chat_lock_path(self.state_root, CHAT_ID),
                chat_lock_path(other_root, CHAT_ID),
            )
        finally:
            release.set()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
