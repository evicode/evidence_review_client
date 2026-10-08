"""Marks drawn over the picture: text, arrows, circles, boxes.

Two rules shape everything here.

**The recording is never touched.** An annotation is a separate record that points at a
moment and a place in the frame. It is drawn on top at playback and burned into a copy
only when somebody asks for that, and the copy says so in its provenance file. The file
the camera wrote stays exactly as it was.

**Coordinates are fractions of the frame, never pixels.** The same annotation is drawn
onto a 640x360 preview, a 1920x1080 export and whatever window the reviewer happens to
have open. Storing pixels would mean a circle that lands on a face in the player and on
a doorway in the export, which in this application is not a cosmetic problem.

The drawing is done in one place, :func:`render`, and that same function produces both
the overlay mpv composites during playback and the image ffmpeg burns in. A reviewer who
exports what they were looking at gets what they were looking at.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import uuid
from enum import Enum
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
)

log = logging.getLogger(__name__)

#: Drawn at this fraction of the frame height unless an annotation says otherwise.
DEFAULT_STROKE = 0.005
DEFAULT_FONT_SCALE = 0.05

#: Amber by default: it reads against both the dark and the washed-out footage that
#: night-time and daylight CCTV respectively produce.
DEFAULT_COLOUR = "#ffb020"

#: Offered in the editor. Chosen to stay apart from one another for a viewer with
#: colour-vision deficiency, rather than to look like a palette.
PALETTE: tuple[tuple[str, str], ...] = (
    ("Amber", "#ffb020"),
    ("Red", "#ff3b30"),
    ("Green", "#34c759"),
    ("Blue", "#4aa3ff"),
    ("White", "#ffffff"),
    ("Black", "#101010"),
)


def new_annotation_id() -> str:
    return str(uuid.uuid4())


class AnnotationKind(str, Enum):
    """What is drawn.

    ``TEXT`` anchors at its first point and ignores the second. The rest are defined by
    two points: a bounding box for ``ELLIPSE`` and ``RECTANGLE``, a start and an end for
    ``ARROW`` and ``LINE``.
    """

    TEXT = "text"
    ARROW = "arrow"
    ELLIPSE = "ellipse"
    RECTANGLE = "rectangle"
    LINE = "line"

    @property
    def label(self) -> str:
        return {
            AnnotationKind.TEXT: "Text",
            AnnotationKind.ARROW: "Arrow",
            AnnotationKind.ELLIPSE: "Circle",
            AnnotationKind.RECTANGLE: "Box",
            AnnotationKind.LINE: "Line",
        }[self]

    @property
    def uses_second_point(self) -> bool:
        return self is not AnnotationKind.TEXT


class Annotation(BaseModel):
    """One mark on the picture.

    The time window is absolute within the source media, in seconds, like
    ``event_offset_seconds`` on an entry. It is stored rather than inherited from the
    entry so that a mark can be made to appear only while the thing it points at is
    actually in shot.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    annotation_id: str = Field(default_factory=new_annotation_id)
    #: Which entry this mark belongs to.
    #:
    #: Informational on the wire, where the mark is already nested inside its
    #: entry, so it is allowed to be absent: the store writes whichever id it was
    #: handed rather than trusting this. Requiring it meant a server that did not
    #: echo it made the whole entry unparseable, and the pull skipped it in
    #: silence -- marks breaking the sync of the entries that carried them.
    entry_id: str = ""
    kind: AnnotationKind = AnnotationKind.ELLIPSE

    #: Fractions of the frame, 0..1 from the top left.
    x1: float = 0.4
    y1: float = 0.4
    x2: float = 0.6
    y2: float = 0.6

    text: str = ""
    colour: str = DEFAULT_COLOUR
    #: Line thickness as a fraction of frame height, so it scales with the picture.
    stroke: float = DEFAULT_STROKE
    #: Cap height as a fraction of frame height.
    font_scale: float = DEFAULT_FONT_SCALE

    #: When the mark is on screen, in seconds into the source media.
    start_seconds: float = 0.0
    end_seconds: float = 0.0

    created_at_utc: _dt.datetime = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    updated_at_utc: _dt.datetime = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    is_deleted: bool = False

    @field_validator("x1", "y1", "x2", "y2")
    @classmethod
    def _inside_the_frame(cls, value: float) -> float:
        """Clamped rather than rejected.

        A drag that ends a few pixels outside the video area is an ordinary thing to do
        with a mouse, and refusing to save it would lose the reviewer's work. A mark
        that is genuinely off-frame is still clamped to an edge, where it is visible and
        can be corrected, rather than being drawn somewhere nobody will look.
        """
        return min(1.0, max(0.0, float(value)))

    @field_validator("stroke", "font_scale")
    @classmethod
    def _sane_size(cls, value: float) -> float:
        return min(0.5, max(0.001, float(value)))

    @field_validator("colour")
    @classmethod
    def _looks_like_a_colour(cls, value: str) -> str:
        text = (value or "").strip()
        if not text.startswith("#") or len(text) not in (7, 9):
            return DEFAULT_COLOUR
        try:
            int(text[1:], 16)
        except ValueError:
            return DEFAULT_COLOUR
        return text.lower()

    # ------------------------------------------------------------------ #

    def covers(self, seconds: float) -> bool:
        """Is this mark on screen at ``seconds``?

        The window is inclusive at both ends. A mark whose start and end are the same
        would otherwise never be drawn, and that is exactly what a mark placed on a
        single frame looks like.
        """
        first, last = sorted((self.start_seconds, self.end_seconds))
        return first - 0.0005 <= seconds <= last + 0.0005

    def overlaps(self, start: float, end: float) -> bool:
        """Does this mark appear at any point in the span ``start``..``end``?"""
        first, last = sorted((self.start_seconds, self.end_seconds))
        return first <= end + 0.0005 and last >= start - 0.0005

    @property
    def summary(self) -> str:
        """A one-line description for a list, a menu or a provenance file."""
        if self.kind is AnnotationKind.TEXT:
            return f'Text "{self.text}"' if self.text else "Text (empty)"
        return f"{self.kind.label} “{self.text}”" if self.text else self.kind.label

    def moved_to(self, **changes: Any) -> Annotation:
        """A copy with changes applied and ``updated_at_utc`` moved strictly on.

        Strictly, because the wall clock is not fine enough to rely on. Windows
        resolves it to about 15 ms -- the reason entries carry a revision counter --
        and a measurement on this machine produced two thousand readings with a single
        distinct value between them.

        Two edits inside one tick would otherwise carry identical timestamps, and both
        the local merge and the server's take the newer of two copies with a strict
        comparison: the second edit would be judged no newer than the first and
        silently discarded, on its way to a colleague or on its way back.
        """
        data = self.model_dump()
        data.update(changes)
        now = _dt.datetime.now(_dt.UTC)
        if now <= self.updated_at_utc:
            now = self.updated_at_utc + _dt.timedelta(microseconds=1)
        data["updated_at_utc"] = now
        return Annotation.model_validate(data)


