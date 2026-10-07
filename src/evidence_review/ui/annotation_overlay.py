"""Keeping the marks on the picture while it plays.

This decides what should be on screen; :class:`MpvBackend` knows how to get it there.

The work is kept off the playback path deliberately. Position updates arrive many times
a second, and re-rendering a 1080p overlay and writing it to disk at that rate would
make the player stutter for no benefit: what is on screen only changes when a mark comes
or goes, or when the window is resized. So a signature is computed for "what should be
showing", and nothing happens while it stays the same.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject

from ..annotations import Annotation, render, visible_at
from ..media.mpv_backend import MpvBackend

log = logging.getLogger(__name__)


class AnnotationOverlay(QObject):
    """Draws an entry's marks over the playing video, when switched on."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend: MpvBackend | None = None
        self._annotations: list[Annotation] = []
        self._enabled = True
        self._signature: tuple | None = None
        self._directory = Path(tempfile.mkdtemp(prefix="evrev_overlay_"))
        self._frame_index = 0

    # ------------------------------------------------------------------ #

    def attach(self, backend: MpvBackend | None) -> None:
        """Point at the player, or at nothing when the media is closed."""
        if self._backend is backend:
            return
        self._clear()
        self._backend = backend
        self._signature = None

    def set_annotations(self, annotations: list[Annotation]) -> None:
        self._annotations = [a for a in annotations if not a.is_deleted]
        self._signature = None  # force the next refresh to actually draw
        self.refresh()

    def annotations(self) -> list[Annotation]:
        return list(self._annotations)

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled == enabled:
            return
        self._enabled = enabled
        if enabled:
            self._signature = None
            self.refresh()
        else:
            self._clear()

    def is_enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #

    def refresh(self, position: float | None = None) -> None:
        """Put the right marks on screen for ``position``.

        Safe to call as often as the player reports a new position: it returns without
        doing anything unless what should be showing has actually changed.
        """
        backend = self._backend
        if backend is None or not self._enabled or not self._annotations:
            self._clear()
            return

        geometry = backend.osd_geometry()
        if geometry is None:
            return  # no picture yet; nothing to draw on
        left, top, width, height = geometry

        if position is None:
            position = backend.position()
        showing = visible_at(self._annotations, position)

        signature = (
            tuple(sorted(a.annotation_id for a in showing)),
            tuple(sorted(a.updated_at_utc.isoformat() for a in showing)),
            left, top, width, height,
        )
        if signature == self._signature:
            return
        self._signature = signature

        if not showing:
            self._clear(keep_signature=True)
            return

        image = render(showing, width, height)
        # A fresh filename each time: mpv keeps the previous file mapped, and writing
        # over it has shown itself to produce a torn frame.
        self._frame_index += 1
        path = self._directory / f"overlay_{self._frame_index % 4}.bgra"
        try:
            path.write_bytes(bytes(image.constBits())[: image.sizeInBytes()])
        except OSError as exc:
            log.warning("Could not write the annotation overlay: %s", exc)
            return
        backend.show_overlay(path, left, top, width, height)

    def _clear(self, *, keep_signature: bool = False) -> None:
        if self._backend is not None:
            self._backend.clear_overlay()
        if not keep_signature:
            self._signature = None

    def shutdown(self) -> None:
        """Take the overlay down and clean up the scratch files."""
        self._clear()
        self._backend = None
        import shutil

        shutil.rmtree(self._directory, ignore_errors=True)
