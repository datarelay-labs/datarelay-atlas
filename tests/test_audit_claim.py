"""Slice C exact-HEAD audit claim, idempotency, and budget persistence."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.audit_claim import (
    FINAL_AUDIT_CLAIM_BRANCH,
    AuditClaim,
    GitHubContentsClaimStore,
    IssueAuditLedger,
    MemoryClaimStore,
    WorkPacketSnapshot,
    make_audit_claim_key,
    run_exact_head_audit,
)
from atlas.chat_audit import SLICE_CLAIM_LEASE_SECONDS, CheckpointCasConflict
from atlas.final_audit import (
    AuditBudget,
    AuditTelemetry,
    BoundedResponsesAuditProvider,
)
from atlas.provenance import ValidationError
from atlas.work_controller import AuditResult, CompletionEvent, PersistSession, WorkstreamRecord


HEAD = "a" * 40
OTHER = "b" * 40
REPO = "datarelay-labs/datarelay-atlas"
BRANCH = "feature/autonomous-local-supervisor-gpt56-audit"
SECRET = "OPENAI_API_KEY=sk-fake-secret-1234567890"
NOW = 1_700_000_000.0


def _bundle(**git_status: str) -> dict:
    git = {
        "evidence_status": "OK",
        "base": OTHER,
        "head": HEAD,
        "diff": "diff --git a/atlas/audit_claim.py b/atlas/audit_claim.py\n+claim\n",
        "changed_files": ["atlas/audit_claim.py"],
    }
    git.update(git_status)
    return {
        "schema": "awc.codex_evidence_bundle.v1",
        "identity": {
            "repository": REPO,
            "branch": BRANCH,
            "head": HEAD,
            "toplevel": "wt",
        },
        "git": git,
        "work_packet": {"status": "OK", "body": "Acceptance: exact-head claim."},
        "tests": {"status": "PASS", "detail": "focused tests OK"},
        "ci": {"status": "OK", "detail": "affected-tests SUCCESS"},
        "pr_reviews": {"status": "OK", "body": "none"},
    }


def _packet(*, head: str = HEAD, status: str = "ACTIVE", issue: int = 47) -> WorkPacketSnapshot:
    return WorkPacketSnapshot(
        repository=REPO,
        issue_number=issue,
        branch=BRANCH,
        head=head,
        status=status,
    )


def _event(head: str = HEAD) -> CompletionEvent:
    return CompletionEvent(
        event_id="evt-47",
        workstream="issue-47",
        issue_number=47,
        branch=BRANCH,
        head=head,
        attempt=1,
    )


def _record(worktree: str, head: str = HEAD) -> WorkstreamRecord:
    return WorkstreamRecord(
        workstream="issue-47",
        repository=REPO,
        issue_number=47,
        branch=BRANCH,
        worktree_path=worktree,
        expected_head=head,
    )


def _git(head: str, *, dirty: bool = False):
    def runner(argv: list[str], cwd: str) -> str:
        if argv[1:] == ["rev-parse", "--show-toplevel"]:
            return cwd
        if argv[1:] == ["remote", "get-url", "origin"]:
            return f"https://github.com/{REPO}.git"
        if argv[1:] == ["branch", "--show-current"]:
            return BRANCH
        if argv[1:] == ["rev-parse", "HEAD"]:
            return head
        if argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
            return " M dirty" if dirty else ""
        raise ValidationError(f"unexpected git argv {argv}")

    return runner


class ScriptAuditor:
    def __init__(self, verdict: str, *, cost: float = 0.01, findings: str = "bounded") -> None:
        self.calls = 0
        self.last_request_body: dict | None = None
        self._verdict = verdict
        self._cost = cost
        self._findings = findings
        self.last_telemetry: AuditTelemetry | None = None

    def audit_bundle(self, bundle: dict, **kwargs: object) -> AuditResult:
        self.calls += 1
        self.last_request_body = {"sent": True}
        self.last_telemetry = AuditTelemetry(
            model="gpt-5.6-sol",
            input_tokens=100,
            output_tokens=20,
            cached_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=self._cost,
            target_sha=HEAD,
            verdict=self._verdict,
            duration_sec=0.25,
        )
        return AuditResult(verdict=self._verdict, findings=self._findings)


class SliceCClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key-not-real"
        self.tmp = tempfile.TemporaryDirectory()
        self.worktree = self.tmp.name
        self.store = MemoryClaimStore()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        if self._prior is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._prior

    def _run(self, auditor: ScriptAuditor | None = None, **overrides: object) -> dict:
        auditor = auditor or ScriptAuditor("PASS")
        fields: dict[str, object] = {
            "audit_requested": True,
            "repository": REPO,
            "issue_number": 47,
            "branch": BRANCH,
            "head": HEAD,
            "packets": [_packet()],
            "worktree_path": self.worktree,
            "store": self.store,
            "auditor": auditor,
            "evidence_bundle": _bundle(),
            "budget": AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=25.0,
                preflight_usd=0.0,
            ),
            "git_runner": _git(HEAD),
            "list_sessions": lambda: [],
            "list_processes": lambda _path: [],
            "now": lambda: NOW,
            "event": _event(),
            "record": _record(self.worktree),
        }
        fields.update(overrides)
        return run_exact_head_audit(**fields)  # type: ignore[arg-type]

    def test_idle_makes_zero_store_and_auditor_calls(self) -> None:
        class Boom(MemoryClaimStore):
            def load(self, issue_number: int):
                raise AssertionError("idle must not read the ledger")

        outcome = self._run(audit_requested=False, store=Boom())
        self.assertEqual(outcome["action"], "idle_noop")
        self.assertEqual(outcome["auditor_calls"], 0)
        self.assertEqual(outcome["checkpoint_writes"], 0)

    def test_ambiguous_and_stale_packet_make_zero_calls(self) -> None:
        auditor = ScriptAuditor("PASS")
        ambiguous = self._run(auditor=auditor, packets=[])
        self.assertEqual(ambiguous["action"], "ambiguous_packet")
        stale = self._run(auditor=auditor, packets=[_packet(head=OTHER)])
        self.assertEqual(stale["action"], "stale_head")
        inactive = self._run(auditor=auditor, packets=[_packet(status="QUEUED")])
        self.assertEqual(inactive["action"], "stale_head")
        self.assertEqual(auditor.calls, 0)
        self.assertEqual(self.store.writes, 0)

    def test_active_cursor_skips_auditor(self) -> None:
        auditor = ScriptAuditor("PASS")
        outcome = self._run(
            auditor=auditor,
            list_sessions=lambda: [
                PersistSession(session_id="s", workspace=self.worktree, status="Attached")
            ],
        )
        self.assertEqual(outcome["action"], "cursor_active_noop")
        self.assertEqual(auditor.calls, 0)
        self.assertEqual(self.store.writes, 0)

    def test_dirty_worktree_refuses_before_call(self) -> None:
        auditor = ScriptAuditor("PASS")
        outcome = self._run(auditor=auditor, git_runner=_git(HEAD, dirty=True))
        self.assertEqual(outcome["action"], "identity_refused")
        self.assertEqual(auditor.calls, 0)
        self.assertEqual(self.store.writes, 0)

    def test_incomplete_evidence_makes_zero_calls(self) -> None:
        auditor = ScriptAuditor("PASS")
        bundle = _bundle(evidence_status="INCOMPLETE", detail=SECRET)
        outcome = self._run(auditor=auditor, evidence_bundle=bundle)
        self.assertEqual(outcome["action"], "evidence_blocked")
        self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
        self.assertNotIn("sk-fake-secret", outcome["findings"] or "")
        self.assertEqual(auditor.calls, 0)
        self.assertEqual(self.store.writes, 0)

    def test_pass_rework_and_human_required_are_normalized_once(self) -> None:
        for verdict in ("PASS", "REWORK", "HUMAN_REQUIRED"):
            store = MemoryClaimStore()
            auditor = ScriptAuditor(verdict, findings=SECRET)
            outcome = self._run(auditor=auditor, store=store)
            self.assertEqual(outcome["action"], "audited")
            self.assertEqual(outcome["verdict"], verdict)
            self.assertEqual(auditor.calls, 1)
            self.assertEqual(store.writes, 3)
            self.assertNotIn(SECRET, json.dumps(outcome))
            self.assertNotIn("diff --git", json.dumps(outcome["telemetry"]))
            self.assertGreater(outcome["telemetry"]["duration_sec"], 0)
            again = ScriptAuditor("PASS")
            duplicate = self._run(auditor=again, store=store)
            self.assertEqual(duplicate["action"], "duplicate_completed")
            self.assertEqual(duplicate["verdict"], verdict)
            self.assertEqual(again.calls, 0)
            self.assertEqual(store.writes, 3)

    def test_live_claim_and_expired_restart_do_not_call_again(self) -> None:
        key = make_audit_claim_key(REPO, 47, HEAD)
        ledger = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            claims={
                key: AuditClaim(
                    repository=REPO,
                    issue_number=47,
                    branch=BRANCH,
                    target_sha=HEAD,
                    claim_key=key,
                    state="claimed",
                    month_id="2023-11",
                    claimed_at=NOW,
                    lease_seconds=SLICE_CLAIM_LEASE_SECONDS,
                )
            },
        )
        self.store.save(ledger, expected_sha=None)
        auditor = ScriptAuditor("PASS")
        live = self._run(auditor=auditor)
        self.assertEqual(live["action"], "duplicate_claim")
        self.assertEqual(auditor.calls, 0)

        expired_store = MemoryClaimStore()
        old = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            month_spent_usd=0.2,
            claims={
                key: AuditClaim(
                    repository=REPO,
                    issue_number=47,
                    branch=BRANCH,
                    target_sha=HEAD,
                    claim_key=key,
                    state="claimed",
                    month_id="2023-11",
                    claimed_at=NOW - SLICE_CLAIM_LEASE_SECONDS - 1,
                    lease_seconds=SLICE_CLAIM_LEASE_SECONDS,
                )
            },
        )
        expired_store.save(old, expected_sha=None)
        expired_store.save_month_spend(
            "2023-11", 0.2, repository=REPO, expected_sha=None
        )
        reconciled = self._run(auditor=auditor, store=expired_store)
        self.assertEqual(reconciled["action"], "reconciled")
        self.assertEqual(reconciled["verdict"], "HUMAN_REQUIRED")
        self.assertEqual(auditor.calls, 0)
        loaded, _sha = expired_store.load(47)
        assert loaded is not None
        self.assertEqual(loaded.claims[key].state, "completed")
        spent, _budget_sha = expired_store.load_month_spend("2023-11")
        self.assertAlmostEqual(spent, 0.2)
        second = self._run(auditor=ScriptAuditor("PASS"), store=expired_store)
        self.assertEqual(second["action"], "duplicate_completed")
        self.assertEqual(second["auditor_calls"], 0)

    def test_monthly_spend_survives_a_new_worker_and_blocks(self) -> None:
        first = ScriptAuditor("PASS", cost=0.20)
        done = self._run(
            auditor=first,
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=0.22,
                preflight_usd=0.0,
            ),
        )
        self.assertEqual(done["action"], "audited")
        self.assertEqual(first.calls, 1)
        spent, _sha = self.store.load_month_spend("2023-11")
        self.assertAlmostEqual(spent, 0.20)
        fresh = AuditBudget(
            per_run_hard_usd=10.0,
            monthly_hard_usd=0.22,
            month_spent_usd=0.0,
            preflight_usd=0.0,
        )
        second_auditor = ScriptAuditor("PASS", cost=0.20)
        bundle = _bundle()
        bundle["git"] = dict(bundle["git"])
        bundle["git"]["head"] = OTHER
        bundle["identity"] = dict(bundle["identity"])
        bundle["identity"]["head"] = OTHER
        blocked = self._run(
            auditor=second_auditor,
            head=OTHER,
            packets=[_packet(head=OTHER)],
            evidence_bundle=bundle,
            budget=fresh,
            git_runner=_git(OTHER),
            event=_event(OTHER),
            record=_record(self.worktree, OTHER),
        )
        self.assertEqual(blocked["action"], "budget_blocked")
        self.assertEqual(second_auditor.calls, 0)
        self.assertIn("monthly budget exceeded", blocked["findings"] or "")

    def test_soft_budget_blocks_before_call(self) -> None:
        auditor = ScriptAuditor("PASS")
        outcome = self._run(
            auditor=auditor,
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=25.0,
                preflight_usd=0.0,
                monthly_soft_usd=0.01,
            ),
        )
        self.assertEqual(outcome["action"], "budget_blocked")
        self.assertIn("soft budget", outcome["findings"] or "")
        self.assertEqual(auditor.calls, 0)
        self.assertEqual(self.store.writes, 0)

    def test_missing_key_is_human_required_without_call_or_claim(self) -> None:
        os.environ.pop("OPENAI_API_KEY", None)
        auditor = ScriptAuditor("PASS")
        outcome = self._run(auditor=auditor)
        self.assertEqual(outcome["action"], "missing_key")
        self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
        self.assertEqual(auditor.calls, 0)
        self.assertEqual(self.store.writes, 0)

    def test_provider_pass_persists_redacted_telemetry(self) -> None:
        transport_calls: list[dict] = []

        class Transport:
            def request_json(self, method, path, *, api_key, body=None):
                transport_calls.append({"method": method, "api_key": api_key, "body": body})
                return {
                    "id": "resp_c",
                    "status": "completed",
                    "output_text": json.dumps(
                        {"verdict": "PASS", "findings": SECRET, "target_sha": HEAD}
                    ),
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }

        provider = BoundedResponsesAuditProvider(
            transport=Transport(),  # type: ignore[arg-type]
            sleeper=lambda _s: None,
            git_runner=_git(HEAD),
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=25.0,
                preflight_usd=0.0,
            ),
        )
        outcome = self._run(auditor=provider)
        self.assertEqual(outcome["action"], "audited")
        self.assertEqual(outcome["verdict"], "PASS")
        self.assertEqual(len(transport_calls), 1)
        raw = self.store.load(47)[0].to_json()  # type: ignore[union-attr]
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("test-key-not-real", raw)
        self.assertNotIn("diff --git", raw)
        self.assertGreater(outcome["telemetry"]["duration_sec"], 0)

    def test_provider_charges_usage_once(self) -> None:
        class Transport:
            def request_json(self, method, path, *, api_key, body=None):
                return {
                    "id": "resp_once",
                    "status": "completed",
                    "output_text": json.dumps(
                        {"verdict": "PASS", "findings": "ok", "target_sha": HEAD}
                    ),
                    "usage": {"input_tokens": 10, "output_tokens": 2000},
                }

        provider = BoundedResponsesAuditProvider(
            transport=Transport(),  # type: ignore[arg-type]
            sleeper=lambda _s: None,
            git_runner=_git(HEAD),
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=0.07,
                preflight_usd=0.0,
            ),
        )
        outcome = self._run(
            auditor=provider,
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=0.07,
                preflight_usd=0.0,
            ),
        )
        self.assertEqual(outcome["action"], "audited")
        self.assertEqual(outcome["verdict"], "PASS")
        self.assertAlmostEqual(outcome["telemetry"]["estimated_cost_usd"], 0.04004)
        spent, _sha = self.store.load_month_spend("2023-11")
        self.assertAlmostEqual(spent, 0.04004)

    def test_monthly_budget_is_shared_across_work_packets(self) -> None:
        first = ScriptAuditor("PASS", cost=0.04)
        done = self._run(
            auditor=first,
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=0.07,
                preflight_usd=0.0,
            ),
        )
        self.assertEqual(done["verdict"], "PASS")
        self.assertEqual(first.calls, 1)
        second = ScriptAuditor("PASS", cost=0.04)
        blocked = self._run(
            auditor=second,
            issue_number=48,
            packets=[_packet(issue=48)],
            record=_record(self.worktree),
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=0.07,
                month_spent_usd=0.0,
                preflight_usd=0.0,
            ),
        )
        self.assertEqual(blocked["action"], "budget_blocked")
        self.assertEqual(second.calls, 0)
        spent, _sha = self.store.load_month_spend("2023-11")
        self.assertAlmostEqual(spent, 0.04)

    def test_month_spend_cas_rejects_a_stale_writer(self) -> None:
        sha = self.store.save_month_spend(
            "2023-11", 0.04, repository=REPO, expected_sha=None
        )
        with self.assertRaises(CheckpointCasConflict):
            self.store.save_month_spend(
                "2023-11", 0.08, repository=REPO, expected_sha="stale"
            )
        self.store.save_month_spend(
            "2023-11", 0.08, repository=REPO, expected_sha=sha
        )
        spent, _sha = self.store.load_month_spend("2023-11")
        self.assertAlmostEqual(spent, 0.08)


class GitHubClaimStoreTests(unittest.TestCase):
    def test_contents_put_is_cas_bound_and_redacts_findings(self) -> None:
        key = make_audit_claim_key(REPO, 47, HEAD)
        ledger = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            claims={
                key: AuditClaim(
                    repository=REPO,
                    issue_number=47,
                    branch=BRANCH,
                    target_sha=HEAD,
                    claim_key=key,
                    state="completed",
                    month_id="2023-11",
                    claimed_at=NOW,
                    lease_seconds=60,
                    verdict="REWORK",
                    findings=SECRET,
                    telemetry={
                        "model": "gpt-5.6-sol",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "target_sha": HEAD,
                        "verdict": "REWORK",
                        "duration_sec": 0.1,
                    },
                )
            },
        )
        seen: list[dict] = []
        endpoints: list[str] = []
        posts: list[dict] = []
        base_sha = "c" * 40
        branch_ready = {"ok": False}

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            endpoint = (
                argv[argv.index("--input") - 1] if "--input" in argv else argv[-1]
            )
            endpoints.append(endpoint)
            method = "GET"
            if "--method" in argv:
                method = argv[argv.index("--method") + 1]
            if endpoint == f"repos/{REPO}":
                return _completed(argv, 0, {"default_branch": "main"})
            if endpoint.endswith("/git/ref/heads/main"):
                return _completed(argv, 0, {"object": {"sha": base_sha}})
            if endpoint.endswith(f"/git/ref/heads/{FINAL_AUDIT_CLAIM_BRANCH}"):
                if branch_ready["ok"]:
                    return _completed(
                        argv,
                        0,
                        {"object": {"sha": base_sha}, "ref": f"refs/heads/{FINAL_AUDIT_CLAIM_BRANCH}"},
                    )
                return _completed(argv, 1, stderr="404 Not Found")
            if method == "POST" and endpoint.endswith("/git/refs"):
                body = json.loads(Path(argv[argv.index("--input") + 1]).read_text(encoding="utf-8"))
                posts.append(body)
                branch_ready["ok"] = True
                return _completed(argv, 0, {"ref": body["ref"], "object": {"sha": base_sha}})
            if method == "PUT":
                body = json.loads(Path(argv[argv.index("--input") + 1]).read_text(encoding="utf-8"))
                seen.append(body)
                if body.get("sha") == "stale":
                    return _completed(argv, 1, stderr="HTTP 409 conflict")
                return _completed(argv, 0, {"sha": "blob1", "content": {"sha": "blob1"}})
            return _completed(argv, 1, stderr="404 Not Found")

        store = GitHubContentsClaimStore(REPO, command_runner=runner)
        created = store.ensure_claim_branch()
        self.assertEqual(created["action"], "created")
        self.assertEqual(created["branch"], FINAL_AUDIT_CLAIM_BRANCH)
        self.assertEqual(posts[0]["ref"], f"refs/heads/{FINAL_AUDIT_CLAIM_BRANCH}")
        self.assertEqual(posts[0]["sha"], base_sha)
        again = store.ensure_claim_branch()
        self.assertEqual(again["action"], "exists")
        self.assertEqual(len(posts), 1)
        sha = store.save(ledger, expected_sha=None)
        self.assertEqual(sha, "blob1")
        self.assertEqual(seen[0]["branch"], FINAL_AUDIT_CLAIM_BRANCH)
        self.assertNotEqual(seen[0]["branch"], "main")
        self.assertNotIn("sha", seen[0])
        decoded = base64.b64decode(seen[0]["content"]).decode("utf-8")
        self.assertNotIn("sk-fake-secret", decoded)
        self.assertIn("REWORK", decoded)
        self.assertTrue(
            any(".atlas/final-audit/claims/issue-47.json" in item for item in endpoints)
        )
        with self.assertRaises(CheckpointCasConflict):
            store.save(ledger, expected_sha="stale")

    def test_concurrent_branch_create_is_idempotent(self) -> None:
        base_sha = "d" * 40

        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            endpoint = (
                argv[argv.index("--input") - 1] if "--input" in argv else argv[-1]
            )
            if endpoint == f"repos/{REPO}":
                return _completed(argv, 0, {"default_branch": "main"})
            if endpoint.endswith("/git/ref/heads/main"):
                return _completed(argv, 0, {"object": {"sha": base_sha}})
            if endpoint.endswith(f"/git/ref/heads/{FINAL_AUDIT_CLAIM_BRANCH}"):
                if "--method" not in argv:
                    # First probe misses; the post-conflict verify hits.
                    if getattr(runner, "posted", False):
                        return _completed(argv, 0, {"object": {"sha": base_sha}})
                    return _completed(argv, 1, stderr="404 Branch not found")
            if "--method" in argv and argv[argv.index("--method") + 1] == "POST":
                runner.posted = True  # type: ignore[attr-defined]
                return _completed(
                    argv,
                    1,
                    stderr='422 {"message":"Reference already exists"}',
                )
            return _completed(argv, 1, stderr="404 Not Found")

        store = GitHubContentsClaimStore(REPO, command_runner=runner)
        outcome = store.ensure_claim_branch()
        self.assertEqual(outcome["action"], "exists_race")
        self.assertEqual(outcome["branch"], FINAL_AUDIT_CLAIM_BRANCH)

    def test_claim_branch_refuses_the_default_branch(self) -> None:
        def runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
            return _completed(argv, 0, {"default_branch": "main"})

        store = GitHubContentsClaimStore(
            REPO, command_runner=runner, branch="main"
        )
        with self.assertRaises(ValidationError) as caught:
            store.ensure_claim_branch()
        self.assertIn("default branch", str(caught.exception))


def _completed(
    argv: list[str],
    code: int,
    payload: dict | None = None,
    *,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    stdout = json.dumps(payload) if payload is not None else ""
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


if __name__ == "__main__":
    unittest.main()