def visible_at(annotations: list[Annotation], seconds: float) -> list[Annotation]:
    """The marks on screen at ``seconds``, deleted ones left out."""
    return [a for a in annotations if not a.is_deleted and a.covers(seconds)]


def segments(annotations: list[Annotation], start: float, end: float) -> list[tuple[float, float, list[Annotation]]]:
    """Split ``start``..``end`` into spans over which the visible marks do not change.

    Burning annotations in means handing ffmpeg one image per span and telling it when
    to show each. Working the spans out from the times the marks come and go keeps that
    to the smallest number of images: an export where every mark is on for the whole
    clip needs exactly one, which is the common case.

    Spans with nothing visible are left out; there is nothing to draw over them.
    """
    live = [a for a in annotations if not a.is_deleted and a.overlaps(start, end)]
    if not live:
        return []

    boundaries = {start, end}
    for annotation in live:
        first, last = sorted((annotation.start_seconds, annotation.end_seconds))
        for moment in (first, last):
            if start < moment < end:
                boundaries.add(moment)

    ordered = sorted(boundaries)
    spans: list[tuple[float, float, list[Annotation]]] = []
    for left, right in pairwise(ordered):
        if right - left < 0.0005:
            continue
        middle = (left + right) / 2
        showing = [a for a in live if a.covers(middle)]
        if showing:
            spans.append((left, right, showing))
    return spans


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
#
# Qt is imported at module scope rather than inside each function. It is a hard
# dependency of the client, and threading the classes through as arguments -- which is
# what the alternative came to -- made the drawing code unreadable and put a closure
# over a loop variable in the middle of it.

