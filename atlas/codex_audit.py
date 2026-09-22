"""Codex CLI read-only audit provider for Autonomous Work Controller (ADR-0006).

Primary PoC auditor: Codex CLI authenticated with the owner's ChatGPT plan.
No OpenAI API key is required on this path.

Default mode gathers a deterministic evidence bundle in-process (git, Work
Packet, tests, CI) and asks Codex to judge only that bundle with
tools/apps/browser/shell disabled. Codex local shell is not required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from atlas.provenance import ValidationError
from atlas.work_controller import (
    AUDIT_VERDICTS,
    AuditResult,
    CompletionEvent,
    WorkstreamRecord,
    WorktreeIdentity,
    _looks_like_secret,
    default_git_runner,
    normalize_github_repository,
    parse_audit_verdict_payload,
    require_clean_porcelain,
    validate_worktree_identity,
)

GitRunner = Callable[[list[str], str], str]
CodexRunner = Callable[[list[str], str, str], str]
CommandRunner = Callable[[list[str], str], subprocess.CompletedProcess[str]]

DEFAULT_CODEX_TIMEOUT_SEC = 180
DEFAULT_TEST_TIMEOUT_SEC = 120
DEFAULT_MAX_DIFF_CHARS = 12000
DEFAULT_MAX_PACKET_CHARS = 8000
DEFAULT_MAX_TEST_CHARS = 8000
DEFAULT_MAX_CI_CHARS = 4000
DEFAULT_MAX_REVIEW_CHARS = 8000
DEFAULT_MAX_STATUS_CHARS = 2000

# Features disabled so Codex judges the supplied bundle only.
# Deliberately omits skill_search / skill_mcp_dependency_install: nonessential
# for bounded audits and unsupported on some older Codex builds.
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "apps",
    "computer_use",
    "code_mode_host",
    "plugins",
    "plugin_sharing",
    "remote_plugin",
    "hooks",
    "multi_agent",
    "multi_agent_v2",
)

CODEX_AUDIT_INSTRUCTIONS = """You are an independent read-only engineering auditor for DataRelay Atlas.
Judge ONLY the deterministic EVIDENCE_BUNDLE embedded in this prompt.
Do not use tools, shell, apps, browser, MCP, or web search.
Do not edit files, commit, push, or request additional runtime access.
Return ONLY a JSON object with keys:
  verdict: one of PASS, REWORK, HUMAN_REQUIRED
  findings: concise evidence-backed string (include paths/lines when useful)
PASS only when the bundle satisfies the workstream goal and acceptance constraints,
including that PR review evidence is inspectable and shows no unresolved actionable
review feedback when a PR is in scope.
REWORK when actionable defects remain that a fresh /work-resume cycle can fix.
HUMAN_REQUIRED when owner judgment, credentials, or out-of-scope decisions are needed,
or when the bundle is insufficient to judge safely, or when PR review evidence is
ERROR/unavailable (fail closed; never PASS when review feedback cannot be inspected).
"""


def _run_capture(
    argv: list[str],
    cwd: str,
    *,
    timeout_sec: int,
    runner: CommandRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    if runner is not None:
        return runner(argv, cwd)
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        missing = argv[0] if argv else "command"
        return subprocess.CompletedProcess(
            argv,
            127,
            stdout="",
            stderr=f"{missing} not found on PATH: {exc}",
        )


def _trim(text: str, limit: int) -> str:
    raw = text.strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "\n...[truncated]...\n"


def _trim_marked(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(bounded_text, truncated)`` for fail-closed collectors."""
    raw = text.strip()
    if len(raw) <= limit:
        return raw, False
    return raw[:limit] + "\n...[truncated]...\n", True


def _untracked_paths_from_status(status_text: str) -> list[str]:
    """Parse ``git status --short`` lines for untracked (``??``) paths only.

    Returns path names only; never reads file contents.
    """
    paths: list[str] = []
    for line in str(status_text or "").splitlines():
        if line.startswith("?? "):
            path = line[3:].strip()
            if path:
                paths.append(path)
    return paths


def _bounded_path_list(paths: list[str], *, max_chars: int = 2000) -> list[str]:
    """Bound a list of path strings without reading file contents."""
    bounded: list[str] = []
    remaining = max_chars
    for path in paths:
        entry = path if len(path) <= 240 else path[:240] + "..."
        encoded_len = len(entry) + (1 if bounded else 0)
        if encoded_len > remaining and bounded:
            break
        bounded.append(entry)
        remaining = max(0, remaining - encoded_len)
        if remaining <= 0:
            break
    return bounded


