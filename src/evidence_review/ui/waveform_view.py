"""The picture of a recording, and the surface you work on it with.

An audio file used to show a black rectangle. For EVP review that is close to useless:
the work is three hours of near-silence with a two-second whisper in it somewhere, and a
seek bar gives no clue where to listen. This shows where the sound is, lets you drag out
the span of something you heard, and marks it as an event exactly as the video view does.

Three things it has to get right:

**Quiet has to be visible.** Nothing in EVP is recorded near full scale. Drawn against
the format's range, a whisper is a flat line. The view scales against the loudest sample
in the recording and offers further gain on top, so the interesting part of a quiet file
fills the height.

**Zoom has to reach the detail.** Finding a whisper means getting down to a second or two
on screen. The stored envelope is 10 ms per bucket; below about a second and a half on
screen that is coarser than the pixels, so the visible span is re-read from the file at
the resolution actually being drawn.

**Where you are must always be obvious.** At 1:200 zoom on a three-hour file the main
view shows a thousandth of the recording, so a strip along the bottom keeps the whole
thing in sight with the visible span marked on it.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..media.waveform import Waveform
from ..util import format_timecode
from . import theme
from .marker_bar import EventMark

log = logging.getLogger(__name__)

#: Height of the whole-file strip along the bottom.
OVERVIEW_HEIGHT = 34
#: Height of the time ruler along the top.
RULER_HEIGHT = 18
#: Height of the strip that carries the processing badge and the re-read note.
#: Only present when there is something to say, so an ordinary recording loses no
#: height to it.
NOTICE_HEIGHT = 22
#: A drag shorter than this many pixels is a click, not a span.
DRAG_SLOP = 4
#: Never zoom in past this, which is about forty milliseconds across a wide window.
MIN_VIEW_SECONDS = 0.05
#: The wave itself. Blue against the amber of a status colour and the red of the
#: LOG accent, so a mark drawn over it never reads as part of the recording.
WAVE_COLOUR = "#5ac8fa"


class WaveformView(QWidget):
    """Shows a recording's shape, and lets the reviewer work on it."""

    seek_requested = Signal(float)
    #: A span dragged out on the waveform: the reviewer heard something here.
    span_selected = Signal(float, float)
    mark_activated = Signal(int)
    #: The visible span changed, so a caller can re-read detail for it.
    view_changed = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._waveform: Waveform | None = None
        self._detail: Waveform | None = None
        self._detail_span: tuple[float, float] = (0.0, 0.0)
        self._duration = 0.0
        self._position = 0.0
        self._marks: list[EventMark] = []
        self._open_mark: float | None = None
        self._active_index: int | None = None
        self._gain = 1.0
        self._message = "No recording loaded."
        #: What the drawn envelope has been through, or None when it is the recording
        #: itself. A waveform is read as evidence -- somebody measures a peak off it and
        #: writes that down -- so when the picture is of processed audio it has to say
        #: so on its own face, not only in the panel that happens to be open beside it.
        self._processed: str | None = None
        #: Set while a new envelope is being read behind the one on screen.
        self._reading: str | None = None

        self._view_start = 0.0
        self._view_end = 0.0

        # Columns are expensive on a long recording and depend only on the span,
        # the width and the data -- never on where the playhead is.
        self._columns: list[tuple[int, int]] = []
        self._column_key: tuple | None = None
        self._overview_columns_cache: list[tuple[int, int]] = []
        self._overview_key: tuple | None = None

        self._drag_from: float | None = None
        self._drag_to: float | None = None
        self._panning = False
        self._pan_anchor = QPoint()

        # Re-reading detail on every wheel click would start an ffmpeg per notch.
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(180)
        self._detail_timer.timeout.connect(self._announce_view)

        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setAutoFillBackground(True)

    # ------------------------------------------------------------------ #
    # What is being shown
    # ------------------------------------------------------------------ #

    def set_waveform(self, waveform: Waveform | None) -> None:
        self._waveform = waveform
        self._column_key = self._overview_key = None
        self._detail = None
        if waveform is not None:
            self._duration = waveform.duration
            self._view_start, self._view_end = 0.0, waveform.duration
        self.update()

    def set_detail(self, waveform: Waveform | None, start: float, end: float) -> None:
        """A higher-resolution read of one span, for when the view is zoomed in."""
        self._detail = waveform
        self._column_key = None
        self._detail_span = (start, end)
        self.update()

    def set_processed(self, description: str | None) -> None:
        """Say that the drawn envelope is of cleaned-up audio, and what was done to it.

        ``None`` means the picture is of the recording as it was captured.
        """
        self._processed = description or None
        self.update()

    def set_reading(self, note: str | None) -> None:
        """Note that a new envelope is being read while the old one stays on screen."""
        self._reading = note or None
        self.update()

    def reading(self) -> str | None:
        """The re-read note, or None when the picture is current."""
        return self._reading

    def processed(self) -> str | None:
        """What the drawn envelope has been through, or None for the recording itself."""
        return self._processed

    def set_message(self, message: str) -> None:
        """What to say when there is no waveform: loading, or why there is none."""
        self._message = message
        self.update()

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        if self._view_end <= 0.0:
            self._view_start, self._view_end = 0.0, self._duration
        self.update()

    def set_position(self, seconds: float) -> None:
        self._position = max(0.0, seconds)
        self._follow_playhead()
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
        self._open_mark = start
        self.update()

    def set_gain(self, gain: float) -> None:
        """Extra amplification for the display only. Never touches the audio."""
        self._gain = max(0.1, min(64.0, gain))
        self.update()

    def gain(self) -> float:
        return self._gain

    def view_span(self) -> tuple[float, float]:
        return self._view_start, self._view_end

    def has_waveform(self) -> bool:
        return self._waveform is not None

    # ------------------------------------------------------------------ #
    # Zoom and pan
    # ------------------------------------------------------------------ #

    def set_view(self, start: float, end: float) -> None:
        if self._duration <= 0:
            return
        span = max(MIN_VIEW_SECONDS, min(self._duration, end - start))
        start = max(0.0, min(self._duration - span, start))
        self._view_start, self._view_end = start, start + span
        self.update()
        self._detail_timer.start()

    def zoom_by(self, factor: float, around: float | None = None) -> None:
        """Zoom keeping ``around`` (seconds) under the same pixel."""
        if self._duration <= 0:
            return
        anchor = self._position if around is None else around
        span = (self._view_end - self._view_start) / factor
        span = max(MIN_VIEW_SECONDS, min(self._duration, span))
        fraction = 0.5
        current = self._view_end - self._view_start
        if current > 0:
            fraction = min(1.0, max(0.0, (anchor - self._view_start) / current))
        self.set_view(anchor - span * fraction, anchor - span * fraction + span)

    def reset_zoom(self) -> None:
        self.set_view(0.0, self._duration or 1.0)

    def zoom_to_span(self, start: float, end: float, *, margin: float = 0.35) -> None:
        """Frame one event with a little room either side."""
        span = max(MIN_VIEW_SECONDS, end - start)
        pad = span * margin
        self.set_view(start - pad, end + pad)

    def _follow_playhead(self) -> None:
        """Scroll when the playhead leaves the view, but never while being dragged."""
        if self._drag_from is not None or self._duration <= 0:
            return
        span = self._view_end - self._view_start
        if span >= self._duration:
            return
        if self._view_start <= self._position <= self._view_end:
            return
        # Put the playhead a third in, so there is more ahead than behind -- the
        # reviewer is listening forwards.
        self.set_view(self._position - span / 3, self._position - span / 3 + span)

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #

    def _notice_rect(self) -> QRect:
        """The strip carrying the badge and the re-read note, empty when there is none.

        It is reserved out of the wave's own height rather than drawn on top of it. A
        label floating over the picture covers the tallest peaks -- which on an EVP
        recording is exactly the event somebody is hunting, since the whole point of the
        cleanup is to make that peak the tallest thing on screen. A disclosure that
        hides the evidence it is disclosing about is the wrong trade.

        It stays inside the widget, so it still travels into the snapshot an audio entry
        carries: the view is grabbed whole.
        """
        if not (self._processed or self._reading):
            return QRect(0, RULER_HEIGHT, self.width(), 0)
        return QRect(0, RULER_HEIGHT, self.width(), NOTICE_HEIGHT)

    def _wave_rect(self) -> QRect:
        top = RULER_HEIGHT + self._notice_rect().height()
        return QRect(
            0,
            top,
            self.width(),
            max(10, self.height() - top - OVERVIEW_HEIGHT),
        )

    def _overview_rect(self) -> QRect:
        return QRect(0, self.height() - OVERVIEW_HEIGHT, self.width(), OVERVIEW_HEIGHT)

    def _x_for(self, seconds: float) -> float:
        span = self._view_end - self._view_start
        if span <= 0:
            return 0.0
        return (seconds - self._view_start) / span * self.width()

    def _seconds_at(self, x: float) -> float:
        span = self._view_end - self._view_start
        if self.width() <= 0:
            return self._view_start
        return self._view_start + (x / self.width()) * span

    def _overview_seconds_at(self, x: float) -> float:
        if self.width() <= 0 or self._duration <= 0:
            return 0.0
        return max(0.0, min(self._duration, x / self.width() * self._duration))

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(theme.BG_PANEL))

        wave_rect = self._wave_rect()
        # BG_SUNKEN is the theme's own colour for the video letterbox: a hole in the
        # surface rather than a panel on it, which is what this well is too.
        painter.fillRect(wave_rect, QColor(theme.BG_SUNKEN))

        if self._waveform is None or self._duration <= 0:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return

        self._paint_ruler(painter)
        self._paint_marks(painter, wave_rect)
        self._paint_wave(painter, wave_rect)
        self._paint_mark_edges(painter, wave_rect)
        self._paint_selection(painter, wave_rect)
        self._paint_playhead(painter, wave_rect)
        self._paint_overview(painter)
        self._paint_notices(painter)
        painter.end()

    def _source_for_drawing(self) -> Waveform:
        """The detail read if it covers the view, otherwise the stored envelope."""
        detail = self._detail
        if detail is not None:
            start, end = self._detail_span
            if start <= self._view_start + 1e-6 and end >= self._view_end - 1e-6:
                return detail
        return self._waveform  # type: ignore[return-value]

    def _scale(self) -> float:
        """Samples to half-height, normalised against the recording's own loudest part.

        Against the format's full range a quiet recording is a flat line, which is
        exactly the recording somebody is studying.
        """
        source = self._waveform
        reference = max(1, source.peak_amplitude if source else 1)
        return self._gain / reference

    def _columns_for(self, rect: QRect) -> list[tuple[int, int]]:
        """The drawn columns, worked out once per view rather than once per paint.

        Reducing a million buckets to a thousand columns costs about 220 ms on a
        three-hour recording, and the playhead repaints several times a second -- so
        without this the view spent more time recomputing an unchanged picture than
        playing, and got slower the longer the recording. The result depends only on the
        span, the width and the data, none of which the playhead moving changes.
        """
        source = self._source_for_drawing()
        offset = self._detail_span[0] if source is self._detail else 0.0
        key = (
            id(source),
            self._view_start,
            self._view_end,
            rect.width(),
            offset,
        )
        if key != self._column_key:
            self._column_key = key
            self._columns = source.window(
                self._view_start - offset, self._view_end - offset, rect.width()
            )
        return self._columns

    def _overview_columns(self, rect: QRect) -> list[tuple[int, int]]:
        """The whole-file strip, which only changes when the recording does."""
        source = self._waveform
        if source is None:
            return []
        key = (id(source), rect.width())
        if key != self._overview_key:
            self._overview_key = key
            self._overview_columns_cache = source.window(0.0, self._duration, rect.width())
        return self._overview_columns_cache

    def _paint_notices(self, painter: QPainter) -> None:
        """Paint the processing badge and the re-read note into their reserved strip.

        Both say the same kind of thing -- the picture is not simply the recording -- so
        they share one strip: the badge on the left because it is the standing claim,
        the note on the right because it is the passing one. When both are up the badge
        gives way first, since "this is processed" still holds while a new read runs.

        Amber for the badge, matching the cleanup panel's banner and the status bar:
        three places saying it, in one colour, so it cannot be read as decoration. The
        wave itself stays blue -- amber already means a clipped column there, and one
        colour cannot carry two meanings on the same picture.
        """
        rect = self._notice_rect()
        if rect.height() <= 0:
            return

        painter.save()
        painter.fillRect(rect, QColor(theme.WARNING if self._processed else theme.BG_ELEVATED))

        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 0.5))
        metrics = QFontMetrics(font)

        note = self._reading or ""
        note_width = metrics.horizontalAdvance(note) + 16 if note else 0
        ink = QColor(theme.BG_SUNKEN if self._processed else theme.TEXT_MUTED)

        if self._processed:
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            # Elided, never clipped: a truncated chain reads as a shorter chain.
            label = metrics.elidedText(
                f"SHOWING PROCESSED AUDIO — {self._processed}",
                Qt.TextElideMode.ElideRight,
                max(40, rect.width() - note_width - 24),
            )
            painter.setPen(QPen(ink))
            painter.drawText(
                rect.adjusted(10, 0, 0, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )

        if note:
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QPen(ink))
            painter.drawText(
                rect.adjusted(0, 0, -10, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                note,
            )
        painter.restore()

    def _paint_wave(self, painter: QPainter, rect: QRect) -> None:
        columns = self._columns_for(rect)
        if not columns:
            return

        middle = rect.center().y() + 1
        half = rect.height() / 2 - 2
        scale = self._scale()

        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(rect.left(), middle, rect.right(), middle)

        wave_colour = QColor(WAVE_COLOUR)
        over_colour = QColor(theme.WARNING)
        painter.setPen(QPen(wave_colour, 1))

        over_columns: list[int] = []
        for index, (low, high) in enumerate(columns):
            x = rect.left() + index
            top = middle - min(half, high * scale * half)
            bottom = middle - max(-half, low * scale * half)
            if (high * scale) > 1.0 or (-low * scale) > 1.0:
                over_columns.append(int(x))
            if abs(top - bottom) < 1:
                painter.drawPoint(int(x), int(middle))
            else:
                painter.drawLine(int(x), int(top), int(x), int(bottom))

        # Where the gain drives a column past the top, say so with a thin mark at the
        # edge it ran past. Colouring the whole column instead turned the very moment
        # somebody had zoomed in to study into a solid block with no shape in it.
        if over_columns:
            painter.setPen(QPen(over_colour, 1))
            for x in over_columns:
                painter.drawLine(x, rect.top(), x, rect.top() + 2)
                painter.drawLine(x, rect.bottom() - 2, x, rect.bottom())

    def _paint_ruler(self, painter: QPainter) -> None:
        span = self._view_end - self._view_start
        if span <= 0:
            return
        step = _tick_step(span)
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        metrics = QFontMetrics(painter.font())

        first = self._view_start - (self._view_start % step)
        tick = first
        while tick <= self._view_end:
            x = self._x_for(tick)
            if 0 <= x <= self.width():
                painter.drawLine(int(x), RULER_HEIGHT - 4, int(x), RULER_HEIGHT)
                label = format_timecode(tick, millis=step < 1)
                width = metrics.horizontalAdvance(label)
                if x + width / 2 < self.width() and x - width / 2 > 0:
                    painter.drawText(int(x - width / 2), RULER_HEIGHT - 6, label)
            tick += step

    def _paint_marks(self, painter: QPainter, rect: QRect) -> None:
        for index, mark in enumerate(self._marks):
            start = self._x_for(mark.start)
            end = self._x_for(mark.effective_end)
            if end < 0 or start > self.width():
                continue
            width = max(2.0, end - start)
            colour = QColor(theme.status_colour(mark.status) if mark.is_logged else theme.ACCENT)
            colour.setAlpha(70 if index != self._active_index else 115)
            painter.fillRect(QRectF(start, rect.top(), width, rect.height()), colour)

            edge = QColor(colour)
            edge.setAlpha(200)
            painter.setPen(QPen(edge, 1))
            painter.drawLine(int(start), rect.top(), int(start), rect.bottom())

        if self._open_mark is not None:
            start = self._x_for(self._open_mark)
            end = self._x_for(self._position)
            pen = QPen(QColor(theme.ACCENT), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(
                QRectF(
                    min(start, end), rect.top() + 1, max(2.0, abs(end - start)), rect.height() - 2
                )
            )

    def _paint_mark_edges(self, painter: QPainter, rect: QRect) -> None:
        """The boundaries of each event, over the wave rather than under it.

        The shading goes underneath so the shape stays readable, but a span sitting
        under a loud event would then be invisible -- which is the span most worth
        seeing, since loud is why it was logged.
        """
        for index, mark in enumerate(self._marks):
            start = self._x_for(mark.start)
            end = max(start + 2.0, self._x_for(mark.effective_end))
            if end < 0 or start > self.width():
                continue
            colour = QColor(theme.status_colour(mark.status) if mark.is_logged else theme.ACCENT)
            painter.setPen(QPen(colour, 2 if index == self._active_index else 1))
            painter.drawLine(int(start), rect.top(), int(start), rect.bottom())
            painter.drawLine(int(end), rect.top(), int(end), rect.bottom())
            painter.drawLine(int(start), rect.top(), int(end), rect.top())

    def _paint_selection(self, painter: QPainter, rect: QRect) -> None:
        if self._drag_from is None or self._drag_to is None:
            return
        start, end = sorted((self._drag_from, self._drag_to))
        left, right = self._x_for(start), self._x_for(end)
        if right - left < 1:
            return
        fill = QColor(theme.ACCENT)
        fill.setAlpha(60)
        painter.fillRect(QRectF(left, rect.top(), right - left, rect.height()), fill)
        painter.setPen(QPen(QColor(theme.ACCENT), 1))
        painter.drawRect(QRectF(left, rect.top() + 1, right - left, rect.height() - 2))

        label = f"{format_timecode(end - start)}"
        painter.drawText(int(left) + 4, rect.top() + 14, label)

    def _paint_playhead(self, painter: QPainter, rect: QRect) -> None:
        x = self._x_for(self._position)
        if -1 <= x <= self.width() + 1:
            painter.setPen(QPen(QColor(theme.TEXT), 1.5))
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())

    def _paint_overview(self, painter: QPainter) -> None:
        """The whole recording, with the visible span marked on it.

        Without this, zooming into two seconds of a three-hour file leaves no way to
        tell where you are.
        """
        rect = self._overview_rect()
        painter.fillRect(rect, QColor(theme.BG_BASE))

        source = self._waveform
        if source is None or self._duration <= 0:
            return

        columns = self._overview_columns(rect)
        middle = rect.center().y()
        half = rect.height() / 2 - 2
        scale = self._scale()
        painter.setPen(QPen(QColor(theme.TEXT_MUTED), 1))
        for index, (low, high) in enumerate(columns):
            top = middle - min(half, high * scale * half)
            bottom = middle - max(-half, low * scale * half)
            painter.drawLine(rect.left() + index, int(top), rect.left() + index, int(bottom))

        # The visible span.
        left = self._view_start / self._duration * rect.width()
        right = self._view_end / self._duration * rect.width()
        window = QColor(theme.ACCENT)
        window.setAlpha(45)
        painter.fillRect(QRectF(left, rect.top(), max(2.0, right - left), rect.height()), window)
        painter.setPen(QPen(QColor(theme.ACCENT), 1))
        painter.drawRect(QRectF(left, rect.top(), max(2.0, right - left), rect.height() - 1))

        played = self._position / self._duration * rect.width()
        painter.setPen(QPen(QColor(theme.TEXT), 1))
        painter.drawLine(int(played), rect.top(), int(played), rect.bottom())

    # ------------------------------------------------------------------ #
    # Mouse
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._waveform is None:
            return
        position = event.position()

        if self._overview_rect().contains(position.toPoint()):
            # Clicking the strip jumps the view there, keeping the current zoom.
            span = self._view_end - self._view_start
            centre = self._overview_seconds_at(position.x())
            self.set_view(centre - span / 2, centre + span / 2)
            return

        if event.button() is Qt.MouseButton.MiddleButton or (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._panning = True
            self._pan_anchor = position.toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() is not Qt.MouseButton.LeftButton:
            return

        index = self._mark_at(position.x())
        if index is not None and event.type() == QMouseEvent.Type.MouseButtonDblClick:
            self.mark_activated.emit(index)
            return

        self._drag_from = self._seconds_at(position.x())
        self._drag_to = self._drag_from
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        position = event.position()

        if self._panning:
            moved = self._pan_anchor.x() - position.x()
            span = self._view_end - self._view_start
            shift = moved / max(1, self.width()) * span
            self.set_view(self._view_start + shift, self._view_end + shift)
            self._pan_anchor = position.toPoint()
            return

        if self._drag_from is not None:
            self._drag_to = self._seconds_at(position.x())
            self.update()
            return

        if self._waveform is not None and not self._overview_rect().contains(position.toPoint()):
            index = self._mark_at(position.x())
            if index is not None:
                mark = self._marks[index]
                text = mark.describe_span()
                if mark.observation:
                    text += f"\n{mark.observation[:120]}"
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
            else:
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    format_timecode(self._seconds_at(position.x())),
                    self,
                )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.IBeamCursor)
            return
        if self._drag_from is None:
            return

        start, end = self._drag_from, self._drag_to
        self._drag_from = self._drag_to = None
        self.update()
        if start is None or end is None:
            return

        moved = abs(self._x_for(end) - self._x_for(start))
        if moved < DRAG_SLOP:
            # A click, not a drag: go there.
            self.seek_requested.emit(max(0.0, start))
            return
        first, last = sorted((start, end))
        self.span_selected.emit(max(0.0, first), max(0.0, last))

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._waveform is None:
            return
        index = self._mark_at(event.position().x())
        if index is not None:
            self.mark_activated.emit(index)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._waveform is None or self._duration <= 0:
            event.ignore()
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            event.ignore()
            return
        self.zoom_by(1.25**steps, around=self._seconds_at(event.position().x()))
        event.accept()

    def _mark_at(self, x: float) -> int | None:
        best: int | None = None
        best_width = float("inf")
        for index, mark in enumerate(self._marks):
            left = self._x_for(mark.start)
            right = max(left + 2.0, self._x_for(mark.effective_end))
            if left - 2 <= x <= right + 2 and (right - left) < best_width:
                best, best_width = index, right - left
        return best

    def _announce_view(self) -> None:
        self.view_changed.emit(self._view_start, self._view_end)


def _tick_step(span: float) -> float:
    """A round interval giving roughly eight labels across the view."""
    rough = span / 8
    for step in (
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2,
        5,
        10,
        15,
        30,
        60,
        120,
        300,
        600,
        900,
        1800,
        3600,
        7200,
    ):
        if rough <= step:
            return float(step)
    return 7200.0
