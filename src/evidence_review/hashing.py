"""SHA-256 hashing of evidence files, cached so it is paid for at most once per file.

Pure functions only -- no Qt -- so this stays trivially testable. The Qt wrapper
that runs it off the UI thread lives in :mod:`evidence_review.workers`.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .store import LocalStore

log = logging.getLogger(__name__)

#: 1 MiB reads keep throughput high without holding much memory.
CHUNK_SIZE = 1024 * 1024


def compute_sha256(
    path: str | Path,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> str | None:
    """Hash a file, returning ``None`` if cancelled or unreadable.

    ``progress`` receives ``(bytes_done, bytes_total)``.
    """
    path = Path(path)
    try:
        total = path.stat().st_size
    except OSError:
        log.warning("Cannot stat %s for hashing", path)
        return None

    digest = hashlib.sha256()
    done = 0
    try:
        with path.open("rb") as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except OSError:
        log.warning("Hashing failed for %s", path, exc_info=True)
        return None
    return digest.hexdigest()


def file_identity(path: str | Path) -> tuple[str, int, int] | None:
    """``(path, size, mtime_ns)`` -- the cache key for a file's hash."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_size, stat.st_mtime_ns


def cached_sha256(
    store: LocalStore,
    path: str | Path,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> str | None:
    """Return the cached hash for a file, computing and caching it if needed."""
    identity = file_identity(path)
    if identity is None:
        return None
    key_path, size, mtime_ns = identity

    existing = store.get_cached_hash(key_path, size, mtime_ns)
    if existing:
        return existing

    digest = compute_sha256(path, cancel=cancel, progress=progress)
    if digest:
        store.put_cached_hash(key_path, size, mtime_ns, digest)
    return digest


def lookup_cached_sha256(store: LocalStore, path: str | Path) -> str | None:
    """Cache-only lookup. Never touches the file beyond ``stat``."""
    identity = file_identity(path)
    if identity is None:
        return None
    return store.get_cached_hash(*identity)