def collect_git_evidence(
    worktree_path: str,
    *,
    base_ref: str | None = None,
    git_runner: GitRunner | None = None,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    max_status_chars: int = DEFAULT_MAX_STATUS_CHARS,
) -> dict:
    """Collect bounded local git evidence outside Codex.

    Porcelain ``status`` is stored only in bounded form. Completeness is
    reported as ``evidence_status`` (OK / INCOMPLETE / ERROR) so truncated
    diffs/status or untracked paths whose contents are not included cannot
    silently permit PASS. ``status_digest`` remains informational only;
    autonomous audit/dispatch gates require clean porcelain instead.
    """
    runner = git_runner or default_git_runner
    cwd = str(Path(worktree_path).resolve())
    evidence: dict = {"collector": "atlas.codex_audit.collect_git_evidence"}
    truncated_fields: list[str] = []
    raw_status = ""
    try:
        raw_status = runner(["git", "status", "--short", "--branch"], cwd)
        evidence["status_digest"] = hashlib.sha256(
            raw_status.encode("utf-8")
        ).hexdigest()
        bounded_status, status_truncated = _trim_marked(
            raw_status, max_status_chars
        )
        evidence["status"] = bounded_status
        if status_truncated:
            truncated_fields.append("status")
    except ValidationError as exc:
        evidence["status"] = f"ERROR: {exc}"
        evidence["status_digest"] = ""

    for label, argv in (
        ("head", ["git", "rev-parse", "HEAD"]),
        ("branch", ["git", "branch", "--show-current"]),
        ("origin", ["git", "remote", "get-url", "origin"]),
    ):
        try:
            evidence[label] = runner(argv, cwd)
        except ValidationError as exc:
            evidence[label] = f"ERROR: {exc}"
    diff_ref = base_ref or "origin/main"
    evidence["base_ref"] = diff_ref
    try:
        evidence["diff_stat"] = runner(
            ["git", "diff", "--stat", f"{diff_ref}...HEAD"], cwd
        )
        diff = runner(["git", "diff", "--find-renames", f"{diff_ref}...HEAD"], cwd)
        bounded, was_truncated = _trim_marked(diff, max_diff_chars)
        evidence["diff"] = bounded
        if was_truncated:
            truncated_fields.append("diff")
    except ValidationError as exc:
        evidence["diff_stat"] = f"ERROR: {exc}"
        evidence["diff"] = f"ERROR: {exc}"
    # Include uncommitted work so local audits see dirty-tree changes.
    for label, argv in (
        ("workdir_diff_stat", ["git", "diff", "--stat", "HEAD"]),
        ("workdir_diff", ["git", "diff", "--find-renames", "HEAD"]),
        ("staged_diff_stat", ["git", "diff", "--cached", "--stat"]),
        ("staged_diff", ["git", "diff", "--cached", "--find-renames"]),
    ):
        try:
            value = runner(argv, cwd)
            if label.endswith("_diff"):
                bounded, was_truncated = _trim_marked(value, max_diff_chars)
                value = bounded
                if was_truncated:
                    truncated_fields.append(label)
            evidence[label] = value
        except ValidationError as exc:
            evidence[label] = f"ERROR: {exc}"

    # Parse untracked from temporary raw status only; never retain raw.
    untracked = _untracked_paths_from_status(raw_status)
    if untracked:
        evidence["untracked_files"] = _bounded_path_list(untracked)
    del raw_status

    audited_keys = (
        "status",
        "head",
        "branch",
        "origin",
        "diff_stat",
        "diff",
        "workdir_diff_stat",
        "workdir_diff",
        "staged_diff_stat",
        "staged_diff",
    )
    error_fields = [
        key
        for key in audited_keys
        if str(evidence.get(key) or "").startswith("ERROR:")
    ]
    if error_fields:
        evidence["evidence_status"] = "ERROR"
        evidence["detail"] = (
            "git evidence collection failed; "
            f"error fields={','.join(error_fields)}"
        )
        evidence["error_fields"] = error_fields
    elif truncated_fields or untracked:
        evidence["evidence_status"] = "INCOMPLETE"
        details: list[str] = []
        if truncated_fields:
            details.append(
                "git evidence truncated under section budget; "
                f"truncated fields={','.join(truncated_fields)}"
            )
            evidence["truncated_fields"] = truncated_fields
        if untracked:
            details.append(
                "untracked files present; contents not included in git evidence "
                f"(count={len(untracked)})"
            )
        evidence["detail"] = "; ".join(details)
    else:
        evidence["evidence_status"] = "OK"
    return evidence


