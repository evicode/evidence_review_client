"""The strip under the video showing every tagged event in the current file.

Two kinds of mark share it: entries already logged, and marks the reviewer has
just made but not yet written up. Both are navigable, so tagging can run ahead of
typing -- which is the point of marking with a single key while the video plays.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..util import format_timecode
from .theme import ACCENT, status_colour

#: Height of the strip. Tall enough to hit with the mouse, short enough to read
#: as an annotation of the seek bar rather than a second scrubber.
BAR_HEIGHT = 20
MIN_MARK_PIXELS = 3.0

#: The dark theme's own values. Both are derived from the palette at paint time
#: so the bar is legible on a light theme too - it is custom-painted, so clearing
#: the stylesheet does nothing for it, and it used to stay a dark strip with an
#: invisible white playhead whatever the rest of the window did.
TRACK_COLOUR = QColor("#23272c")
PENDING_COLOUR = QColor(ACCENT)
PLAYHEAD_COLOUR = QColor("#ffffff")


@dataclass(slots=True)
class EventMark:
    """One tagged span of a media file.

    ``entry_id`` is set once the mark has been written up as a log entry; until
    then it is a pending mark that lives only for this session.
    """

    start: float
    end: float | None = None
    entry_id: str | None = None
    observation: str = ""
    status: str = ""

    @property
    def is_logged(self) -> bool:
        return self.entry_id is not None

    @property
    def duration(self) -> float | None:
        if self.end is None:
            return None
        return max(0.0, self.end - self.start)

    @property
    def effective_end(self) -> float:
        """Where the mark finishes, treating an unclosed mark as a point."""
        return self.start if self.end is None else self.end

    def describe_span(self) -> str:
        """Where the event is and how long it lasts, as the reviewer reads it.

        Lives here because two places show it - the hover tooltip and the status
        bar when a mark is selected - and they had drifted: the tooltip gave the
        span, the status bar gave only the start, so stepping onto a marked event
        with P or N appeared to show an event with no end.
        """
        text = format_timecode(self.start, millis=False)
        if self.duration:
            text += f" for {format_timecode(self.duration, millis=False)}"
        return text

    def contains(self, seconds: float) -> bool:
        return self.start <= seconds <= self.effective_end


class MarkerBar(QWidget):
    """Draws marks against the media timeline and lets them be clicked."""

    #: A mark was clicked; carries its index in the list last given to set_marks.
    mark_activated = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._duration = 0.0
        self._position = 0.0
        self._marks: list[EventMark] = []
        self._active_index: int | None = None
        self._open_start: float | None = None

        self.setFixedHeight(BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Tagged events in this file")

    # -- state ---------------------------------------------------------------- #

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, float(seconds))
        self.update()

    def set_position(self, seconds: float) -> None:
        self._position = max(0.0, float(seconds))
        # An open mark grows with the playhead, so repaint while one is running.
        if self._open_start is not None or self._duration > 0:
            self.update()

    def set_marks(self, marks: list[EventMark]) -> None:
        self._marks = list(marks)
        if self._active_index is not None and self._active_index >= len(self._marks):
            self._active_index = None
        self.update()

    def set_active_index(self, index: int | None) -> None:
        self._active_index = index
        self.update()

    def set_open_mark(self, start: float | None) -> None:
        """Show a mark that has been started but not yet closed."""
        self._open_start = start
        self.update()

    # -- geometry ------------------------------------------------------------- #

    def _x_for(self, seconds: float) -> float:
        if self._duration <= 0:
            return 0.0
        ratio = min(max(seconds / self._duration, 0.0), 1.0)
        return ratio * self.width()

    def _seconds_at(self, x: float) -> float:
        if self.width() <= 0 or self._duration <= 0:
            return 0.0
        return min(max(x / self.width(), 0.0), 1.0) * self._duration

    def _index_at(self, x: float) -> int | None:
        """The mark under a pixel, preferring the narrowest so short marks stay
        clickable when they sit inside longer ones."""
        best: tuple[float, int] | None = None
        for index, mark in enumerate(self._marks):
            left = self._x_for(mark.start)
            right = max(self._x_for(mark.effective_end), left + MIN_MARK_PIXELS)
            if left - 2 <= x <= right + 2:
                width = right - left
                if best is None or width < best[0]:
                    best = (width, index)
        return None if best is None else best[1]

    # -- painting ------------------------------------------------------------- #

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        track_colour, contrast_colour = self._theme_colours()

        track = QRectF(0, BAR_HEIGHT * 0.3, self.width(), BAR_HEIGHT * 0.4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_colour)
        painter.drawRoundedRect(track, 3, 3)

        if self._duration <= 0:
            return

        for index, mark in enumerate(self._marks):
            self._paint_mark(painter, index, mark)

        if self._open_start is not None:
            self._paint_open_mark(painter)

        playhead_x = self._x_for(self._position)
        painter.setPen(QPen(contrast_colour, 1.5))
        painter.drawLine(int(playhead_x), 2, int(playhead_x), BAR_HEIGHT - 2)

    def _theme_colours(self) -> tuple[QColor, QColor]:
        """The track, and whatever contrasts with it for the playhead and outlines.

        Taken from the palette rather than fixed, so this widget follows the theme
        like everything else. On the dark theme the window is dark and these come
        out as the designed values.
        """
        window = self.palette().color(self.backgroundRole())
        if window.lightness() < 128:
            return TRACK_COLOUR, PLAYHEAD_COLOUR
        return window.darker(118), QColor("#1a1d21")

    def _paint_mark(self, painter: QPainter, index: int, mark: EventMark) -> None:
        left = self._x_for(mark.start)
        right = max(self._x_for(mark.effective_end), left + MIN_MARK_PIXELS)
        active = index == self._active_index

        colour = QColor(status_colour(mark.status)) if mark.is_logged else QColor(PENDING_COLOUR)
        if not mark.is_logged:
            colour.setAlpha(210)

        rect = QRectF(left, BAR_HEIGHT * 0.18, right - left, BAR_HEIGHT * 0.64)
        painter.setBrush(colour)
        if active:
            painter.setPen(QPen(self._theme_colours()[1], 1.4))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 2.5, 2.5)

        # A pending mark gets a tick above it, so "not written up yet" is visible
        # at a glance without relying on colour alone.
        if not mark.is_logged:
            painter.setPen(QPen(QColor(PENDING_COLOUR), 1.6))
            painter.drawLine(int(left), 1, int(min(right, left + 6)), 1)

    def _paint_open_mark(self, painter: QPainter) -> None:
        start = self._open_start or 0.0
        left = self._x_for(start)
        right = max(self._x_for(self._position), left + MIN_MARK_PIXELS)

        colour = QColor(PENDING_COLOUR)
        colour.setAlpha(110)
        painter.setBrush(colour)
        painter.setPen(QPen(QColor(PENDING_COLOUR), 1.0, Qt.PenStyle.DashLine))
        painter.drawRoundedRect(
            QRectF(left, BAR_HEIGHT * 0.18, right - left, BAR_HEIGHT * 0.64), 2.5, 2.5
        )

    # -- interaction ---------------------------------------------------------- #

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        index = self._index_at(event.position().x())
        if index is not None:
            self.mark_activated.emit(index)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        index = self._index_at(event.position().x())
        if index is None:
            QToolTip.hideText()
            self.setToolTip(
                f"Tagged events in this file  —  {format_timecode(self._seconds_at(event.position().x()))}"
            )
        else:
            mark = self._marks[index]
            span = mark.describe_span()
            detail = (
                " ".join(mark.observation.split())[:90] if mark.observation else "not logged yet"
            )
            QToolTip.showText(event.globalPosition().toPoint(), f"{span}\n{detail}", self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        QToolTip.hideText()
        super().leaveEvent(event)
