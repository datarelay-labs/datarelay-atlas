"""GitHub-backed Audit Control Packet and [AI Work] finding handoff (ADR-0007).

Canonical durable checkpoint state uses the GitHub Contents API with
server-enforced blob-SHA compare-and-set. Issue bodies are not an atomic
mutation surface. Local file stores remain derived cache / offline tests only.
"""

from __future__ import annotations

import base64
import hashlib
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

DEFAULT_CHECKPOINT_BRANCH = "atlas/chat-audit-control"
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


def checkpoint_contents_path(issue_number: int) -> str:
    return f".atlas/chat-audit/checkpoints/issue-{int(issue_number)}.json"


def _is_contents_conflict(completed: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{completed.stderr or ''}\n{completed.stdout or ''}".lower()
    if "http 409" in detail or '"status":"409"' in detail.replace(" ", ""):
        return True
    if "http 422" in detail and (
        "sha" in detail or "conflict" in detail or "already exists" in detail
    ):
        return True
    if "is at " in detail and "but expected" in detail:
        return True
    return False


class GitHubContentsCheckpointStore:
    """Canonical checkpoint store via Contents API blob-SHA CAS.

    GitHub Issue body read-modify-write is not atomic across hosts. Mutations
    bind to the blob SHA observed at load and use PUT
    /repos/.../contents/... so the server rejects a stale expected SHA.
    """

    def __init__(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str | None = None,
        path: str | None = None,
        cache: FileCheckpointStore | None = None,
        command_runner: CommandRunner | None = None,
        cwd: str | None = None,
    ):
        self.repository = normalize_github_repository(repository)
        if int(issue_number) < 1:
            raise ValidationError(f"invalid checkpoint issue_number: {issue_number}")
        self.issue_number = int(issue_number)
        env_branch = os.environ.get("ATLAS_CHAT_AUDIT_CHECKPOINT_BRANCH", "").strip()
        self.branch = (
            (branch or env_branch or DEFAULT_CHECKPOINT_BRANCH).strip()
            or DEFAULT_CHECKPOINT_BRANCH
        )
        self.path = path or checkpoint_contents_path(self.issue_number)
        self.cache = cache
        self._runner = command_runner or _default_runner
        self._cwd = cwd or str(Path.cwd())
        # Blob SHA observed by the latest successful load/save (None = absent).
        self._cas_blob_sha: str | None = None
        self._cas_loaded = False

    @contextmanager
    def lock(self) -> Iterator[None]:
        if self.cache is not None:
            with self.cache.lock():
                yield
        else:
            yield

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(argv, self._cwd)

    def _api_endpoint(self) -> str:
        return f"repos/{self.repository}/contents/{self.path}"

    def _get_contents(self) -> dict[str, Any] | None:
        completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"{self._api_endpoint()}?ref={self.branch}",
            ]
        )
        detail = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if completed.returncode != 0:
            if "404" in detail or "Not Found" in detail:
                return None
            raise ValidationError(
                (completed.stderr or completed.stdout or "").strip()[:500]
                or "gh api contents GET failed for audit checkpoint"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("gh api contents GET returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("gh api contents GET returned non-object JSON")
        payload_type = payload.get("type")
        if payload_type is not None and payload_type != "file":
            raise ValidationError(
                f"checkpoint path must be a file, got {payload_type!r}"
            )
        return payload

    def _put_contents(
        self,
        *,
        packet: AuditControlPacket,
        expected_sha: str | None,
    ) -> str:
        raw = json.dumps(packet.to_dict(), indent=2, sort_keys=True) + "\n"
        body: dict[str, Any] = {
            "message": (
                f"atlas chat-audit checkpoint issue-{self.issue_number} "
                f"rev={packet.canonical_revision}"
            ),
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if expected_sha:
            body["sha"] = expected_sha
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    "-H",
                    "Accept: application/vnd.github+json",
                    self._api_endpoint(),
                    "--input",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            if _is_contents_conflict(completed):
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: contents blob SHA "
                    "conflict (stale or concurrent writer)"
                )
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500] or "gh api contents PUT failed for audit checkpoint"
            )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError("gh api contents PUT returned non-JSON") from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        new_sha = None
        if isinstance(content, dict):
            new_sha = content.get("sha")
        if not new_sha and isinstance(payload, dict):
            new_sha = payload.get("sha")
        if not isinstance(new_sha, str) or not new_sha.strip():
            # Deterministic fallback when API omits sha in mocked/minimal replies.
            new_sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return new_sha.strip()

    def load(self) -> AuditControlPacket | None:
        payload = self._get_contents()
        if payload is None:
            self._cas_blob_sha = None
            self._cas_loaded = True
            return None
        encoded = str(payload.get("content") or "")
        if not encoded:
            raise ValidationError("checkpoint contents payload missing content")
        try:
            decoded = base64.b64decode(encoded, validate=False).decode("utf-8")
            raw = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("checkpoint contents JSON is invalid") from exc
        if not isinstance(raw, dict):
            raise ValidationError("checkpoint contents JSON must be an object")
        packet = AuditControlPacket.from_dict(raw)
        blob_sha = str(payload.get("sha") or "").strip()
        if not blob_sha:
            raise ValidationError("checkpoint contents response missing blob sha")
        self._cas_blob_sha = blob_sha
        self._cas_loaded = True
        if self.cache is not None:
            self.cache.save(packet)
        return packet

    def save(self, packet: AuditControlPacket) -> None:
        """Persist via Contents API PUT bound to the loaded blob SHA.

        Does not perform a client-side re-read/check before write: concurrency
        safety comes from GitHub rejecting a stale expected ``sha``.
        """
        if not self._cas_loaded:
            # Require an explicit load (or prior successful save) so writers bind
            # to a server-observed CAS token rather than inventing one.
            raise ValidationError(
                "checkpoint save requires a prior load to bind Contents CAS sha"
            )
        safe = sanitize_packet_for_persistence(packet)
        expected_sha = self._cas_blob_sha
        if expected_sha is None:
            if int(safe.canonical_revision) != 0:
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: create requires "
                    "canonical_revision=0 when contents are absent"
                )
            next_revision = 1
        else:
            next_revision = int(safe.canonical_revision) + 1
        safe.canonical_revision = next_revision
        packet.canonical_revision = next_revision
        new_sha = self._put_contents(packet=safe, expected_sha=expected_sha)
        self._cas_blob_sha = new_sha
        self._cas_loaded = True
        if self.cache is not None:
            self.cache.save(safe)


# Backward-compatible alias for older imports/docs.
GitHubIssueCheckpointStore = GitHubContentsCheckpointStore


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
    checkpoint_branch: str | None = None,
) -> CheckpointStore:
    """Prefer GitHub Contents CAS store; local file is cache when configured."""
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
        return GitHubContentsCheckpointStore(
            repository=repository,
            issue_number=issue,
            branch=checkpoint_branch,
            cache=cache,
        )
    if require_github:
        raise ValidationError(
            "GitHub-backed Audit Control Packet required: pass "
            "--checkpoint-issue or set ATLAS_CHAT_AUDIT_ISSUE "
            "(local chat-audit.json is cache/test-only; canonical state uses "
            "Contents API blob-SHA CAS on branch atlas/chat-audit-control)"
        )
    return cache