def collect_work_packet_evidence(
    *,
    repository: str,
    issue_number: int,
    command_runner: CommandRunner | None = None,
    max_chars: int = DEFAULT_MAX_PACKET_CHARS,
) -> dict:
    """Fetch the active Work Packet issue body via gh (no secrets expected)."""
    cwd = str(Path.cwd())
    argv = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repository,
        "--json",
        "number,title,state,body,updatedAt",
    ]
    try:
        completed = _run_capture(
            argv, cwd, timeout_sec=60, runner=command_runner
        )
    except subprocess.TimeoutExpired:
        return {
            "collector": "atlas.codex_audit.collect_work_packet_evidence",
            "status": "ERROR",
            "detail": "gh issue view timed out",
        }
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return {
            "collector": "atlas.codex_audit.collect_work_packet_evidence",
            "status": "ERROR",
            "detail": detail[:500] or f"exit {completed.returncode}",
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "collector": "atlas.codex_audit.collect_work_packet_evidence",
            "status": "ERROR",
            "detail": "gh issue view returned non-JSON",
        }
    body = str(payload.get("body") or "")
    bounded_body, truncated = _trim_marked(body, max_chars)
    result: dict = {
        "collector": "atlas.codex_audit.collect_work_packet_evidence",
        "number": payload.get("number"),
        "title": payload.get("title"),
        "state": payload.get("state"),
        "updated_at": payload.get("updatedAt"),
        "body": bounded_body,
    }
    if truncated:
        result["status"] = "INCOMPLETE"
        result["detail"] = (
            "work packet body truncated under max_chars budget"
        )
        result["truncated"] = True
        result["original_body_chars"] = len(body.strip())
        result["max_chars"] = max_chars
    else:
        result["status"] = "OK"
    return result


def collect_test_evidence(
    worktree_path: str,
    *,
    command_runner: CommandRunner | None = None,
    max_chars: int = DEFAULT_MAX_TEST_CHARS,
) -> dict:
    """Run the cheap Atlas unit suite and capture a bounded transcript."""
    cwd = str(Path(worktree_path).resolve())
    env = dict(os.environ)
    env["PYTHONPATH"] = cwd + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    argv = ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
    try:
        if command_runner is not None:
            completed = command_runner(argv, cwd)
        else:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TEST_TIMEOUT_SEC,
                env=env,
            )
    except subprocess.TimeoutExpired:
        return {
            "collector": "atlas.codex_audit.collect_test_evidence",
            "status": "ERROR",
            "detail": f"unittest timed out after {DEFAULT_TEST_TIMEOUT_SEC}s",
            "command": argv,
        }
    transcript = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    return {
        "collector": "atlas.codex_audit.collect_test_evidence",
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "exit_code": completed.returncode,
        "command": argv,
        "transcript": _trim(transcript, max_chars),
    }


def collect_ci_evidence(
    *,
    repository: str,
    head: str,
    command_runner: CommandRunner | None = None,
    max_chars: int = DEFAULT_MAX_CI_CHARS,
) -> dict:
    """Collect PR/check evidence for the current HEAD when available."""
    cwd = str(Path.cwd())
    search_argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        repository,
        "--state",
        "all",
        "--search",
        head,
        "--json",
        "number,url,state,title,headRefOid",
        "--limit",
        "5",
    ]
    try:
        completed = _run_capture(
            search_argv, cwd, timeout_sec=60, runner=command_runner
        )
    except subprocess.TimeoutExpired:
        return {
            "collector": "atlas.codex_audit.collect_ci_evidence",
            "status": "ERROR",
            "detail": "gh pr list timed out",
        }
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return {
            "collector": "atlas.codex_audit.collect_ci_evidence",
            "status": "ERROR",
            "detail": detail[:500] or f"exit {completed.returncode}",
            "head": head,
        }
    try:
        prs = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return {
            "collector": "atlas.codex_audit.collect_ci_evidence",
            "status": "ERROR",
            "detail": "gh pr list returned non-JSON",
            "head": head,
        }
    if not isinstance(prs, list) or not prs:
        return {
            "collector": "atlas.codex_audit.collect_ci_evidence",
            "status": "ABSENT",
            "detail": "no PR found for head",
            "head": head,
        }
    chosen = None
    head_l = head.lower()
    for item in prs:
        oid = str(item.get("headRefOid") or "").lower()
        if oid and (oid == head_l or oid.startswith(head_l[:12]) or head_l.startswith(oid[:12])):
            chosen = item
            break
    if chosen is None:
        return {
            "collector": "atlas.codex_audit.collect_ci_evidence",
            "status": "ABSENT",
            "detail": "no PR headRefOid matched audited head",
            "head": head,
            "candidates": prs,
        }
    number = chosen.get("number")
    checks_argv = [
        "gh",
        "pr",
        "checks",
        str(number),
        "--repo",
        repository,
    ]
    try:
        checks = _run_capture(
            checks_argv, cwd, timeout_sec=60, runner=command_runner
        )
    except subprocess.TimeoutExpired:
        return {
            "collector": "atlas.codex_audit.collect_ci_evidence",
            "status": "ERROR",
            "detail": "gh pr checks timed out",
            "pr": chosen,
        }
    status, detail = classify_gh_pr_checks_result(
        checks.returncode, checks.stdout or "", checks.stderr or ""
    )
    result: dict = {
        "collector": "atlas.codex_audit.collect_ci_evidence",
        "status": status,
        "head": head,
        "pr": chosen,
        "checks_exit_code": checks.returncode,
        "checks": _trim(
            "\n".join(
                part for part in (checks.stdout, checks.stderr) if part
            ).strip(),
            max_chars,
        ),
    }
    if detail:
        result["detail"] = _trim(detail, 500)
    return result


