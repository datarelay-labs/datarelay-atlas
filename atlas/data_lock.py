"""Exclusive data-root lock and atomic file publish.

Writers and backup share this module so neither imports the other.
"""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = ".write.lock"

_holders: dict[str, "_Holder"] = {}
_holders_guard = threading.Lock()


@dataclass
class _Holder:
    rlock: threading.RLock
    depth: int = 0
    fd: int | None = None


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
    """Exclusive lock shared by registry writes, projection writes, and backup.

    Same-thread reentry uses one flock. The descriptor is close-on-exec so a
    child process does not inherit the held lock.
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
            fd = os.open(
                path / LOCK_NAME,
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
                0o600,
            )
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
            holder.fd = fd
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
            if holder.depth == 0 and holder.fd is not None:
                fd = holder.fd
                holder.fd = None
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
    finally:
        holder.rlock.release()


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
