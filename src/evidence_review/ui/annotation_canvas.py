"""The drawing surface: one still frame with marks laid over it.

Annotations are edited against a frozen frame rather than over live video. mpv renders
into a native child window that paints over anything Qt puts on top of it, so drawing
directly on the player would mean fighting the compositor for every pixel. A still frame
sidesteps that entirely, and it is the better tool anyway: the reviewer is placing a
circle around something specific and wants it to hold still while they do it.

The canvas owns no state that matters. It holds a list of :class:`Annotation` and hands
back a new list whenever the reviewer changes something; the dialog decides whether any
of it is saved.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..annotations import Annotation, AnnotationKind, render

#: How close to a corner counts as grabbing it, in widget pixels.
HANDLE_GRAB = 10.0

#: A drag shorter than this is a click, not an attempt to draw a shape.
CLICK_SLOP = 0.012


class AnnotationCanvas(QWidget):
    """Shows a frame and lets the reviewer draw on it."""

    annotations_changed = Signal()
    selection_changed = Signal(object)  # Annotation | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame = QImage()
        self._annotations: list[Annotation] = []
        self._selected: str | None = None
        self._tool: AnnotationKind | None = None
        self._template = Annotation(entry_id="")  # carries the current colour and sizes

        self._drag: str | None = None  # "new" | "move" | "resize"
        self._handle = 0
        self._grab_at: tuple[float, float] = (0.0, 0.0)
        self._before: Annotation | None = None

        self.setMinimumSize(360, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------------ #
    # What is being edited
    # ------------------------------------------------------------------ #

    def set_frame(self, frame: QImage) -> None:
        self._frame = frame if frame is not None else QImage()
        self.update()

    def set_annotations(self, annotations: list[Annotation]) -> None:
        self._annotations = list(annotations)
        if self._selected and not any(a.annotation_id == self._selected for a in self._annotations):
            self._selected = None
        self.update()

    def annotations(self) -> list[Annotation]:
        return list(self._annotations)

    def set_tool(self, tool: AnnotationKind | None) -> None:
        """``None`` is the select tool."""
        self._tool = tool
        self.setCursor(
            Qt.CursorShape.ArrowCursor if tool is None else Qt.CursorShape.CrossCursor
        )

    def tool(self) -> AnnotationKind | None:
        return self._tool

    def set_style(self, *, colour: str | None = None, stroke: float | None = None,
                  font_scale: float | None = None) -> None:
        """The look new marks start with, and the look the selected one takes on."""
        changes = {
            key: value
            for key, value in (
                ("colour", colour), ("stroke", stroke), ("font_scale", font_scale)
            )
            if value is not None
        }
        if not changes:
            return
        self._template = self._template.moved_to(**changes)
        selected = self.selected()
        if selected is not None:
            self._replace(selected.moved_to(**changes))

    def selected(self) -> Annotation | None:
        return next(
            (a for a in self._annotations if a.annotation_id == self._selected), None
        )

    def select(self, annotation_id: str | None) -> None:
        self._selected = annotation_id
        self.update()
        self.selection_changed.emit(self.selected())

    def update_selected(self, **changes) -> None:
        selected = self.selected()
        if selected is not None:
            self._replace(selected.moved_to(**changes))

    def remove_selected(self) -> None:
        selected = self.selected()
        if selected is None:
            return
        self._annotations = [
            a for a in self._annotations if a.annotation_id != selected.annotation_id
        ]
        self._selected = None
        self.update()
        self.annotations_changed.emit()
        self.selection_changed.emit(None)

    # ------------------------------------------------------------------ #
    # Where the picture is
    # ------------------------------------------------------------------ #

    def video_rect(self) -> QRectF:
        """The rectangle the frame occupies, letterboxed inside the widget.

        Marks are positioned against the picture, not the widget, so everything maps
        through here. Getting this wrong puts a circle in the black bars.
        """
        if self._frame.isNull() or self._frame.width() <= 0:
            return QRectF(self.rect())
        available = QRectF(self.rect())
        aspect = self._frame.width() / self._frame.height()
        width = available.width()
        height = width / aspect
        if height > available.height():
            height = available.height()
            width = height * aspect
        return QRectF(
            available.left() + (available.width() - width) / 2,
            available.top() + (available.height() - height) / 2,
            width, height,
        )

    def _to_fraction(self, position: QPointF) -> tuple[float, float]:
        rect = self.video_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return 0.0, 0.0
        return (
            min(1.0, max(0.0, (position.x() - rect.left()) / rect.width())),
            min(1.0, max(0.0, (position.y() - rect.top()) / rect.height())),
        )

    def _to_widget(self, x: float, y: float) -> QPointF:
        rect = self.video_rect()
        return QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101216"))
        rect = self.video_rect()

        if not self._frame.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(rect, self._frame)
        else:
            painter.setPen(QPen(QColor("#6b7280")))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                "No frame to draw on.\nOpen the media and seek to the event first.",
            )
            painter.end()
            return

        # The same renderer the player and the export use, at the size it is shown.
        overlay = render(
            self._annotations, int(rect.width()), int(rect.height()),
            selected_id=self._selected,
        )
        painter.drawImage(rect.topLeft(), overlay)

        # A hairline around the picture, so the edge of the frame is obvious when the
        # footage is dark and the letterboxing is not.
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()

    # ------------------------------------------------------------------ #
    # Mouse
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is not Qt.MouseButton.LeftButton or self._frame.isNull():
            return
        position = event.position()
        fraction = self._to_fraction(position)

        if self._tool is None:
            self._begin_edit(position, fraction)
            return

        # Drawing a new mark. It starts as a dot and grows with the drag; a plain click
        # leaves a default-sized one, which is what somebody placing a text label does.
        new = self._template.moved_to(
            annotation_id=Annotation(entry_id="").annotation_id,
            kind=self._tool,
            x1=fraction[0], y1=fraction[1], x2=fraction[0], y2=fraction[1],
            text="",
        )
        self._annotations.append(new)
        self._selected = new.annotation_id
        self._drag = "new"
        self._grab_at = fraction
        self._before = new
        self.update()
        self.selection_changed.emit(new)

    def _begin_edit(self, position: QPointF, fraction: tuple[float, float]) -> None:
        """Select what is under the cursor, and work out whether this is a move or a resize."""
        selected = self.selected()
        if selected is not None:
            handle = self._handle_at(selected, position)
            if handle is not None:
                self._drag, self._handle = "resize", handle
                self._grab_at, self._before = fraction, selected
                return

        hit = self._topmost_at(fraction)
        self._selected = hit.annotation_id if hit else None
        self.update()
        self.selection_changed.emit(hit)
        if hit is not None:
            self._drag, self._grab_at, self._before = "move", fraction, hit

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is None or self._before is None:
            return
        fraction = self._to_fraction(event.position())
        dx, dy = fraction[0] - self._grab_at[0], fraction[1] - self._grab_at[1]

        if self._drag == "new":
            self._replace(self._before.moved_to(x2=fraction[0], y2=fraction[1]))
        elif self._drag == "move":
            self._replace(self._before.moved_to(
                x1=self._before.x1 + dx, y1=self._before.y1 + dy,
                x2=self._before.x2 + dx, y2=self._before.y2 + dy,
            ))
        elif self._drag == "resize":
            corner = {
                0: {"x1": fraction[0], "y1": fraction[1]},
                1: {"x2": fraction[0], "y1": fraction[1]},
                2: {"x1": fraction[0], "y2": fraction[1]},
                3: {"x2": fraction[0], "y2": fraction[1]},
            }[self._handle]
            self._replace(self._before.moved_to(**corner))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is None:
            return
        finishing, self._drag, self._before = self._drag, None, None

        selected = self.selected()
        if finishing == "new" and selected is not None:
            width = abs(selected.x2 - selected.x1)
            height = abs(selected.y2 - selected.y1)
            if width < CLICK_SLOP and height < CLICK_SLOP:
                # A click rather than a drag. Give it a usable default size instead of
                # leaving a zero-sized mark that cannot be seen or grabbed again.
                if selected.kind is AnnotationKind.TEXT:
                    sized = selected.moved_to(x2=selected.x1, y2=selected.y1)
                else:
                    sized = selected.moved_to(
                        x1=selected.x1 - 0.06, y1=selected.y1 - 0.06,
                        x2=selected.x1 + 0.06, y2=selected.y1 + 0.06,
                    )
                self._replace(sized)
        self.annotations_changed.emit()

    # ------------------------------------------------------------------ #

    def _replace(self, annotation: Annotation) -> None:
        self._annotations = [
            annotation if a.annotation_id == annotation.annotation_id else a
            for a in self._annotations
        ]
        self.update()
        self.annotations_changed.emit()
        if annotation.annotation_id == self._selected:
            self.selection_changed.emit(annotation)

    def _handle_at(self, annotation: Annotation, position: QPointF) -> int | None:
        if annotation.kind is AnnotationKind.TEXT:
            return None
        corners = [
            (annotation.x1, annotation.y1), (annotation.x2, annotation.y1),
            (annotation.x1, annotation.y2), (annotation.x2, annotation.y2),
        ]
        for index, (x, y) in enumerate(corners):
            point = self._to_widget(x, y)
            if (point - position).manhattanLength() <= HANDLE_GRAB * 2:
                return index
        return None

    def _topmost_at(self, fraction: tuple[float, float]) -> Annotation | None:
        """The last mark drawn that contains this point, since that is the one on top."""
        x, y = fraction
        rect = self.video_rect()
        # A line is thin: allow a few pixels either side, expressed as a fraction so it
        # behaves the same whatever size the dialog happens to be.
        slack_x = (HANDLE_GRAB / rect.width()) if rect.width() else 0.01
        slack_y = (HANDLE_GRAB / rect.height()) if rect.height() else 0.01
        for annotation in reversed(self._annotations):
            left, right = sorted((annotation.x1, annotation.x2))
            top, bottom = sorted((annotation.y1, annotation.y2))
            if annotation.kind is AnnotationKind.TEXT:
                # Text hangs down and to the right of its anchor.
                right = max(right, left + 0.25)
                bottom = max(bottom, top + annotation.font_scale * 1.6)
            if (left - slack_x <= x <= right + slack_x
                    and top - slack_y <= y <= bottom + slack_y):
                return annotation
        return None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected()
            return
        super().keyPressEvent(event)

    def frame_size(self) -> QRect:
        return self._frame.rect()