def classify_gh_pr_checks_result(
    returncode: int, stdout: str, stderr: str
) -> tuple[str, str]:
    """Map ``gh pr checks`` exit + output to OK|PENDING|FAIL|ERROR.

    ``gh pr checks --help`` documents exit 0 (success) and 8 (pending).
    ``gh help exit-codes`` documents exit 1 (command failed) and 4 (auth).
    Non-0/8 exits are not proof of a failed check unless parseable check
    rows show a failure state; otherwise return ERROR so the deterministic
    gate fails closed to HUMAN_REQUIRED instead of autonomous REWORK.
    """
    if returncode == 0:
        return "OK", ""
    if returncode == 8:
        return "PENDING", ""
    if returncode == 4:
        return "ERROR", "gh pr checks authentication failure"

    states: list[str] = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            states.append(parts[1].strip().lower())

    fail_states = {
        "fail",
        "failed",
        "failure",
        "cancel",
        "cancelled",
        "canceled",
        "timed_out",
        "timedout",
        "error",
        "action_required",
        "startup_failure",
    }
    pending_states = {
        "pending",
        "queued",
        "in_progress",
        "expected",
        "waiting",
        "requested",
    }
    if states:
        if any(state in fail_states for state in states):
            return "FAIL", ""
        if any(state in pending_states for state in states):
            return "PENDING", ""
        return (
            "ERROR",
            f"gh pr checks exit {returncode} with unrecognized row states: "
            f"{sorted(set(states))}",
        )

    detail = "\n".join(part for part in (stdout, stderr) if part).strip()
    return (
        "ERROR",
        detail[:500] or f"gh pr checks command failure (exit {returncode})",
    )


def _bounded_review_items(
    raw: object, *, max_chars: int
) -> tuple[list[dict], bool]:
    """Normalize review/comment payloads into a bounded list of dicts.

    Returns ``(items, truncated)``. ``truncated`` is True when at least one
    eligible entry was omitted because the character budget was exhausted, or
    when any included body was shortened by the per-item cap (so actionable
    text in a trimmed tail is never silently dropped under status=OK).
    Callers must fail closed on truncation rather than report status=OK.
    """
    if not isinstance(raw, list):
        return [], False
    eligible = [entry for entry in raw if isinstance(entry, dict)]
    items: list[dict] = []
    remaining = max_chars
    truncated = False
    for idx, entry in enumerate(eligible):
        body = str(entry.get("body") or "")
        body_limit = min(1200, max(200, remaining))
        bounded_body = _trim(body, body_limit)
        if len(body.strip()) > body_limit:
            # Per-item cap dropped a tail; keep the bounded body but fail closed.
            truncated = True
        item = {
            "id": entry.get("id"),
            "user": ((entry.get("user") or {}) if isinstance(entry.get("user"), dict) else {}).get(
                "login"
            ),
            "state": entry.get("state"),
            "commit_id": entry.get("commit_id"),
            "original_commit_id": entry.get("original_commit_id"),
            "submitted_at": entry.get("submitted_at"),
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
            "path": entry.get("path"),
            "line": entry.get("line"),
            "original_line": entry.get("original_line"),
            "body": bounded_body,
        }
        encoded = json.dumps(item, sort_keys=True)
        if len(encoded) > remaining and items:
            truncated = True
            break
        items.append(item)
        remaining = max(0, remaining - len(encoded))
        if remaining <= 0 and idx + 1 < len(eligible):
            truncated = True
            break
    return items, truncated


def _flatten_slurped_gh_pages(
    raw: object, *, path: str
) -> tuple[list[object], str | None]:
    """Flatten ``gh api --paginate --slurp`` output into one list.

    ``--paginate`` alone emits each page as a separate JSON value; ``--slurp``
    wraps those pages in one outer JSON array. Every page must itself be a JSON
    array (list endpoints). Returns ``(flat_items, error_detail)``; on any
    malformation ``error_detail`` is set and callers must fail closed.
    """
    if not isinstance(raw, list):
        return [], f"gh api {path} --slurp returned non-array"
    flat: list[object] = []
    for page_idx, page in enumerate(raw):
        if not isinstance(page, list):
            return [], (
                f"gh api {path} --slurp page {page_idx} is not a JSON array"
            )
        flat.extend(page)
    return flat, None


