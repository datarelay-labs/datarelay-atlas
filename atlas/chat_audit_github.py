"""GitHub-backed Audit Control Packet and [AI Work] finding handoff (ADR-0007).

Canonical durable state lives on GitHub Issues. Local file stores remain
derived cache / offline test surfaces only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator
from contextlib import contextmanager

from atlas.chat_audit import (
    AuditControlPacket,
    AuditFinding,
    CheckpointCasConflict,
    CheckpointStore,
    FileCheckpointStore,
    sanitize_finding,
    sanitize_packet_for_persistence,
)
from atlas.provenance import ValidationError
from atlas.secrets import sanitize_durable_text
from atlas.work_controller import normalize_github_repository

CHECKPOINT_MARKER_START = "<!-- atlas-chat-audit-checkpoint:start -->"
CHECKPOINT_MARKER_END = "<!-- atlas-chat-audit-checkpoint:end -->"
HANDOFF_MARKER = "<!-- atlas-chat-audit-finding-id:"
CommandRunner = Callable[[list[str], str], subprocess.CompletedProcess[str]]


def _default_runner(
    argv: list[str], cwd: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"command timed out: {' '.join(argv[:4])}") from exc
    except FileNotFoundError as exc:
        raise ValidationError(f"command not found: {argv[0]}") from exc


def embed_checkpoint_in_issue_body(
    existing_body: str, packet: AuditControlPacket
) -> str:
    """Embed checkpoint JSON between markers; preserve surrounding issue text."""
    payload = json.dumps(packet.to_dict(), indent=2, sort_keys=True)
    block = (
        f"{CHECKPOINT_MARKER_START}\n"
        f"```json\n{payload}\n```\n"
        f"{CHECKPOINT_MARKER_END}\n"
    )
    body = existing_body or ""
    if CHECKPOINT_MARKER_START in body and CHECKPOINT_MARKER_END in body:
        pre = body.split(CHECKPOINT_MARKER_START, 1)[0]
        post = body.split(CHECKPOINT_MARKER_END, 1)[1]
        return pre.rstrip() + "\n\n" + block + post.lstrip("\n")
    title = "# Atlas Chat Audit Control Packet\n\n"
    if body.strip():
        return title + block + "\n" + body.lstrip()
    return title + block


def extract_checkpoint_from_issue_body(body: str) -> AuditControlPacket | None:
    if CHECKPOINT_MARKER_START not in (body or ""):
        return None
    try:
        section = body.split(CHECKPOINT_MARKER_START, 1)[1]
        section = section.split(CHECKPOINT_MARKER_END, 1)[0]
    except IndexError:
        raise ValidationError("malformed audit checkpoint markers in issue body")
    fence = section
    if "```" in fence:
        parts = fence.split("```")
        # expect ```json\n{...}\n```
        if len(parts) < 3:
            raise ValidationError("checkpoint JSON fence is incomplete")
        json_text = parts[1]
        if json_text.lstrip().startswith("json"):
            json_text = json_text.lstrip()[4:]
    else:
        json_text = fence
    try:
        raw = json.loads(json_text.strip())
    except json.JSONDecodeError as exc:
        raise ValidationError("checkpoint JSON is invalid") from exc
    if not isinstance(raw, dict):
        raise ValidationError("checkpoint JSON must be an object")
    return AuditControlPacket.from_dict(raw)


class GitHubIssueCheckpointStore:
    """Canonical checkpoint store backed by a GitHub Issue body section."""

    def __init__(
        self,
        *,
        repository: str,
        issue_number: int,
        cache: FileCheckpointStore | None = None,
        command_runner: CommandRunner | None = None,
        cwd: str | None = None,
    ):
        self.repository = normalize_github_repository(repository)
        if int(issue_number) < 1:
            raise ValidationError(f"invalid checkpoint issue_number: {issue_number}")
        self.issue_number = int(issue_number)
        self.cache = cache
        self._runner = command_runner or _default_runner
        self._cwd = cwd or str(Path.cwd())
        self._lock = (cache.lock if cache is not None else None)

    @contextmanager
    def lock(self) -> Iterator[None]:
        if self.cache is not None:
            with self.cache.lock():
                yield
        else:
            yield

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(argv, self._cwd)

    def _view_issue(self) -> dict[str, Any]:
        completed = self._run(
            [
                "gh",
                "issue",
                "view",
                str(self.issue_number),
                "--repo",
                self.repository,
                "--json",
                "number,title,body,state",
            ]
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500] or "gh issue view failed for audit checkpoint"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("gh issue view returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("gh issue view returned non-object JSON")
        return payload

    def _edit_body(self, body: str) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(body)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(self.issue_number),
                    "--repo",
                    self.repository,
                    "--body-file",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500] or "gh issue edit failed for audit checkpoint"
            )

    def load(self) -> AuditControlPacket | None:
        payload = self._view_issue()
        packet = extract_checkpoint_from_issue_body(str(payload.get("body") or ""))
        if packet is not None and self.cache is not None:
            self.cache.save(packet)
        return packet

    def save(self, packet: AuditControlPacket) -> None:
        """Persist checkpoint with compare-and-set on canonical_revision.

        Independent processes do not share the local cache lock. Every write
        must bind to the revision observed at load (or 0 for create). A stale
        writer that loses the race fails closed instead of overwriting newer
        canonical state.
        """
        safe = sanitize_packet_for_persistence(packet)
        base_revision = int(safe.canonical_revision)
        payload = self._view_issue()
        current_body = str(payload.get("body") or "")
        remote = extract_checkpoint_from_issue_body(current_body)
        if remote is None:
            if base_revision != 0:
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: expected revision "
                    f"{base_revision} but canonical issue has no checkpoint"
                )
            next_revision = 1
        else:
            remote_revision = int(remote.canonical_revision)
            if remote_revision != base_revision:
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: expected revision "
                    f"{base_revision} but canonical is {remote_revision}"
                )
            next_revision = remote_revision + 1
        safe.canonical_revision = next_revision
        packet.canonical_revision = next_revision
        new_body = embed_checkpoint_in_issue_body(current_body, safe)
        self._edit_body(new_body)
        if self.cache is not None:
            self.cache.save(safe)


class GitHubAIWorkHandoff:
    """Idempotent GitHub [AI Work] create/update handoff. Local JSON is not success."""

    def __init__(
        self,
        *,
        repository: str,
        command_runner: CommandRunner | None = None,
        cwd: str | None = None,
    ):
        self.repository = normalize_github_repository(repository)
        self._runner = command_runner or _default_runner
        self._cwd = cwd or str(Path.cwd())
        self.handoffs: list[dict[str, Any]] = []

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(argv, self._cwd)

    def upsert_implementation_packet(
        self, packet: AuditControlPacket, finding: AuditFinding
    ) -> dict[str, Any]:
        safe = sanitize_finding(finding)
        marker = f"{HANDOFF_MARKER}{safe.finding_id} -->"
        title = f"[AI Work] Audit finding: {safe.finding_id}"[:240]
        body = (
            f"{marker}\n"
            f"PACKET_VERSION=2\n"
            f"TARGET_REPO={packet.target_repository}\n"
            f"STATUS=ACTIVE\n"
            f"BRANCH={packet.target_branch}\n"
            f"TASK_KIND=DEVELOPMENT\n"
            f"OWNER_INTENT=Implement bounded audit finding {safe.finding_id}\n"
            f"LAST_VERIFIED_HEAD={packet.current_target_sha}\n"
            f"GATE=IMPLEMENTATION\n\n"
            f"## Goal\n\n{sanitize_durable_text(safe.summary)}\n\n"
            f"## Finding\n\n"
            f"- id: `{safe.finding_id}`\n"
            f"- unit: `{safe.unit}`\n"
            f"- severity: `{safe.severity}`\n"
            f"- head: `{packet.current_target_sha}`\n\n"
            f"## Next Action\n\n"
            f"1. Implement the bounded audit finding in Cursor.\n"
            f"2. Do not modify product code from Chat.\n"
            f"3. Re-audit exact HEAD after the fix lands.\n"
        )
        existing = self._find_existing(safe.finding_id)
        if existing is not None:
            number = int(existing["number"])
            self._edit_issue(number, title=title, body=body)
            record = {
                "action": "updated",
                "issue_number": number,
                "title": title,
                "repository": self.repository,
                "finding": safe.to_dict(),
                "url": existing.get("url"),
            }
        else:
            number, url = self._create_issue(title=title, body=body)
            record = {
                "action": "created",
                "issue_number": number,
                "title": title,
                "repository": self.repository,
                "finding": safe.to_dict(),
                "url": url,
            }
        self.handoffs.append(record)
        return record

    def _find_existing(self, finding_id: str) -> dict[str, Any] | None:
        # Prefer exact client-side title/marker match over GitHub search index
        # (search can lag immediately after create and cause duplicate issues).
        completed = self._run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repository,
                "--state",
                "open",
                "--json",
                "number,title,body,url",
                "--limit",
                "100",
            ]
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or "gh issue list failed; refusing local-only finding handoff"
            )
        try:
            items = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("gh issue list returned non-JSON") from exc
        if not isinstance(items, list):
            raise ValidationError("gh issue list returned non-list JSON")
        marker = f"{HANDOFF_MARKER}{finding_id} -->"
        expected_title_prefix = f"[AI Work] Audit finding: {finding_id}"
        matches = [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("title") or "").startswith(expected_title_prefix)
            and marker in str(item.get("body") or "")
        ]
        if len(matches) > 1:
            raise ValidationError(
                f"multiple open AI Work packets for finding_id={finding_id!r}"
            )
        return matches[0] if matches else None

    def _create_issue(self, *, title: str, body: str) -> tuple[int, str]:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(body)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    self.repository,
                    "--title",
                    title,
                    "--body-file",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or "gh issue create failed; refusing local-only finding handoff"
            )
        url = (completed.stdout or "").strip().splitlines()[-1].strip()
        match = re.search(r"/issues/(\d+)\s*$", url)
        if not match:
            raise ValidationError("gh issue create did not return an issue URL")
        return int(match.group(1)), url

    def _edit_issue(self, number: int, *, title: str, body: str) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(body)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    self.repository,
                    "--title",
                    title,
                    "--body-file",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or "gh issue edit failed; refusing local-only finding handoff"
            )


def resolve_checkpoint_store(
    *,
    data_root: Path,
    repository: str | None,
    checkpoint_issue: int | None,
    require_github: bool = True,
) -> CheckpointStore:
    """Prefer GitHub canonical store; local file is cache when GitHub configured."""
    issue = checkpoint_issue
    if issue is None:
        env = os.environ.get("ATLAS_CHAT_AUDIT_ISSUE", "").strip()
        if env:
            issue = int(env)
    cache = FileCheckpointStore(data_root)
    if issue is not None:
        if not repository:
            raise ValidationError(
                "repository is required for GitHub-backed audit checkpoint"
            )
        return GitHubIssueCheckpointStore(
            repository=repository,
            issue_number=issue,
            cache=cache,
        )
    if require_github:
        raise ValidationError(
            "GitHub-backed Audit Control Packet required: pass "
            "--checkpoint-issue or set ATLAS_CHAT_AUDIT_ISSUE "
            "(local chat-audit.json is cache/test-only)"
        )
    return cache
