"""Codex CLI read-only audit provider for Autonomous Work Controller (ADR-0006).

Primary PoC auditor: Codex CLI authenticated with the owner's ChatGPT plan.
No OpenAI API key is required on this path.
"""

from __future__ import annotations

import json
import os
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
    normalize_github_repository,
    parse_audit_verdict_payload,
    validate_worktree_identity,
)

GitRunner = Callable[[list[str], str], str]
CodexRunner = Callable[[list[str], str, str], str]

DEFAULT_CODEX_TIMEOUT_SEC = 180

CODEX_AUDIT_INSTRUCTIONS = """You are an independent read-only engineering auditor for DataRelay Atlas.
Do not edit files, commit, push, or run mutating commands.
Prefer the embedded LOCAL git evidence in this prompt; avoid shell tools when that evidence is sufficient.
Return ONLY a JSON object with keys:
  verdict: one of PASS, REWORK, HUMAN_REQUIRED
  findings: concise evidence-backed string (include paths/lines when useful)
PASS only when evidence satisfies the workstream goal and acceptance constraints.
REWORK when actionable defects remain that a fresh /work-resume cycle can fix.
HUMAN_REQUIRED when owner judgment, credentials, or out-of-scope decisions are needed.
"""


def collect_local_audit_evidence(
    worktree_path: str,
    *,
    base_ref: str | None = None,
    git_runner: GitRunner | None = None,
    max_diff_chars: int = 12000,
) -> str:
    """Collect bounded local git evidence outside the Codex sandbox."""
    from atlas.work_controller import default_git_runner

    runner = git_runner or default_git_runner
    cwd = str(Path(worktree_path).resolve())
    chunks: list[str] = []
    for label, argv in (
        ("status", ["git", "status", "--short", "--branch"]),
        ("head", ["git", "rev-parse", "HEAD"]),
        ("branch", ["git", "branch", "--show-current"]),
        ("origin", ["git", "remote", "get-url", "origin"]),
    ):
        try:
            chunks.append(f"## {label}\n{runner(argv, cwd)}\n")
        except ValidationError as exc:
            chunks.append(f"## {label}\nERROR: {exc}\n")
    diff_ref = base_ref or "origin/main"
    try:
        stat = runner(["git", "diff", "--stat", f"{diff_ref}...HEAD"], cwd)
        chunks.append(f"## diff_stat against {diff_ref}\n{stat}\n")
        diff = runner(["git", "diff", "--find-renames", f"{diff_ref}...HEAD"], cwd)
        if len(diff) > max_diff_chars:
            diff = diff[:max_diff_chars] + "\n...[truncated]...\n"
        chunks.append(f"## diff against {diff_ref}\n{diff}\n")
    except ValidationError as exc:
        chunks.append(f"## diff against {diff_ref}\nERROR: {exc}\n")
    return "\n".join(chunks).strip() + "\n"


def build_codex_audit_prompt(
    event: CompletionEvent,
    record: WorkstreamRecord,
    *,
    identity: WorktreeIdentity,
    base_ref: str | None = None,
    evidence: str = "",
) -> str:
    """Bounded read-only audit prompt; never includes credentials."""
    payload = {
        "role": "awc_codex_audit",
        "workstream": record.workstream,
        "issue_number": record.issue_number,
        "repository": identity.repository,
        "worktree_path": identity.worktree_path,
        "branch": identity.branch,
        "head": identity.head,
        "base_ref": base_ref or "",
        "event_id": event.event_id,
        "attempt": event.attempt,
        "max_attempts": record.max_attempts,
        "controller_state": record.state,
        "resume_command": "/work-resume",
        "evidence": evidence,
        "rules": [
            "read_only",
            "no_edits",
            "no_commits",
            "no_pushes",
            "local_worktree_primary",
            "return_json_verdict_only",
        ],
    }
    return (
        CODEX_AUDIT_INSTRUCTIONS
        + "\n\nAUDIT_CONTEXT_JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n"
    )


def build_codex_audit_command(
    worktree_path: str,
    *,
    last_message_path: str,
    model: str | None = None,
) -> list[str]:
    """Fixed non-interactive Codex argv for controller use.

    Uses ChatGPT-plan auth from local Codex login state. Read-only sandbox.
    Prompt is supplied on stdin (`-`).
    """
    cmd = [
        "codex",
        "exec",
        "-C",
        str(Path(worktree_path).resolve()),
        "-s",
        "read-only",
        "--ephemeral",
        "--color",
        "never",
        "-o",
        last_message_path,
        "-",
    ]
    if model:
        cmd[2:2] = ["-m", model]
    return cmd


def default_codex_runner(command: list[str], prompt: str, cwd: str) -> str:
    """Run Codex exec and return the last-message file contents."""
    if not command or command[0] != "codex":
        raise ValidationError(f"refusing non-codex command: {command!r}")
    if "-s" not in command or "read-only" not in command:
        raise ValidationError("codex audit runner requires read-only sandbox")
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
    return "OPENAI_API_KEY" in text or ("sk-" in text and "sk-example" not in text)


class CodexAuditProvider:
    """AuditPort implementation backed by Codex CLI + ChatGPT plan auth."""

    def __init__(
        self,
        *,
        runner: CodexRunner | None = None,
        git_runner: GitRunner | None = None,
        model: str | None = None,
        base_ref: str | None = None,
        evidence: str = "",
        require_identity: bool = True,
    ) -> None:
        self._runner = runner or default_codex_runner
        self._git_runner = git_runner
        self.model = model
        self.base_ref = base_ref
        self.evidence = evidence
        self.require_identity = require_identity
        self.last_command: list[str] | None = None
        self.last_prompt: str | None = None
        self.last_identity: WorktreeIdentity | None = None

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
        local_evidence = self.evidence
        if not local_evidence:
            local_evidence = collect_local_audit_evidence(
                identity.worktree_path,
                base_ref=self.base_ref,
                git_runner=self._git_runner,
            )
        prompt = build_codex_audit_prompt(
            event,
            record,
            identity=identity,
            base_ref=self.base_ref,
            evidence=local_evidence,
        )
        # Strengthen no-shell preference once local evidence is attached.
        prompt += (
            "\nLOCAL_EVIDENCE_IS_AUTHORITATIVE: Use the embedded git evidence above. "
            "Do not run shell commands unless evidence is missing a required field. "
            "Never edit files.\n"
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