def collect_pr_review_evidence(
    *,
    repository: str,
    pr_number: int,
    command_runner: CommandRunner | None = None,
    max_chars: int = DEFAULT_MAX_REVIEW_CHARS,
) -> dict:
    """Collect machine-observable PR review feedback (fail closed).

    Surfaces: submitted reviews, inline review comments, and top-level PR
    conversation comments. Each GitHub list endpoint is fetched with
    ``gh api --paginate --slurp``, pages are flattened, then bounded. When a
    section budget truncates uninspected entries, status is INCOMPLETE so the
    auditor fails closed instead of terminal PASS.
    """
    cwd = str(Path.cwd())
    pull_base = f"repos/{repository}/pulls/{pr_number}"
    issue_base = f"repos/{repository}/issues/{pr_number}"
    sections: dict[str, object] = {
        "collector": "atlas.codex_audit.collect_pr_review_evidence",
        "repository": repository,
        "pr_number": pr_number,
    }
    section_budget = max(1, max_chars // 3)
    truncated_sections: list[str] = []
    for label, path in (
        ("reviews", f"{pull_base}/reviews"),
        ("inline_comments", f"{pull_base}/comments"),
        ("conversation_comments", f"{issue_base}/comments"),
    ):
        argv = ["gh", "api", "--paginate", "--slurp", path]
        try:
            completed = _run_capture(
                argv, cwd, timeout_sec=60, runner=command_runner
            )
        except subprocess.TimeoutExpired:
            return {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ERROR",
                "detail": f"gh api {path} timed out",
                "repository": repository,
                "pr_number": pr_number,
            }
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ERROR",
                "detail": detail[:500] or f"exit {completed.returncode}",
                "repository": repository,
                "pr_number": pr_number,
                "failed_section": label,
            }
        try:
            slurped = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ERROR",
                "detail": f"gh api {path} returned non-JSON",
                "repository": repository,
                "pr_number": pr_number,
                "failed_section": label,
            }
        payload, flatten_error = _flatten_slurped_gh_pages(slurped, path=path)
        if flatten_error:
            return {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ERROR",
                "detail": flatten_error,
                "repository": repository,
                "pr_number": pr_number,
                "failed_section": label,
            }
        items, truncated = _bounded_review_items(
            payload, max_chars=section_budget
        )
        sections[label] = items
        if truncated:
            truncated_sections.append(label)
    if truncated_sections:
        sections["status"] = "INCOMPLETE"
        sections["detail"] = (
            "review evidence truncated under section budget; "
            f"incomplete sections={','.join(truncated_sections)}"
        )
        sections["truncated_sections"] = truncated_sections
    else:
        sections["status"] = "OK"
    return sections


def collect_audit_evidence_bundle(
    event: CompletionEvent,
    record: WorkstreamRecord,
    *,
    identity: WorktreeIdentity,
    base_ref: str | None = None,
    git_runner: GitRunner | None = None,
    command_runner: CommandRunner | None = None,
    include_tests: bool = True,
    include_ci: bool = True,
    include_work_packet: bool = True,
) -> dict:
    """Build the deterministic evidence bundle Codex will judge."""
    bundle = {
        "schema": "awc.codex_evidence_bundle.v1",
        "resume_command": "/work-resume",
        "event": {
            "event_id": event.event_id,
            "workstream": event.workstream,
            "issue_number": event.issue_number,
            "branch": event.branch,
            "head": event.head,
            "attempt": event.attempt,
            "session_id": event.session_id,
        },
        "record": {
            "repository": record.repository,
            "worktree_path": record.worktree_path,
            "branch": record.branch,
            "expected_head": record.expected_head,
            "state": record.state,
            "attempt": record.attempt,
            "max_attempts": record.max_attempts,
            "last_audit_verdict": record.last_audit_verdict,
            "last_findings": record.last_findings,
        },
        "identity": {
            "repository": identity.repository,
            "branch": identity.branch,
            "head": identity.head,
            "toplevel": identity.toplevel,
            "worktree_path": identity.worktree_path,
        },
        "git": collect_git_evidence(
            identity.worktree_path,
            base_ref=base_ref,
            git_runner=git_runner,
        ),
    }
    if include_work_packet:
        bundle["work_packet"] = collect_work_packet_evidence(
            repository=identity.repository,
            issue_number=record.issue_number,
            command_runner=command_runner,
        )
    if include_tests:
        bundle["tests"] = collect_test_evidence(
            identity.worktree_path,
            command_runner=command_runner,
        )
    if include_ci:
        ci = collect_ci_evidence(
            repository=identity.repository,
            head=identity.head,
            command_runner=command_runner,
        )
        bundle["ci"] = ci
        # Fail closed: when a PR is in scope, review feedback must be inspectable.
        pr = ci.get("pr") if isinstance(ci.get("pr"), dict) else None
        pr_number = pr.get("number") if pr else None
        if ci.get("status") in {"OK", "PENDING", "FAIL"} and pr_number is not None:
            bundle["pr_reviews"] = collect_pr_review_evidence(
                repository=identity.repository,
                pr_number=int(pr_number),
                command_runner=command_runner,
            )
        elif ci.get("status") == "ABSENT":
            bundle["pr_reviews"] = {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ABSENT",
                "detail": "no PR found for head; review evidence not applicable",
            }
        else:
            bundle["pr_reviews"] = {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ERROR",
                "detail": "ci evidence unavailable; review evidence fail-closed",
            }
    return bundle


