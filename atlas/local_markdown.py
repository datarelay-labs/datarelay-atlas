"""Atlas-owned personal Markdown snapshot import and fetch.

Reads happen only from an explicit allowlisted import root. Durable bytes live
under ``<data-root>/personal-snapshots`` and are what sync projects.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Callable

from atlas.data_lock import atomic_write_text
from atlas.github_sync import FetchedSource
from atlas.provenance import (
    LOCAL_MARKDOWN_PROVIDER,
    CanonicalSource,
    ValidationError,
    validate_project_id,
    validate_source,
    validate_source_path,
)
from atlas.registry import SOURCE_ID_RE
from atlas.secrets import contains_unsafe_secret

SNAPSHOT_DIRNAME = "personal-snapshots"
IMPORT_DIRNAME = "personal-import"


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_allowlisted_markdown(
    import_root: Path,
    relative: str,
    *,
    max_bytes: int | None = None,
    byte_observer: Callable[[int], None] | None = None,
) -> str:
    """Return bounded UTF-8 Markdown from a regular file inside ``import_root``."""
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1
    ):
        raise ValidationError("max_bytes must be a positive integer")
    path = _contained_file(import_root, relative, root_kind="import root")
    data = _read_regular(
        path,
        max_bytes=max_bytes,
        byte_observer=byte_observer,
    )
    return _decode_markdown(data)


def publish_snapshot(
    snapshot_root: Path,
    project_id: str,
    source_id: str,
    text: str,
) -> Path:
    """Store snapshot bytes at the Atlas-owned source_id path."""
    root = _ensure_real_directory(snapshot_root, "snapshot root")
    path = _snapshot_file(root, project_id, source_id, create=True)
    if path.is_symlink():
        raise ValidationError("symlink escape")
    atomic_write_text(path, text)
    return path


def fetch_local_markdown(snapshot_root: Path, source: CanonicalSource) -> FetchedSource:
    """Read an Atlas-owned snapshot. Revision is the content SHA-256.

    This path is strictly non-mutating: a missing root or file fails closed
    and does not create directories.
    """
    validate_source(source)
    if source.provider != LOCAL_MARKDOWN_PROVIDER:
        raise ValidationError(f"unsupported provider: {source.provider}")
    root = Path(snapshot_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("snapshot is missing")
    path = _snapshot_file(root, source.project_id, source.source_id, create=False)
    if path.is_symlink() or not path.is_file():
        raise ValidationError("snapshot is missing")
    data = _read_regular(path)
    text = _decode_markdown(data)
    return FetchedSource(content=text, source_revision=content_sha256(data))


def _snapshot_file(root: Path, project_id: str, source_id: str, *, create: bool) -> Path:
    validate_project_id(project_id)
    if not SOURCE_ID_RE.match(source_id):
        raise ValidationError(f"invalid source_id: {source_id}")
    project_dir = root / project_id
    if project_dir.is_symlink():
        raise ValidationError("symlink escape")
    if create:
        project_dir.mkdir(parents=True, exist_ok=True)
    elif not project_dir.is_dir():
        raise ValidationError("snapshot is missing")
    if project_dir.is_symlink() or not project_dir.is_dir():
        raise ValidationError("symlink escape")
    return project_dir / f"{source_id}.md"


def _contained_file(root: Path, relative: str, *, root_kind: str) -> Path:
    base = Path(root)
    if not base.is_absolute():
        base = Path.cwd() / base
    if base.is_symlink() or not base.is_dir():
        raise ValidationError(f"{root_kind} must be a real directory")
    validate_source_path(relative)
    if Path(relative).suffix != ".md":
        raise ValidationError("unsupported media")
    current = base
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise ValidationError("symlink escape")
    if not current.is_file():
        raise ValidationError("snapshot is not a regular file")
    try:
        if not current.resolve().is_relative_to(base.resolve()):
            raise ValidationError("path escapes import root")
    except OSError as exc:
        raise ValidationError("path escapes import root") from exc
    return current


def _ensure_real_directory(root: Path, kind: str) -> Path:
    path = Path(root)
    if path.is_symlink():
        raise ValidationError(f"{kind} must be a real directory")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValidationError(f"{kind} must be a real directory")
    return path


def _read_regular(
    path: Path,
    *,
    max_bytes: int | None = None,
    byte_observer: Callable[[int], None] | None = None,
) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("snapshot is not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ValidationError("snapshot is not a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("snapshot is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            read_size = 1024 * 1024
            if max_bytes is not None:
                remaining = max_bytes - total
                if remaining < 0:
                    raise ValidationError("snapshot exceeds bounded size")
                read_size = min(read_size, remaining + 1)
            block = os.read(fd, read_size)
            if not block:
                break
            total += len(block)
            if byte_observer is not None:
                byte_observer(len(block))
            if max_bytes is not None and total > max_bytes:
                raise ValidationError("snapshot exceeds bounded size")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _decode_markdown(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("unsupported media") from exc
    if contains_unsafe_secret(text):
        raise ValidationError("snapshot content looks secret")
    return text
