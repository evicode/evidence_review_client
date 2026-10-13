"""Still-image viewing with pan, zoom and rotate.

Implements the same :class:`MediaBackend` interface as the mpv backend so the
transport bar and the LOG workflow do not need to special-case images beyond
hiding the controls that make no sense.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImageReader, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QWidget

from .base import MediaBackend

log = logging.getLogger(__name__)

MIN_SCALE = 0.05
MAX_SCALE = 40.0


class ImageSurface(QGraphicsView):
    """Pan/zoom canvas for a single image."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item: QGraphicsPixmapItem | None = None
        self._fit_mode = True
        self._rotation = 0

        self.setBackgroundBrush(QColor("#101214"))
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- content ------------------------------------------------------------ #

    def show_image(self, path: str | Path) -> bool:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)  # honour the EXIF orientation tag
        image = reader.read()
        if image.isNull():
            log.warning("Could not decode image %s: %s", path, reader.errorString())
            return False

        self._scene.clear()
        self._item = self._scene.addPixmap(QPixmap.fromImage(image))
        self._scene.setSceneRect(QRectF(image.rect()))
        self._rotation = 0
        self.resetTransform()
        self.fit_to_window()
        return True

    def clear(self) -> None:
        self._scene.clear()
        self._item = None
        self.resetTransform()

    @property
    def has_image(self) -> bool:
        return self._item is not None

    def pixmap(self) -> QPixmap | None:
        return self._item.pixmap() if self._item else None

    # -- view control ------------------------------------------------------- #

    def fit_to_window(self) -> None:
        if self._item is None:
            return
        self._fit_mode = True
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_by(self, factor: float) -> None:
        if self._item is None:
            return
        target = self.current_scale() * factor
        if not (MIN_SCALE <= target <= MAX_SCALE):
            return
        self._fit_mode = False
        self.scale(factor, factor)

    def rotate_by(self, degrees: int) -> None:
        if self._item is None:
            return
        self._rotation = (self._rotation + degrees) % 360
        self.rotate(degrees)

    def current_scale(self) -> float:
        return float(self.transform().m11()) or 1.0

    # -- events ------------------------------------------------------------- #

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt naming
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier or self.has_image:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_by(1.15 if delta > 0 else 1 / 1.15)
                event.accept()
                return
        super().wheelEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit_to_window()


class ImageBackend(MediaBackend):
    """MediaBackend adapter over :class:`ImageSurface`."""

    def __init__(self, surface: ImageSurface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._surface = surface

    @property
    def supports_playback(self) -> bool:
        return False

    def load(self, path: str | Path) -> None:
        target = Path(path)
        if not self._surface.show_image(target):
            self.error.emit(f"Could not open image: {target.name}")
            return
        self._path = target
        self.duration_changed.emit(0.0)
        self.position_changed.emit(0.0)
        self.paused_changed.emit(True)
        self.loaded.emit(str(target))

    def stop(self) -> None:
        self._surface.clear()
        self._path = None

    def shutdown(self) -> None:
        self._surface.clear()

    def is_paused(self) -> bool:
        return True

    def capture_frame(self, target: str | Path) -> bool:
        """For a still, the 'frame' is the image itself, saved as displayed."""
        pixmap = self._surface.pixmap()
        if pixmap is None or pixmap.isNull():
            return False
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(pixmap.save(str(path), "PNG"))