# Backward-compatible name used by earlier tests/docs.
def collect_local_audit_evidence(
    worktree_path: str,
    *,
    base_ref: str | None = None,
    git_runner: GitRunner | None = None,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> str:
    git = collect_git_evidence(
        worktree_path,
        base_ref=base_ref,
        git_runner=git_runner,
        max_diff_chars=max_diff_chars,
    )
    chunks = [f"## {key}\n{value}\n" for key, value in git.items()]
    return "\n".join(chunks).strip() + "\n"


def build_codex_audit_prompt(
    event: CompletionEvent,
    record: WorkstreamRecord,
    *,
    identity: WorktreeIdentity,
    evidence_bundle: dict,
    base_ref: str | None = None,
) -> str:
    """Bounded judge-only prompt; never includes credentials."""
    payload = {
        "role": "awc_codex_audit",
        "mode": "judge_evidence_bundle_only",
        "workstream": record.workstream,
        "issue_number": record.issue_number,
        "repository": identity.repository,
        "worktree_path": identity.worktree_path,
        "branch": identity.branch,
        "head": identity.head,
        "base_ref": base_ref or evidence_bundle.get("git", {}).get("base_ref", ""),
        "event_id": event.event_id,
        "attempt": event.attempt,
        "max_attempts": record.max_attempts,
        "controller_state": record.state,
        "resume_command": "/work-resume",
        "rules": [
            "judge_bundle_only",
            "tools_disabled",
            "no_shell",
            "no_apps",
            "no_browser",
            "no_edits",
            "no_commits",
            "no_pushes",
            "return_json_verdict_only",
        ],
    }
    return (
        CODEX_AUDIT_INSTRUCTIONS
        + "\n\nAUDIT_CONTEXT_JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nEVIDENCE_BUNDLE_JSON:\n"
        + json.dumps(evidence_bundle, indent=2, sort_keys=True)
        + "\n"
    )


def build_codex_audit_command(
    worktree_path: str,
    *,
    last_message_path: str,
    model: str | None = None,
) -> list[str]:
    """Fixed non-interactive Codex argv for judge-only bundle audits.

    Ignores user config/rules while preserving ChatGPT-plan auth. Disables
    shell/apps/browser/computer/plugins/hooks/multi-agent so Codex judges only
    the pre-collected evidence bundle. Prompt is on stdin (`-`).
    """
    cmd = [
        "codex",
        "exec",
        "-C",
        str(Path(worktree_path).resolve()),
        "-s",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--color",
        "never",
        "-c",
        'web_search="disabled"',
    ]
    for feature in CODEX_DISABLED_FEATURES:
        cmd.extend(["--disable", feature])
    cmd.extend(["-o", last_message_path, "-"])
    if model:
        # Insert after `exec`
        cmd[2:2] = ["-m", model]
    return cmd


def default_codex_runner(command: list[str], prompt: str, cwd: str) -> str:
    """Run Codex exec and return the last-message file contents."""
    if not command or command[0] != "codex":
        raise ValidationError(f"refusing non-codex command: {command!r}")
    if "-s" not in command or "read-only" not in command:
        raise ValidationError("codex audit runner requires read-only sandbox")
    if "--ignore-user-config" not in command:
        raise ValidationError("codex audit runner requires --ignore-user-config")
    if "--disable" not in command or "shell_tool" not in command:
        raise ValidationError("codex audit runner requires shell_tool disabled")
    if "apps" not in command or "browser_use" not in command:
        raise ValidationError("codex audit runner requires apps/browser disabled")
    if "-o" not in command:
        raise ValidationError("codex audit runner requires -o last-message path")
    out_idx = command.index("-o") + 1
    last_message_path = command[out_idx]
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=DEFAULT_CODEX_TIMEOUT_SEC,
            env=_codex_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(
            f"codex exec timed out after {DEFAULT_CODEX_TIMEOUT_SEC}s"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValidationError(
            f"codex exec failed (exit {completed.returncode}): {detail[:500]}"
        )
    path = Path(last_message_path)
    if not path.is_file():
        raise ValidationError("codex exec produced no last-message output file")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValidationError("codex exec last-message output was empty")
    return text


def _codex_child_env() -> dict[str, str]:
    """Pass through process env without injecting API keys."""
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    return env


def _revalidate_clean_audited_snapshot(
    event: CompletionEvent,
    record: WorkstreamRecord,
    *,
    git_runner: GitRunner | None,
) -> str | None:
    """Revalidate exact repo/branch/HEAD and require clean porcelain.

    Returns an error detail string on mismatch/dirty state; None when the
    autonomous snapshot is still valid. Never raises for expected drift.
    """
    try:
        validate_worktree_identity(
            record.worktree_path,
            repository=record.repository,
            branch=event.branch,
            expected_head=event.head,
            git_runner=git_runner,
        )
    except ValidationError as exc:
        return f"worktree identity drift: {exc}"

    try:
        require_clean_porcelain(
            record.worktree_path,
            git_runner=git_runner,
        )
    except ValidationError as exc:
        return f"worktree dirty: {exc}"
    return None


def _deterministic_gate_before_codex(bundle: dict) -> AuditResult | None:
    """Short-circuit known deterministic failures/errors before spending Codex.

    tests PASS / CI OK|ABSENT => continue (return None)
    tests FAIL / CI FAIL => REWORK
    tests ERROR / CI PENDING|ERROR => HUMAN_REQUIRED
    Missing sections are ignored (offline/overrides may omit them).
    """
    tests = bundle.get("tests")
    if isinstance(tests, dict):
        status = str(tests.get("status") or "")
        if status == "FAIL":
            detail = str(tests.get("detail") or tests.get("transcript") or "")
            return AuditResult(
                verdict="REWORK",
                findings=(
                    "deterministic gate: tests FAIL before Codex; "
                    f"{detail[:300]}".strip()
                ),
            )
        if status == "ERROR":
            detail = str(tests.get("detail") or "")
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=(
                    "deterministic gate: tests ERROR before Codex; "
                    f"{detail[:300]}".strip()
                ),
            )
        if status and status != "PASS":
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=(
                    "deterministic gate: unrecognized tests status "
                    f"{status!r} before Codex"
                ),
            )

    ci = bundle.get("ci")
    if isinstance(ci, dict):
        status = str(ci.get("status") or "")
        if status == "FAIL":
            detail = str(ci.get("detail") or ci.get("checks") or "")
            return AuditResult(
                verdict="REWORK",
                findings=(
                    "deterministic gate: CI FAIL before Codex; "
                    f"{detail[:300]}".strip()
                ),
            )
        if status in {"PENDING", "ERROR"}:
            detail = str(ci.get("detail") or ci.get("checks") or "")
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=(
                    f"deterministic gate: CI {status} before Codex; "
                    f"{detail[:300]}".strip()
                ),
            )
        if status and status not in {"OK", "ABSENT"}:
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=(
                    "deterministic gate: unrecognized CI status "
                    f"{status!r} before Codex"
                ),
            )
    return None


