"""Codex CLI read-only audit provider for Autonomous Work Controller (ADR-0006).

Primary PoC auditor: Codex CLI authenticated with the owner's ChatGPT plan.
No OpenAI API key is required on this path.

Default mode gathers a deterministic evidence bundle in-process (git, Work
Packet, tests, CI) and asks Codex to judge only that bundle with
tools/apps/browser/shell disabled. Codex local shell is not required.
"""

from __future__ import annotations

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
    default_git_runner,
    normalize_github_repository,
    parse_audit_verdict_payload,
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


def collect_git_evidence(
    worktree_path: str,
    *,
    base_ref: str | None = None,
    git_runner: GitRunner | None = None,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> dict:
    """Collect bounded local git evidence outside Codex."""
    runner = git_runner or default_git_runner
    cwd = str(Path(worktree_path).resolve())
    evidence: dict = {"collector": "atlas.codex_audit.collect_git_evidence"}
    for label, argv in (
        ("status", ["git", "status", "--short", "--branch"]),
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
        evidence["diff"] = _trim(diff, max_diff_chars)
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
                value = _trim(value, max_diff_chars)
            evidence[label] = value
        except ValidationError as exc:
            evidence[label] = f"ERROR: {exc}"
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
    return {
        "collector": "atlas.codex_audit.collect_work_packet_evidence",
        "status": "OK",
        "number": payload.get("number"),
        "title": payload.get("title"),
        "state": payload.get("state"),
        "updated_at": payload.get("updatedAt"),
        "body": _trim(body, max_chars),
    }


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
    # gh pr checks: 0=pass, 8=pending, other=fail (see `gh pr checks --help`).
    if checks.returncode == 0:
        status = "OK"
    elif checks.returncode == 8:
        status = "PENDING"
    else:
        status = "FAIL"
    return {
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


def _bounded_review_items(raw: object, *, max_chars: int) -> list[dict]:
    """Normalize review/comment payloads into a bounded list of dicts."""
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    remaining = max_chars
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("body") or "")
        item = {
            "id": entry.get("id"),
            "user": ((entry.get("user") or {}) if isinstance(entry.get("user"), dict) else {}).get(
                "login"
            ),
            "state": entry.get("state"),
            "commit_id": entry.get("commit_id"),
            "submitted_at": entry.get("submitted_at"),
            "created_at": entry.get("created_at"),
            "path": entry.get("path"),
            "line": entry.get("line"),
            "body": _trim(body, min(1200, max(200, remaining))),
        }
        encoded = json.dumps(item, sort_keys=True)
        if len(encoded) > remaining and items:
            break
        items.append(item)
        remaining = max(0, remaining - len(encoded))
        if remaining <= 0:
            break
    return items


def collect_pr_review_evidence(
    *,
    repository: str,
    pr_number: int,
    command_runner: CommandRunner | None = None,
    max_chars: int = DEFAULT_MAX_REVIEW_CHARS,
) -> dict:
    """Collect machine-observable PR reviews + inline comments (fail closed)."""
    cwd = str(Path.cwd())
    base = f"repos/{repository}/pulls/{pr_number}"
    sections: dict[str, object] = {
        "collector": "atlas.codex_audit.collect_pr_review_evidence",
        "repository": repository,
        "pr_number": pr_number,
    }
    for label, path in (
        ("reviews", f"{base}/reviews"),
        ("inline_comments", f"{base}/comments"),
    ):
        argv = ["gh", "api", path]
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
            payload = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return {
                "collector": "atlas.codex_audit.collect_pr_review_evidence",
                "status": "ERROR",
                "detail": f"gh api {path} returned non-JSON",
                "repository": repository,
                "pr_number": pr_number,
                "failed_section": label,
            }
        sections[label] = _bounded_review_items(payload, max_chars=max_chars // 2)
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


def _looks_like_secret(text: str) -> bool:
    """Detect likely live credentials, not mere documentation mentions."""
    if re.search(r"OPENAI_API_KEY\s*=\s*\S+", text):
        return True
    if re.search(r"\bsk-[A-Za-z0-9]{20,}\b", text):
        return True
    if re.search(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", text):
        return True
    return False



class CodexAuditProvider:
    """AuditPort implementation backed by Codex CLI + ChatGPT plan auth.

    Default behavior:
    1. Validate worktree identity.
    2. Gather deterministic evidence (git/work packet/tests/CI).
    3. Invoke Codex with tools/apps/browser/shell disabled to judge the bundle.
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
        return result
