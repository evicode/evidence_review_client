"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSlider,
    QToolTip,
    QWidget,
)

from ..util import format_timecode, parse_timecode


class SeekSlider(QSlider):
    """A slider that jumps straight to a clicked position and previews the time
    under the cursor. The default Qt behaviour of paging by a step is wrong for
    scrubbing footage."""

    #: Emitted while the user drags, with the seconds under the handle.
    scrubbing = Signal(float)
    #: Emitted when the user releases, with the final seconds.
    seek_requested = Signal(float)

    #: Slider units per second. Milliseconds give sub-frame precision.
    RESOLUTION = 1000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, 0)
        self.setSingleStep(self.RESOLUTION)
        self.setPageStep(self.RESOLUTION * 10)
        self.setMouseTracking(True)
        self.setTracking(True)
        self._duration = 0.0
        self._dragging = False
        self.sliderPressed.connect(self._on_pressed)
        self.sliderReleased.connect(self._on_released)
        self.sliderMoved.connect(self._on_moved)

    # -- external state ----------------------------------------------------- #

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, float(seconds))
        self.setRange(0, int(self._duration * self.RESOLUTION))
        self.setEnabled(self._duration > 0)

    def set_position(self, seconds: float) -> None:
        """Move the handle, unless the user is currently dragging it."""
        if self._dragging:
            return
        self.blockSignals(True)
        self.setValue(int(max(0.0, seconds) * self.RESOLUTION))
        self.blockSignals(False)

    def seconds(self) -> float:
        return self.value() / self.RESOLUTION

    # -- interaction -------------------------------------------------------- #

    def _seconds_at(self, x: int) -> float:
        if self.width() <= 0 or self._duration <= 0:
            return 0.0
        ratio = min(max(x / self.width(), 0.0), 1.0)
        return ratio * self._duration

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self._duration > 0:
            self._dragging = True
            seconds = self._seconds_at(int(event.position().x()))
            self.setValue(int(seconds * self.RESOLUTION))
            self.scrubbing.emit(seconds)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        seconds = self._seconds_at(int(event.position().x()))
        if self._duration > 0:
            QToolTip.showText(event.globalPosition().toPoint(), format_timecode(seconds), self)
        if self._dragging:
            self.setValue(int(seconds * self.RESOLUTION))
            self.scrubbing.emit(seconds)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.seek_requested.emit(self.seconds())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        QToolTip.hideText()
        super().leaveEvent(event)

    def _on_pressed(self) -> None:
        self._dragging = True

    def _on_released(self) -> None:
        self._dragging = False
        self.seek_requested.emit(self.seconds())

    def _on_moved(self, value: int) -> None:
        self.scrubbing.emit(value / self.RESOLUTION)


class TimecodeEdit(QLineEdit):
    """Line edit that accepts ``HH:MM:SS.mmm``, ``MM:SS`` or bare seconds."""

    value_changed = Signal(object)  # float | None

    def __init__(self, parent: QWidget | None = None, *, allow_empty: bool = False) -> None:
        super().__init__(parent)
        self._allow_empty = allow_empty
        self.setPlaceholderText("HH:MM:SS.mmm")
        self.setMaximumWidth(140)
        self.editingFinished.connect(self._normalise)

    def set_seconds(self, seconds: float | None) -> None:
        self.setText("" if seconds is None else format_timecode(seconds))

    def seconds(self) -> float | None:
        text = self.text().strip()
        if not text:
            return None
        return parse_timecode(text)

    def _normalise(self) -> None:
        text = self.text().strip()
        if not text:
            if not self._allow_empty:
                self.setProperty("invalid", True)
                self._restyle()
            self.value_changed.emit(None)
            return
        parsed = parse_timecode(text)
        self.setProperty("invalid", parsed is None)
        self._restyle()
        if parsed is not None:
            self.setText(format_timecode(parsed))
        self.value_changed.emit(parsed)

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)


def field_label(text: str) -> QLabel:
    """A form label styled consistently across dialogs."""
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def hint_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def scrollable(page: QWidget) -> QScrollArea:
    """Wrap a page so its contents stay reachable when the screen is short.

    A form is only as usable as the screen it lands on. Laptop panels and display
    scaling both cut the logical height a window gets -- 1366x768 at 125% leaves
    614 usable pixels -- and without this the bottom of a form simply cannot be
    reached, however the window is resized.
    """
    area = QScrollArea()
    area.setWidget(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return area


def fitted_size(
    available: QSize, minimum: QSize, desired: QSize, fraction: float = 0.9
) -> tuple[QSize, QSize]:
    """The (minimum, size) a window should take on a display of ``available``.

    Split out from :func:`fit_to_screen` so the arithmetic can be checked against
    any display without needing that display: the guarantee being made is that
    neither result exceeds what the screen can show, for every screen.
    """
    max_width = max(int(available.width() * fraction), 320)
    max_height = max(int(available.height() * fraction), 240)
    return (
        QSize(min(minimum.width(), max_width), min(minimum.height(), max_height)),
        QSize(min(desired.width(), max_width), min(desired.height(), max_height)),
    )


def fit_to_screen(window: QWidget, fraction: float = 0.9, desired: QSize | None = None) -> None:
    """Size and place a window so the display it opens on can actually hold it.

    sizeHint() knows nothing about the screen, and a minimum size larger than the
    screen can never be satisfied: the window opens with its edges -- and so its
    buttons -- outside the desktop, unreachable at any size. Relaxing the minimum
    first is what makes the clamp effective rather than advisory.

    ``desired`` is the size to aim for when the caller has one, such as a geometry
    remembered from a larger monitor; it falls back to the content's own hint.
    """
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    target = desired if desired is not None and desired.isValid() else window.sizeHint()
    new_minimum, new_size = fitted_size(available.size(), window.minimumSize(), target, fraction)
    window.setMinimumSize(new_minimum)
    window.resize(new_size)

    # Shrinking is not enough if the remembered position was on a monitor that is
    # no longer there, or further right than this one extends.
    frame = window.frameGeometry()
    if not available.contains(frame):
        frame.moveCenter(available.center())
        frame.moveLeft(max(frame.left(), available.left()))
        frame.moveTop(max(frame.top(), available.top()))
        window.move(frame.topLeft())