class CodexAuditProvider:
    """AuditPort implementation backed by Codex CLI + ChatGPT plan auth.

    Default behavior:
    1. Validate worktree identity.
    2. Gather deterministic evidence (git/work packet/tests/CI).
    3. Revalidate exact repo/branch/HEAD + clean porcelain (dirty/drift ⇒
       HUMAN_REQUIRED; never autonomous PASS/REWORK from a dirty tree).
    4. Apply deterministic test/CI gates only from a clean snapshot
       (skip Codex on known FAIL/ERROR).
    5. Invoke Codex with tools/apps/browser/shell disabled to judge the bundle.
    6. Before accepting PASS or REWORK, revalidate identity + clean porcelain.
    """

    def __init__(
        self,
        *,
        runner: CodexRunner | None = None,
        git_runner: GitRunner | None = None,
        command_runner: CommandRunner | None = None,
        model: str | None = None,
        base_ref: str | None = None,
        evidence: str = "",
        evidence_bundle: dict | None = None,
        require_identity: bool = True,
        include_tests: bool = True,
        include_ci: bool = True,
        include_work_packet: bool = True,
    ) -> None:
        self._runner = runner or default_codex_runner
        self._git_runner = git_runner
        self._command_runner = command_runner
        self.model = model
        self.base_ref = base_ref
        self.evidence = evidence  # legacy override string
        self.evidence_bundle_override = evidence_bundle
        self.require_identity = require_identity
        self.include_tests = include_tests
        self.include_ci = include_ci
        self.include_work_packet = include_work_packet
        self.last_command: list[str] | None = None
        self.last_prompt: str | None = None
        self.last_identity: WorktreeIdentity | None = None
        self.last_evidence_bundle: dict | None = None

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        if self.require_identity:
            identity = validate_worktree_identity(
                record.worktree_path,
                repository=record.repository,
                branch=event.branch,
                expected_head=event.head,
                git_runner=self._git_runner,
            )
        else:
            identity = WorktreeIdentity(
                worktree_path=str(Path(record.worktree_path).resolve()),
                repository=normalize_github_repository(record.repository),
                branch=event.branch,
                head=event.head.lower(),
                toplevel=str(Path(record.worktree_path).resolve()),
            )
        self.last_identity = identity

        if self.evidence_bundle_override is not None:
            bundle = dict(self.evidence_bundle_override)
        else:
            bundle = collect_audit_evidence_bundle(
                event,
                record,
                identity=identity,
                base_ref=self.base_ref,
                git_runner=self._git_runner,
                command_runner=self._command_runner,
                include_tests=self.include_tests,
                include_ci=self.include_ci,
                include_work_packet=self.include_work_packet,
            )
            if self.evidence:
                bundle["legacy_evidence_text"] = self.evidence
        self.last_evidence_bundle = bundle

        for section_key, label in (
            ("pr_reviews", "PR review"),
            ("work_packet", "Work Packet"),
        ):
            section = bundle.get(section_key)
            if isinstance(section, dict) and section.get("status") in {
                "ERROR",
                "INCOMPLETE",
            }:
                status = str(section.get("status"))
                detail = str(
                    section.get("detail") or f"{label} evidence unavailable"
                )
                return AuditResult(
                    verdict="HUMAN_REQUIRED",
                    findings=(
                        f"codex audit skipped: {label} evidence "
                        f"status={status} ({detail[:300]})"
                    ),
                )

        git = bundle.get("git")
        if isinstance(git, dict) and git.get("evidence_status") in {
            "ERROR",
            "INCOMPLETE",
        }:
            status = str(git.get("evidence_status"))
            detail = str(git.get("detail") or "git evidence unavailable")
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=(
                    f"codex audit skipped: git evidence status={status} "
                    f"({detail[:300]})"
                ),
            )

        # Clean autonomous snapshot before any deterministic REWORK mapping.
        if self.require_identity:
            drift = _revalidate_clean_audited_snapshot(
                event,
                record,
                git_runner=self._git_runner,
            )
            if drift:
                return AuditResult(
                    verdict="HUMAN_REQUIRED",
                    findings=(
                        "codex audit skipped: audited worktree not a clean "
                        f"autonomous snapshot after evidence collection "
                        f"({drift[:300]})"
                    ),
                )
            identity = validate_worktree_identity(
                record.worktree_path,
                repository=record.repository,
                branch=event.branch,
                expected_head=event.head,
                git_runner=self._git_runner,
            )
            self.last_identity = identity

        gated = _deterministic_gate_before_codex(bundle)
        if gated is not None:
            return gated

        prompt = build_codex_audit_prompt(
            event,
            record,
            identity=identity,
            evidence_bundle=bundle,
            base_ref=self.base_ref,
        )
        self.last_prompt = prompt
        if _looks_like_secret(prompt):
            raise ValidationError("refusing to send credential-like material to Codex")

        with tempfile.TemporaryDirectory(prefix="awc-codex-") as tmp:
            last_message_path = str(Path(tmp) / "last-message.txt")
            command = build_codex_audit_command(
                identity.worktree_path,
                last_message_path=last_message_path,
                model=self.model,
            )
            self.last_command = list(command)
            try:
                raw = self._runner(command, prompt, identity.worktree_path)
            except ValidationError as exc:
                return AuditResult(
                    verdict="HUMAN_REQUIRED",
                    findings=f"codex audit failed: {exc}",
                )
        try:
            result = parse_audit_verdict_payload(raw)
        except ValidationError as exc:
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=f"codex audit output parse failed: {exc}",
            )
        if result.verdict not in AUDIT_VERDICTS:
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=f"codex returned invalid verdict: {result.verdict!r}",
            )

        # Fail closed before returning PASS or REWORK: never dispatch/accept
        # from a dirty or drifted worktree after Codex returns.
        if self.require_identity and result.verdict in {"PASS", "REWORK"}:
            drift = _revalidate_clean_audited_snapshot(
                event,
                record,
                git_runner=self._git_runner,
            )
            if drift:
                label = result.verdict
                return AuditResult(
                    verdict="HUMAN_REQUIRED",
                    findings=(
                        f"codex {label} rejected: audited worktree not a clean "
                        f"autonomous snapshot at {label} gate ({drift[:300]})"
                    ),
                )
        return result
