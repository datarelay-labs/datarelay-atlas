"""Exclusive data-root lock and atomic file publish.

Writers and backup share this module so neither imports the other.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from atlas.provenance import ValidationError

LOCK_NAME = ".write.lock"


def _effective_uid() -> int:
    return os.geteuid()


def require_data_root_writer_owner_fd(root_fd: int) -> None:
    """Reject writers that would create service-unreadable 0600 data files."""
    try:
        current = os.fstat(root_fd)
    except OSError as exc:
        raise ValidationError("data root writer ownership cannot be verified") from exc
    if not stat.S_ISDIR(current.st_mode):
        raise ValidationError("data root writer ownership cannot be verified")
    if current.st_uid != _effective_uid():
        raise ValidationError("data root writer does not own data root")


def data_root_fd_path(root_fd: int) -> Path:
    """Return a stable Linux path bound to an already-open data-root directory."""
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        if base.is_dir():
            return base / str(root_fd)
    raise ValidationError("data root fd boundary is unavailable")


_holders: dict[str, "_Holder"] = {}
_holders_guard = threading.Lock()


@dataclass
class _Holder:
    rlock: threading.RLock
    depth: int = 0
    fd: int | None = None
    root_fd: int | None = None


def projection_store_lock_root(store_root: Path) -> Path:
    """Return the data root whose lock covers this projection store.

    Product layout stores projections at ``<data-root>/projections``. A store
    constructed at any other directory locks that directory itself.
    """
    root = Path(store_root)
    if root.name == "projections":
        return root.parent
    return root


@contextmanager
def data_root_write_lock(data_root: Path):
    """Exclusive lock bound to one stable data-root directory inode.

    Same-thread reentry uses one flock. The root directory and lock file stay
    open for the whole critical section so a path swap while waiting cannot
    redirect a writer to an unlocked directory.
    """
    key = str(Path(data_root).resolve())
    with _holders_guard:
        holder = _holders.get(key)
        if holder is None:
            holder = _Holder(rlock=threading.RLock())
            _holders[key] = holder
    holder.rlock.acquire()
    try:
        if holder.depth == 0:
            path = Path(data_root)
            path.mkdir(parents=True, exist_ok=True)
            root_fd = _open_data_root_dir(path)
            fd = _open_lock_file_at(root_fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                _verify_data_root_identity(path, root_fd)
            except Exception:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
                os.close(root_fd)
                raise
            holder.fd = fd
            holder.root_fd = root_fd
        holder.depth += 1
        try:
            if holder.root_fd is None:
                raise ValidationError("data root fd boundary is unavailable")
            yield holder.root_fd
        finally:
            holder.depth -= 1
            if holder.depth == 0:
                fd = holder.fd
                root_fd = holder.root_fd
                holder.fd = None
                holder.root_fd = None
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                if root_fd is not None:
                    os.close(root_fd)
    finally:
        holder.rlock.release()


def _open_data_root_dir(path: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValidationError("data root fd boundary is unavailable")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValidationError("data root is not a regular directory") from exc
        raise
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValidationError("data root is not a regular directory")
    except Exception:
        os.close(fd)
        raise
    return fd


def _verify_data_root_identity(path: Path, root_fd: int) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
        bound = os.fstat(root_fd)
    except OSError as exc:
        raise ValidationError("data root identity changed while acquiring write lock") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino)
    ):
        raise ValidationError("data root identity changed while acquiring write lock")


def _open_lock_file_at(root_fd: int) -> int:
    """Open the shared lock file relative to the bound data-root directory."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValidationError("data root lock cannot be opened without following symlinks")
    try:
        fd = os.open(
            LOCK_NAME,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValidationError("data root lock is not a regular file") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("data root lock is not a regular file")
        os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        raise
    return fd


def atomic_write_text(path: Path, text: str) -> None:
    """Publish ``text`` by rename so readers never observe a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        payload = text.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)