#: Tried in order for annotation text. Named rather than left to ``QFont()`` so the
#: player and the burned-in export pick the same face: a label that is measured in one
#: font and drawn in another lands in the wrong place, and the two code paths run in
#: different processes often enough for that to happen quietly.
_FONT_PREFERENCES = ("Segoe UI", "Arial", "Helvetica", "DejaVu Sans", "Liberation Sans")
_resolved_family: str | None = None


def _font_family() -> str:
    """The best available face for annotation text, worked out once."""
    global _resolved_family
    if _resolved_family is not None:
        return _resolved_family

    available = set(QFontDatabase.families())
    for family in _FONT_PREFERENCES:
        if family in available:
            _resolved_family = family
            break
    else:
        # No preferred face, and possibly no fonts at all -- a headless environment with
        # no font configuration reports none. Fall back to whatever Qt will give us
        # rather than failing: the shapes are still worth drawing.
        _resolved_family = next(iter(sorted(available)), "")
        if not available:
            log.warning(
                "No font families are available, so annotation text cannot be drawn "
                "legibly. Shapes are unaffected."
            )
    return _resolved_family


def render(
    annotations: list[Annotation],
    width: int,
    height: int,
    *,
    selected_id: str | None = None,
) -> QImage:
    """Draw the marks onto a transparent image of ``width`` x ``height``.

    This is the only place annotations are drawn. The player composites what comes back
    through mpv, the export writes it to a PNG for ffmpeg to burn in, and the editor
    shows it over a still frame -- so all three agree by construction rather than by
    three sets of drawing code being kept in step.

    ``selected_id`` draws handles around one mark. Only the editor passes it; nothing
    that produces a file ever does.
    """
    image = QImage(
        max(1, int(width)), max(1, int(height)), QImage.Format.Format_ARGB32_Premultiplied
    )
    image.fill(Qt.GlobalColor.transparent)
    if not annotations:
        return image

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    try:
        for annotation in annotations:
            if annotation.is_deleted:
                continue
            _draw_one(painter, annotation, image, width, height)
            if selected_id and annotation.annotation_id == selected_id:
                _draw_handles(painter, annotation, width, height)
    finally:
        painter.end()
    return image


def _draw_one(
    painter: QPainter, annotation: Annotation, image: QImage, width: int, height: int
) -> None:
    colour = QColor(annotation.colour)
    thickness = max(1.0, annotation.stroke * height)
    start = QPointF(annotation.x1 * width, annotation.y1 * height)
    finish = QPointF(annotation.x2 * width, annotation.y2 * height)
    box = QRectF(start, finish).normalized()

    # Everything is drawn twice: once in a dark halo a little wider, then in the chosen
    # colour. Footage is not a blank page -- a yellow circle on a cream wall or a white
    # arrow on an overcast sky is invisible without it.
    passes = ((QColor(0, 0, 0, 170), thickness * 1.1), (colour, 0.0))

    for pen_colour, extra in passes:
        pen = QPen(pen_colour)
        pen.setWidthF(thickness + extra)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if annotation.kind is AnnotationKind.ELLIPSE:
            painter.drawEllipse(box)
        elif annotation.kind is AnnotationKind.RECTANGLE:
            painter.drawRect(box)
        elif annotation.kind is AnnotationKind.LINE:
            painter.drawLine(start, finish)
        elif annotation.kind is AnnotationKind.ARROW:
            _draw_arrow(painter, start, finish, thickness + extra, pen_colour)

    if annotation.kind is AnnotationKind.TEXT or annotation.text:
        _draw_text(painter, annotation, image, start, box, colour, height)


