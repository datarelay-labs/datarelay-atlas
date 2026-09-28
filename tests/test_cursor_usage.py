"""Cursor usage Phase 0 inventory and CSV summary regressions."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from atlas.cli import build_parser, main
from atlas.cursor_usage import (
    GitIdentity,
    PacketFact,
    ProcessFact,
    build_report,
    canonical_workspace,
    classify_workers,
    collect_process_facts,
    load_context_advice,
    load_packet_facts,
    load_worker_snapshot,
    parse_usage_csv,
    read_git_identity,
    recommend,
    summarize_usage,
)
from atlas.provenance import ValidationError
from atlas.work_controller import PersistSession

HEADER = (
    "Date,Kind,Model,Max Mode,Input (w/ Cache Write),Input (w/o Cache Write),"
    "Cache Read,Output Tokens,Total Tokens,Cost,Cloud Agent ID,Automation ID"
)
SECRET = "SUPER_SECRET_PROMPT_TEXT"
HEAD = "a86d7a907b97a8cc4cef3f7fd7fe4c8a948ea161"
RESTORE_TOKEN = "0123456789abcdef0123456789abcdef"
LIVE_SESSION = "cursor-alpha-0123456789-1-abcdef"


def _share(part: int, whole: int) -> str:
    value = (Decimal(part) / Decimal(whole)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    return f"{value:.6f}"


def _event_row(
    timestamp: str,
    cache_write: int,
    fresh_input: int,
    cache_read: int,
    output: int,
    *,
    kind: str = "Included",
    model: str = "composer-2.5",
    max_mode: str = "No",
    cost: str = "",
    cloud: str = "",
    automation: str = "",
) -> str:
    total = cache_write + fresh_input + cache_read + output
    return (
        f"{timestamp},{kind},{model},{max_mode},{cache_write},{fresh_input},"
        f"{cache_read},{output},{total},{cost},{cloud},{automation}"
    )


def _write_csv(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".csv", delete=False
    )
    handle.write(text)
    handle.close()
    return Path(handle.name)


def _restore_argv(session_id: str, *, token: bytes | None = None, extra: bytes | None = None) -> bytes:
    """Host-proven restore argv. ``extra`` makes the shape ambiguous."""
    restore_token = RESTORE_TOKEN.encode() if token is None else token
    argv = (
        b"/usr/local/bin/node\0--use-system-ca\0"
        b"/opt/cursor/resources/app/index.js\0"
        b"--cursor-persist-restore\0"
        + restore_token
        + b"\0"
        + session_id.encode()
    )
    if extra:
        argv += b"\0" + extra
    return argv


def _identity(workspace: str) -> GitIdentity:
    return GitIdentity(
        repository="datarelay-labs/datarelay-atlas",
        branch="feature/cursor-usage-phase0-worker-inventory",
        head=HEAD,
        dirty=False,
    )


class CursorUsageCsvTests(unittest.TestCase):
    def test_summary_matches_hand_computed_fixture(self):
        text = "\n".join(
            [
                HEADER,
                _event_row(
                    "2026-09-22T00:00:00Z",
                    0,
                    40,
                    5,
                    5,
                    model="alpha",
                    cost="1.25",
                ),
                _event_row(
                    "2026-09-22T01:00:00Z",
                    0,
                    10,
                    70,
                    20,
                    model="beta",
                    max_mode="Yes",
                    cloud="cloud-1",
                ),
                _event_row(
                    "2026-09-22T02:00:00Z",
                    0,
                    0,
                    4_000_000,
                    1_000_000,
                    model="alpha",
                ),
                _event_row(
                    "2026-09-22T02:01:00Z",
                    0,
                    0,
                    9_000_000,
                    1_000_000,
                    model="alpha",
                    automation="auto-9",
                ),
            ]
        )
        events = parse_usage_csv(_write_csv(text))
        summary = summarize_usage(events)
        self.assertEqual(summary["event_count"], 4)
        self.assertEqual(summary["input_tokens"], 50)
        self.assertEqual(summary["cache_write_tokens"], 0)
        self.assertEqual(summary["cache_read_tokens"], 13_000_075)
        self.assertEqual(summary["output_tokens"], 2_000_025)
        self.assertEqual(summary["total_tokens"], 15_000_150)
        self.assertEqual(summary["max_event_tokens"], 10_000_000)
        self.assertEqual(summary["percentiles"]["p50"], 100)
        self.assertEqual(summary["percentiles"]["p95"], 10_000_000)
        self.assertEqual(summary["percentiles"]["p99"], 10_000_000)
        self.assertEqual(
            summary["model_mix"],
            [
                {"model": "alpha", "event_count": 3, "total_tokens": 15_000_050},
                {"model": "beta", "event_count": 1, "total_tokens": 100},
            ],
        )
        heavy_5_tokens = 15_000_000
        heavy_10_tokens = 10_000_000
        self.assertEqual(summary["heavy_events"]["ge_5000000"]["count"], 2)
        self.assertEqual(summary["heavy_events"]["ge_5000000"]["total_tokens"], heavy_5_tokens)
        self.assertEqual(
            summary["heavy_events"]["ge_5000000"]["token_share"],
            _share(heavy_5_tokens, 15_000_150),
        )
        self.assertEqual(summary["heavy_events"]["ge_10000000"]["count"], 1)
        self.assertEqual(
            summary["heavy_events"]["ge_10000000"]["token_share"],
            _share(heavy_10_tokens, 15_000_150),
        )
        self.assertEqual(summary["max_burst"]["total_tokens"], 15_000_000)
        self.assertEqual(summary["max_burst"]["event_count"], 2)
        self.assertEqual(summary["cache_read_ratio"], _share(13_000_075, 15_000_150))
        self.assertEqual(events[0].cost, "1.25")
        self.assertIsNone(events[2].cost)
        self.assertIsNone(events[0].cloud_agent_id)
        self.assertEqual(events[1].cloud_agent_id, "cloud-1")
        self.assertEqual(events[3].automation_id, "auto-9")
        self.assertFalse(events[0].max_mode)
        self.assertTrue(events[1].max_mode)
        self.assertEqual(events[0].kind, "Included")

    def test_absent_optional_columns_stay_null(self):
        header = (
            "Date,Input (w/ Cache Write),Input (w/o Cache Write),"
            "Cache Read,Output Tokens,Total Tokens"
        )
        text = header + "\n2026-09-22T03:04:05+00:00,0,2,3,4,9\n"
        event = parse_usage_csv(_write_csv(text))[0]
        self.assertIsNone(event.kind)
        self.assertIsNone(event.model)
        self.assertIsNone(event.max_mode)
        self.assertIsNone(event.cost)
        self.assertIsNone(event.cost_to_you)
        self.assertIsNone(event.cloud_agent_id)
        self.assertIsNone(event.automation_id)
        self.assertEqual(event.input_tokens, 2)
        self.assertEqual(event.total_tokens, 9)

    def test_schema_and_type_errors_fail_closed(self):
        cases = [
            HEADER + "\n2026-09-22T00:00:00Z,Included,m,No,0,1,1,1,4,not-a-cost,,\n",
            HEADER + "\n2026-09-22T00:00:00Z,Included,m,maybe,0,1,1,1,3,,,\n",
            HEADER + "\n2026-09-22,Included,m,No,0,1,1,1,3,,,\n",
            HEADER + "\n2026-09-22T00:00:00Z,Included,m,No,0,1,1,1,99,,,\n",
            HEADER + "\n2026-09-22T00:00:00Z,Included,m,No," + ("9" * 5000) + ",0,0,0," + ("9" * 5000) + ",,,\n",
            "Date,User,Input (w/ Cache Write),Input (w/o Cache Write),"
            "Cache Read,Output Tokens,Total Tokens\n"
            "2026-09-22T00:00:00Z,person,0,1,1,1,3\n",
            "Date,Input (w/ Cache Write),Cache Read,Output Tokens,Total Tokens\n"
            "2026-09-22T00:00:00Z,0,1,1,2\n",
        ]
        for text in cases:
            with self.subTest(text=text.splitlines()[0]):
                with self.assertRaises(ValidationError):
                    parse_usage_csv(_write_csv(text))

    def test_high_cache_ratio_does_not_change_recommendation(self):
        text = "\n".join(
            [
                "Date,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens",
                "2026-09-22T00:00:00Z,0,1,99,0,100",
            ]
        )
        summary = summarize_usage(parse_usage_csv(_write_csv(text)))
        self.assertEqual(summary["cache_read_ratio"], "0.990000")
        advice = recommend([], current_observed=False)
        self.assertEqual(advice["recommendation"], "UNKNOWN")
        self.assertNotIn(advice["recommendation"], {"YIELD_BUDGET", "CHECKPOINT_CLEAR_RECOMMENDED"})


class CursorUsageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = canonical_workspace("/tmp/atlas-usage-worker-a")
        self.other = canonical_workspace("/tmp/atlas-usage-worker-b")
        assert self.workspace is not None
        assert self.other is not None
        self.identities = {
            self.workspace: _identity(self.workspace),
            self.other: _identity(self.other),
        }

    def _session(self, session_id: str, workspace: str, status: str = "Detached (running)") -> PersistSession:
        return PersistSession(
            session_id=session_id,
            workspace=workspace,
            status=status,
            task=SECRET,
        )

    def test_idle_resident_is_not_token_spend(self):
        report = build_report(
            [self._session("sess-idle-01", self.workspace)],
            [
                ProcessFact(
                    session_id="sess-idle-01",
                    workspace=self.workspace,
                    ambiguous=False,
                    runtime="quiescent",
                )
            ],
            self.identities,
            summary=None,
        )
        self.assertEqual(report["workers"][0]["state"], "IDLE_REUSABLE")
        self.assertEqual(report["workers"][0]["inference_activity"], "UNKNOWN")
        self.assertEqual(report["workers"][0]["runtime"], "quiescent")
        self.assertTrue(report["workers"][0]["resident"])
        self.assertEqual(report["advisor"]["recommendation"], "CONTINUE")
        heavy = summarize_usage(
            parse_usage_csv(
                _write_csv(
                    "Date,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens\n"
                    "2026-09-22T00:00:00Z,0,0,9000000,1000000,10000000\n"
                )
            )
        )
        with_history = build_report(
            [self._session("sess-idle-01", self.workspace)],
            [
                ProcessFact(
                    session_id="sess-idle-01",
                    workspace=self.workspace,
                    ambiguous=False,
                    runtime="quiescent",
                )
            ],
            self.identities,
            summary=heavy,
        )
        self.assertEqual(with_history["usage"]["heavy_events"]["ge_10000000"]["count"], 1)
        self.assertEqual(with_history["advisor"]["recommendation"], "CONTINUE")
        self.assertNotIn(
            with_history["advisor"]["recommendation"],
            {"YIELD_BUDGET", "CHECKPOINT_CLEAR_RECOMMENDED", "SUMMARIZE_RECOMMENDED"},
        )
        encoded = json.dumps(report)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("task", encoded)

    def test_busy_runtime_is_not_active_inference(self):
        report = build_report(
            [self._session("sess-live-01", self.workspace, "Attached (1 client)")],
            [
                ProcessFact(
                    session_id="sess-live-01",
                    workspace=self.workspace,
                    ambiguous=False,
                    runtime="busy",
                )
            ],
            self.identities,
        )
        worker = report["workers"][0]
        self.assertEqual(worker["inference_activity"], "UNKNOWN")
        self.assertEqual(worker["runtime"], "busy")
        self.assertEqual(worker["state"], "IDLE_REUSABLE")
        self.assertEqual(worker["attachment"], "attached")
        self.assertEqual(report["advisor"]["recommendation"], "CONTINUE")

    def test_duplicate_worktree_and_ambiguous_evidence(self):
        sessions = [
            self._session("sess-dup-01", self.workspace),
            self._session("sess-dup-02", self.workspace),
        ]
        processes = [
            ProcessFact("sess-dup-01", self.workspace, False, "busy"),
            ProcessFact("sess-dup-02", self.workspace, True, "quiescent"),
        ]
        workers = classify_workers(sessions, processes, self.identities)
        self.assertEqual({item["state"] for item in workers}, {"DUPLICATE_WORKTREE"})
        by_id = {item["session_id"]: item for item in workers}
        self.assertEqual(by_id["sess-dup-01"]["inference_activity"], "UNKNOWN")
        self.assertEqual(by_id["sess-dup-01"]["runtime"], "busy")
        self.assertEqual(by_id["sess-dup-02"]["inference_activity"], "UNKNOWN")
        self.assertEqual(by_id["sess-dup-02"]["runtime"], "unknown")
        advice = recommend(workers, current_observed=True)
        self.assertEqual(advice["recommendation"], "HUMAN_REQUIRED")

    def test_missing_process_and_relative_workspace_are_unknown(self):
        workers = classify_workers(
            [
                self._session("sess-missing", self.workspace),
                self._session("sess-relative", "relative/worktree"),
            ],
            [],
            self.identities,
        )
        self.assertEqual(
            [item["state"] for item in workers],
            ["ORPHAN_OR_UNKNOWN", "ORPHAN_OR_UNKNOWN"],
        )

    def test_terminal_packet_survivor_requires_exact_head(self):
        report = build_report(
            [self._session("sess-done-01", self.workspace)],
            [ProcessFact("sess-done-01", self.workspace, False, "quiescent")],
            self.identities,
            [
                PacketFact(
                    repository="datarelay-labs/datarelay-atlas",
                    branch="feature/cursor-usage-phase0-worker-inventory",
                    status="COMPLETE",
                    head=HEAD,
                    issue_number=78,
                )
            ],
        )
        self.assertEqual(report["workers"][0]["state"], "TERMINAL_WORK_SURVIVOR")
        self.assertEqual(report["workers"][0]["packet_status"], "COMPLETE")
        self.assertEqual(report["advisor"]["recommendation"], "HUMAN_REQUIRED")
        self.assertNotIn(
            report["advisor"]["recommendation"],
            {"CHECKPOINT_CLEAR_RECOMMENDED", "YIELD_BUDGET", "SUMMARIZE_RECOMMENDED"},
        )

    def test_stale_packet_head_fails_closed_for_same_branch(self):
        report = build_report(
            [self._session("sess-done-02", self.workspace)],
            [ProcessFact("sess-done-02", self.workspace, False, "quiescent")],
            self.identities,
            [
                PacketFact(
                    repository="datarelay-labs/datarelay-atlas",
                    branch="feature/cursor-usage-phase0-worker-inventory",
                    status="COMPLETE",
                    head="0" * 40,
                )
            ],
        )
        worker = report["workers"][0]
        self.assertEqual(worker["state"], "ORPHAN_OR_UNKNOWN")
        self.assertIsNone(worker["packet_status"])
        self.assertEqual(report["advisor"]["recommendation"], "HUMAN_REQUIRED")

    def test_noncanonical_reconciliation_on_worker_branch_fails_closed(self):
        report = build_report(
            [self._session("sess-reconcile", self.workspace)],
            [ProcessFact("sess-reconcile", self.workspace, False, "quiescent")],
            self.identities,
            [],
            packet_observations=[
                {
                    "repository": "datarelay-labs/datarelay-atlas",
                    "issue_number": 80,
                    "branch": "feature/cursor-usage-phase0-worker-inventory",
                    "canonical_fact": False,
                    "reasons": ["PR_HEAD_MISMATCH"],
                }
            ],
        )
        worker = report["workers"][0]
        self.assertEqual(worker["state"], "ORPHAN_OR_UNKNOWN")
        self.assertEqual(report["advisor"]["recommendation"], "HUMAN_REQUIRED")
        self.assertEqual(
            report["reconciliation"][0]["reasons"], ["PR_HEAD_MISMATCH"]
        )

    def test_packet_fact_without_exact_head_is_rejected(self):
        path = _write_csv("")
        path.write_text(
            json.dumps(
                [
                    {
                        "repository": "datarelay-labs/datarelay-atlas",
                        "branch": "feature/cursor-usage-phase0-worker-inventory",
                        "status": "COMPLETE",
                        "head": "b61413b",
                    }
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            load_packet_facts(path)

    def test_conflicting_packet_facts_do_not_guess(self):
        workers = classify_workers(
            [self._session("sess-packet", self.workspace)],
            [ProcessFact("sess-packet", self.workspace, False, "quiescent")],
            self.identities,
            [
                PacketFact(
                    "datarelay-labs/datarelay-atlas",
                    "feature/cursor-usage-phase0-worker-inventory",
                    "COMPLETE",
                    HEAD,
                ),
                PacketFact(
                    "https://github.com/datarelay-labs/datarelay-atlas.git",
                    "feature/cursor-usage-phase0-worker-inventory",
                    "ACTIVE",
                    HEAD,
                ),
            ],
        )
        self.assertEqual(workers[0]["state"], "ORPHAN_OR_UNKNOWN")
        self.assertIsNone(workers[0]["packet_status"])

    def test_unreadable_git_identity_is_unknown(self):
        workers = classify_workers(
            [self._session("sess-git", self.workspace)],
            [ProcessFact("sess-git", self.workspace, False, "quiescent")],
            {self.workspace: None},
        )
        self.assertEqual(workers[0]["state"], "ORPHAN_OR_UNKNOWN")
        self.assertIsNone(workers[0]["repository"])

    def test_workspace_mismatch_is_unknown(self):
        workers = classify_workers(
            [self._session("sess-mismatch", self.workspace)],
            [ProcessFact("sess-mismatch", self.other, False, "quiescent")],
            self.identities,
        )
        self.assertEqual(workers[0]["state"], "ORPHAN_OR_UNKNOWN")


class CursorUsageAdvisorTests(unittest.TestCase):
    def test_historical_heavy_event_does_not_select_a_control_action(self):
        text = "\n".join(
            [
                "Date,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens",
                "2026-09-22T00:00:00Z,0,0,9000000,1000000,10000000",
                "2026-09-27T00:00:00Z,0,1,0,0,1",
            ]
        )
        summary = summarize_usage(parse_usage_csv(_write_csv(text)))
        self.assertEqual(summary["heavy_events"]["ge_10000000"]["count"], 1)
        self.assertGreaterEqual(summary["heavy_events"]["ge_5000000"]["count"], 1)
        idle = [{"state": "IDLE_REUSABLE"}]
        current = recommend(idle, current_observed=True)
        historical = recommend([], current_observed=False)
        self.assertEqual(current["recommendation"], "CONTINUE")
        self.assertEqual(historical["recommendation"], "UNKNOWN")
        for advice in (current, historical):
            self.assertNotIn(
                advice["recommendation"],
                {"YIELD_BUDGET", "CHECKPOINT_CLEAR_RECOMMENDED", "SUMMARIZE_RECOMMENDED"},
            )
            self.assertNotIn("HEAVY_EVENT_10M", advice["reasons"])
            self.assertNotIn("LARGE_EVENT", advice["reasons"])


class CursorUsageProcessTests(unittest.TestCase):
    def test_collector_discards_command_text_and_sees_running_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=10,
                ppid=1,
                state="S",
                cwd=workspace,
                cmdline=_restore_argv(LIVE_SESSION),
            )
            self._proc(root, pid=11, ppid=10, state="R", cwd=workspace, cmdline=b"sleep\0" + SECRET.encode())
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION},
            )
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].session_id, LIVE_SESSION)
            self.assertEqual(facts[0].runtime, "busy")
            self.assertFalse(facts[0].ambiguous)
            encoded = repr(facts)
            self.assertNotIn(SECRET, encoded)
            self.assertNotIn(RESTORE_TOKEN, encoded)
            self.assertNotIn("index.js", encoded)
            self.assertNotIn("ACTIVE", encoded)
            self.assertNotIn("IDLE", encoded)

    def test_sleeping_persist_process_is_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=4,
                ppid=1,
                state="S",
                cwd=workspace,
                cmdline=_restore_argv(LIVE_SESSION),
            )
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION},
            )
            self.assertEqual(facts[0].runtime, "quiescent")
            self.assertEqual(facts[0].session_id, LIVE_SESSION)
            self.assertNotIn(RESTORE_TOKEN, repr(facts))

    def test_malformed_restore_token_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=5,
                ppid=1,
                state="S",
                cwd=workspace,
                cmdline=_restore_argv(LIVE_SESSION, token=b"!!!", extra=SECRET.encode()),
            )
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION},
            )
            self.assertTrue(facts[0].ambiguous)
            self.assertIsNone(facts[0].session_id)
            self.assertEqual(facts[0].runtime, "unknown")
            self.assertNotIn(SECRET, repr(facts))
            self.assertNotIn(LIVE_SESSION, repr(facts))

    def test_agent_persist_argv_is_not_the_restore_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=6,
                ppid=1,
                state="R",
                cwd=workspace,
                cmdline=(
                    b"/usr/bin/agent\0persist\0--cursor-persist-restore\0"
                    + LIVE_SESSION.encode()
                    + b"\0"
                    + SECRET.encode()
                ),
            )
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION},
            )
            self.assertEqual(facts, [])

    def test_host_proven_restore_argv_keeps_only_positional_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=8,
                ppid=1,
                state="S",
                cwd=workspace,
                cmdline=_restore_argv(LIVE_SESSION),
            )
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION, RESTORE_TOKEN},
            )
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].session_id, LIVE_SESSION)
            self.assertEqual(facts[0].runtime, "quiescent")
            self.assertFalse(facts[0].ambiguous)
            encoded = repr(facts)
            self.assertNotIn(RESTORE_TOKEN, encoded)
            self.assertNotIn("index.js", encoded)
            self.assertNotIn("--use-system-ca", encoded)

    def test_known_id_outside_final_argument_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=9,
                ppid=1,
                state="S",
                cwd=workspace,
                cmdline=_restore_argv("cursor-other-0123456789-1-abcdef", extra=LIVE_SESSION.encode()),
            )
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION},
            )
            self.assertEqual(len(facts), 1)
            self.assertTrue(facts[0].ambiguous)
            self.assertIsNone(facts[0].session_id)
            self.assertNotIn(LIVE_SESSION, repr(facts))

    def test_unknown_session_id_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            self._proc(
                root,
                pid=12,
                ppid=1,
                state="S",
                cwd=workspace,
                cmdline=_restore_argv(LIVE_SESSION),
            )
            facts = collect_process_facts(proc_root=root / "proc", known_session_ids=set())
            self.assertTrue(facts[0].ambiguous)
            self.assertIsNone(facts[0].session_id)
            self.assertNotIn(LIVE_SESSION, repr(facts))
            self.assertNotIn(RESTORE_TOKEN, repr(facts))

    def _proc(self, root: Path, *, pid: int, ppid: int, state: str, cwd: Path, cmdline: bytes) -> None:
        proc = root / "proc" / str(pid)
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(cmdline)
        (proc / "stat").write_text(f"{pid} (agent) {state} {ppid}\n", encoding="utf-8")
        (proc / "status").write_text(f"PPid:\t{ppid}\n", encoding="utf-8")
        (proc / "cwd").symlink_to(cwd)


class CursorUsageRunnableProcessTests(unittest.TestCase):
    def test_runnable_persist_process_cannot_become_active_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "work"
            workspace.mkdir()
            proc = root / "proc" / "7"
            proc.mkdir(parents=True)
            (proc / "cmdline").write_bytes(_restore_argv(LIVE_SESSION))
            (proc / "stat").write_text("7 (agent) R 1\n", encoding="utf-8")
            (proc / "status").write_text("PPid:\t1\n", encoding="utf-8")
            (proc / "cwd").symlink_to(workspace)
            canonical = canonical_workspace(str(workspace))
            assert canonical is not None
            facts = collect_process_facts(
                proc_root=root / "proc",
                known_session_ids={LIVE_SESSION},
            )
            self.assertEqual(facts[0].runtime, "busy")
            report = build_report(
                [
                    PersistSession(
                        session_id=LIVE_SESSION,
                        workspace=canonical,
                        status="Detached (running in background)",
                        task=SECRET,
                    )
                ],
                facts,
                {canonical: _identity(canonical)},
            )
            worker = report["workers"][0]
            self.assertEqual(worker["inference_activity"], "UNKNOWN")
            self.assertEqual(worker["runtime"], "busy")
            self.assertNotEqual(worker["state"], "RUNNING_AUTHORIZED")
            self.assertEqual(worker["state"], "IDLE_REUSABLE")
            self.assertEqual(report["advisor"]["recommendation"], "CONTINUE")
            self.assertNotIn(SECRET, json.dumps(report))


class CursorUsageGitTests(unittest.TestCase):
    def test_git_identity_drops_porcelain_text(self):
        workspace = canonical_workspace("/tmp/atlas-usage-git")
        assert workspace is not None

        def runner(argv: list[str], cwd: str) -> str:
            del cwd
            mapping = {
                ("git", "rev-parse", "--show-toplevel"): workspace,
                ("git", "remote", "get-url", "origin"): "git@github.com:datarelay-labs/datarelay-atlas.git",
                ("git", "branch", "--show-current"): "feature/cursor-usage-phase0-worker-inventory",
                ("git", "rev-parse", "HEAD"): HEAD,
                ("git", "status", "--porcelain", "--untracked-files=all"): SECRET,
            }
            return mapping[tuple(argv)]

        identity = read_git_identity(workspace, runner)
        assert identity is not None
        self.assertTrue(identity.dirty)
        self.assertEqual(identity.repository, "datarelay-labs/datarelay-atlas")
        self.assertNotIn(SECRET, repr(identity))


class CursorUsageSnapshotTests(unittest.TestCase):
    def _worker(self, *, head: str = HEAD, state: str = "IDLE_REUSABLE") -> dict:
        return {
            "session_id": "cursor-snapshot-0123456789-1-abcdef",
            "workspace": "/srv/worker",
            "resident": True,
            "attachment": "detached",
            "inference_activity": "UNKNOWN",
            "runtime": "quiescent",
            "state": state,
            "repository": "datarelay-labs/datarelay-atlas",
            "branch": "feature/cursor-usage-phase0-worker-inventory",
            "head": head,
            "dirty": False,
            "packet_status": None,
            "host_id": "dev-atlas",
        }

    def _snapshot(self, worker: dict) -> Path:
        path = _write_csv("")
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "cursor_worker_inventory",
                    "host_id": "dev-atlas",
                    "observed_at": "2026-09-28T00:00:00Z",
                    "workers": [worker],
                    "advisor": {"recommendation": "CONTINUE", "reasons": []},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_snapshot_is_bounded_and_central_reconciliation_reclassifies_worker(self):
        workers, summary = load_worker_snapshot(self._snapshot(self._worker()))
        self.assertEqual(summary["host_id"], "dev-atlas")
        self.assertEqual(summary["worker_count"], 1)
        self.assertEqual(workers[0]["host_id"], "dev-atlas")

        report = build_report(
            [],
            [],
            {},
            [
                PacketFact(
                    repository="datarelay-labs/datarelay-atlas",
                    branch="feature/cursor-usage-phase0-worker-inventory",
                    status="COMPLETE",
                    head=HEAD,
                    issue_number=78,
                )
            ],
            imported_workers=workers,
            snapshot_summaries=[summary],
        )
        self.assertEqual(report["workers"][0]["state"], "TERMINAL_WORK_SURVIVOR")
        self.assertEqual(report["workers"][0]["packet_status"], "COMPLETE")
        self.assertEqual(report["snapshots"][0]["host_id"], "dev-atlas")

    def test_snapshot_rejects_invented_inference_and_raw_content(self):
        worker = self._worker()
        worker["inference_activity"] = "ACTIVE"
        with self.assertRaises(ValidationError):
            load_worker_snapshot(self._snapshot(worker))

        worker = self._worker()
        path = self._snapshot(worker)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["prompt"] = SECRET
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValidationError):
            load_worker_snapshot(path)

    def test_reconciliation_output_is_relevant_and_summarized(self):
        worker = self._worker()
        observations = [
            {
                "repository": "datarelay-labs/datarelay-atlas",
                "issue_number": 78,
                "issue_state": "CLOSED",
                "branch": "feature/cursor-usage-phase0-worker-inventory",
                "canonical_fact": True,
                "packet_status": "COMPLETE",
                "head": HEAD,
                "reasons": [],
            },
            {
                "repository": "datarelay-labs/datarelay-atlas",
                "issue_number": 80,
                "issue_state": "OPEN",
                "branch": "feature/prod-usage-control-plane-dogfood",
                "canonical_fact": True,
                "packet_status": "ACTIVE",
                "head": HEAD,
                "reasons": [],
            },
            {
                "repository": "datarelay-labs/datarelay-atlas",
                "issue_number": 2,
                "issue_state": "CLOSED",
                "branch": "old-unrelated-branch",
                "canonical_fact": False,
                "packet_status": None,
                "head": None,
                "reasons": ["PACKET_METADATA_INVALID"],
            },
        ]
        report = build_report(
            [],
            [],
            {},
            packet_observations=observations,
            imported_workers=[worker],
        )
        self.assertEqual(len(report["reconciliation"]), 2)
        self.assertEqual(
            report["reconciliation_summary"],
            {
                "observed_count": 3,
                "canonical_count": 2,
                "noncanonical_count": 1,
                "returned_count": 2,
            },
        )


class CursorUsageContextTests(unittest.TestCase):
    def test_context_advice_is_bounded_and_clear_is_recommendation_only(self):
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(
                {
                    "durable_checkpoint": True,
                    "logical_boundary": True,
                },
                handle,
            )
            path = Path(handle.name)
        advice = load_context_advice(path)
        self.assertEqual(advice["recommendation"], "CLEAR_RECOMMENDED")
        self.assertEqual(advice["reasons"], ["SEMANTIC_BOUNDARY"])
        self.assertEqual(advice["source"], "ENGINEERING_SYSTEM_CONTEXT_EPOCH")

        path.write_text(
            json.dumps(
                {
                    "durable_checkpoint": True,
                    "logical_boundary": True,
                    "prompt": SECRET,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            load_context_advice(path)

    def test_usage_report_parser_exposes_read_only_reconciliation_options(self):
        args = build_parser().parse_args(
            [
                "usage",
                "report",
                "--github-reconcile",
                "--repository",
                "datarelay-labs/datarelay-atlas",
                "--context-facts",
                "/tmp/context.json",
                "--host-id",
                "prod-atlas",
                "--worker-snapshot",
                "/tmp/dev-atlas.json",
                "--worker-snapshot",
                "/tmp/dev-control.json",
            ]
        )
        self.assertTrue(args.github_reconcile)
        self.assertEqual(args.repository, ["datarelay-labs/datarelay-atlas"])
        self.assertEqual(args.context_facts, "/tmp/context.json")
        self.assertEqual(args.host_id, "prod-atlas")
        self.assertEqual(
            args.worker_snapshot,
            ["/tmp/dev-atlas.json", "/tmp/dev-control.json"],
        )


class CursorUsageCliTests(unittest.TestCase):
    def test_summarize_cli_and_rejection(self):
        path = _write_csv(
            "Date,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens\n"
            "2026-09-22T00:00:00Z,1,2,3,4,10\n"
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["usage", "summarize", "--csv", str(path)])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["kind"], "cursor_usage_summary")
        self.assertEqual(payload["total_tokens"], 10)
        self.assertEqual(payload["advisor"]["recommendation"], "UNKNOWN")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rejected = main(["usage", "summarize", "--csv", str(path) + ".missing"])
        self.assertEqual(rejected, 1)
        self.assertIn("error:", stderr.getvalue())

    def test_module_does_not_control_sessions(self):
        source = (
            Path(__file__).resolve().parents[1] / "atlas" / "cursor_usage.py"
        ).read_text(encoding="utf-8")
        for token in ("os.kill", "SIGKILL", "SIGTERM", "/clear", "persist stop"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
