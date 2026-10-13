"""Qt wrappers that keep slow work off the UI thread."""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .hashing import cached_sha256
from .store import LocalStore

log = logging.getLogger(__name__)


class HashWorker(QThread):
    """Computes a file's SHA-256 in the background and caches the result.

    Evidence files are routinely multi-gigabyte, so this must never run on the UI
    thread and must be cancellable the moment the user opens a different file.
    """

    hash_ready = Signal(str, str)  # media_path, sha256
    hash_failed = Signal(str, str)  # media_path, reason
    progress = Signal(str, int, int)  # media_path, done, total

    def __init__(self, db_path: str | Path, media_path: str | Path, parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._media_path = Path(media_path)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # QThread entry point
        # A worker thread needs its own store; LocalStore keeps connections
        # thread-local, so constructing one here is correct and cheap.
        store = LocalStore(self._db_path)
        try:
            digest = cached_sha256(
                store,
                self._media_path,
                cancel=self._cancel,
                progress=lambda done, total: self.progress.emit(str(self._media_path), done, total),
            )
        except Exception as exc:
            log.exception("Hashing failed for %s", self._media_path)
            self.hash_failed.emit(str(self._media_path), str(exc))
            return
        finally:
            store.close()

        if self._cancel.is_set():
            return
        if digest:
            self.hash_ready.emit(str(self._media_path), digest)
        else:
            self.hash_failed.emit(str(self._media_path), "unreadable")


class WaveformWorker(QThread):
    """Reads a recording's envelope in the background, and caches it.

    A three-hour EVP session takes around twenty seconds to read, which is far too long
    to hold the UI for and perfectly acceptable to do once. Like hashing, it has to be
    abandoned the instant the reviewer opens something else: they have moved on, and
    finishing would only spend their machine on a file nobody is looking at.
    """

    # The chain is carried in the signal rather than looked up when it arrives: a read
    # of a long recording outlives several changes to the cleanup, and a late envelope
    # labelled with whatever the panel happens to say by then is a picture of one thing
    # captioned as another.
    waveform_ready = Signal(str, str, object)  # media_path, audio_filters, Waveform
    waveform_failed = Signal(str, str)  # media_path, reason
    progress = Signal(str, float)  # media_path, 0..1

    def __init__(
        self,
        media_path: str | Path,
        cache_dir: str | Path,
        parent: QObject | None = None,
        audio_filters: str = "",
    ) -> None:
        super().__init__(parent)
        self._media_path = Path(media_path)
        self._cache_dir = Path(cache_dir)
        self._audio_filters = audio_filters
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # QThread entry point
        from .media.waveform import (
            WaveformCancelled,
            WaveformError,
            cache_path,
            extract_peaks,
            load_peaks,
            prune_cache,
            save_peaks,
        )

        path = str(self._media_path)
        cached_at = cache_path(self._cache_dir, self._media_path, self._audio_filters)

        if cached_at is not None:
            cached = load_peaks(cached_at)
            if cached is not None:
                # Touched so the pruner treats it as recently used, which is what makes
                # "the files I keep coming back to" the ones that survive.
                with contextlib.suppress(OSError):
                    cached_at.touch()
                if not self._cancel.is_set():
                    self.waveform_ready.emit(path, self._audio_filters, cached)
                return

        try:
            waveform = extract_peaks(
                self._media_path,
                progress=lambda fraction: self.progress.emit(path, fraction),
                should_stop=self._cancel.is_set,
                audio_filters=self._audio_filters,
            )
        except WaveformCancelled:
            return
        except WaveformError as exc:
            self.waveform_failed.emit(path, str(exc))
            return
        except Exception as exc:
            log.exception("Reading the waveform failed for %s", self._media_path)
            self.waveform_failed.emit(path, str(exc))
            return

        if self._cancel.is_set():
            return

        if cached_at is not None:
            save_peaks(waveform, cached_at)
            prune_cache(self._cache_dir)
        self.waveform_ready.emit(path, self._audio_filters, waveform)


class WaveformDetailWorker(QThread):
    """Re-reads one visible span at the resolution actually being drawn.

    Zoomed in past the stored envelope's 10 ms buckets, the view would otherwise show
    stairs. The span is short, so this is quick -- but it still cannot run on the UI
    thread, because it happens while somebody is scrolling.
    """

    # path, audio_filters, start, end, Waveform
    detail_ready = Signal(str, str, float, float, object)
    detail_failed = Signal(str, str)

    def __init__(
        self,
        media_path: str | Path,
        start: float,
        end: float,
        peaks_per_second: int,
        parent: QObject | None = None,
        audio_filters: str = "",
    ) -> None:
        super().__init__(parent)
        self._media_path = Path(media_path)
        self._start = max(0.0, start)
        self._end = end
        self._peaks_per_second = peaks_per_second
        self._audio_filters = audio_filters
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # QThread entry point
        from .media.waveform import WaveformCancelled, WaveformError, extract_peaks

        path = str(self._media_path)
        span = max(0.05, self._end - self._start)
        try:
            waveform = extract_peaks(
                self._media_path,
                peaks_per_second=self._peaks_per_second,
                start=self._start,
                duration=span,
                should_stop=self._cancel.is_set,
                audio_filters=self._audio_filters,
            )
        except (WaveformCancelled, WaveformError):
            return
        except Exception:
            log.exception("Reading waveform detail failed for %s", self._media_path)
            return

        if not self._cancel.is_set():
            self.detail_ready.emit(
                path, self._audio_filters, self._start, self._start + span, waveform
            )