def _draw_arrow(
    painter: QPainter, start: QPointF, finish: QPointF, thickness: float, colour: QColor
) -> None:
    """A line with a solid head, sized from the stroke so it stays in proportion."""
    dx, dy = finish.x() - start.x(), finish.y() - start.y()
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    head = max(thickness * 3.2, length * 0.16)
    ux, uy = dx / length, dy / length

    # Stop the shaft short of the head so the point stays sharp rather than being
    # blunted by the line's own round cap poking through it.
    painter.drawLine(
        start, QPointF(finish.x() - ux * head * 0.75, finish.y() - uy * head * 0.75)
    )

    spread = head * 0.42
    left = QPointF(finish.x() - ux * head - uy * spread, finish.y() - uy * head + ux * spread)
    right = QPointF(finish.x() - ux * head + uy * spread, finish.y() - uy * head - ux * spread)
    painter.setBrush(colour)
    painter.setPen(QPen(colour, 0))
    painter.drawPolygon(QPolygonF([finish, left, right]))
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _draw_text(
    painter: QPainter,
    annotation: Annotation,
    image: QImage,
    start: QPointF,
    box: QRectF,
    colour: QColor,
    height: int,
) -> None:
    """Text on a dark plate, so it is readable over whatever is behind it."""
    if not annotation.text:
        return
    pixel_size = max(6.0, annotation.font_scale * height)
    font = QFont(_font_family())
    font.setPixelSize(round(pixel_size))
    font.setBold(True)
    painter.setFont(font)

    metrics = QFontMetricsF(font)
    lines = annotation.text.splitlines() or [annotation.text]
    line_height = metrics.height()
    text_width = max(metrics.horizontalAdvance(line) for line in lines)
    padding = pixel_size * 0.3

    # A label attached to a shape sits just above it; a standalone text mark sits where
    # it was placed.
    if annotation.kind is AnnotationKind.TEXT:
        anchor = QPointF(start.x(), start.y())
    else:
        anchor = QPointF(box.left(), box.top() - line_height * len(lines) - padding * 2)

    plate = QRectF(
        anchor.x(), anchor.y(),
        text_width + padding * 2, line_height * len(lines) + padding * 2,
    )
    # Keep the plate on the picture: a label placed near an edge would otherwise be
    # drawn half outside it and lost from the export.
    plate.moveLeft(min(max(0.0, plate.left()), max(0.0, image.width() - plate.width())))
    plate.moveTop(min(max(0.0, plate.top()), max(0.0, image.height() - plate.height())))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 165))
    painter.drawRoundedRect(plate, padding * 0.6, padding * 0.6)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(colour))
    for index, line in enumerate(lines):
        baseline = plate.top() + padding + metrics.ascent() + index * line_height
        painter.drawText(QPointF(plate.left() + padding, baseline), line)


def _draw_handles(
    painter: QPainter, annotation: Annotation, width: int, height: int
) -> None:
    """Corner marks on the selected annotation. Editor only -- never exported."""
    thickness = max(1.0, annotation.stroke * height)
    size = max(4.0, thickness * 1.6)
    start = QPointF(annotation.x1 * width, annotation.y1 * height)
    finish = QPointF(annotation.x2 * width, annotation.y2 * height)
    box = QRectF(start, finish).normalized()

    painter.setPen(QPen(QColor(255, 255, 255, 230), max(1.0, thickness * 0.4)))
    painter.setBrush(QColor(0, 0, 0, 160))
    points = (
        [start]
        if annotation.kind is AnnotationKind.TEXT
        else [box.topLeft(), box.topRight(), box.bottomLeft(), box.bottomRight()]
    )
    for handle in points:
        painter.drawRect(QRectF(handle.x() - size, handle.y() - size, size * 2, size * 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
