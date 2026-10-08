"""Small icons drawn in code.

Painted rather than taken from a font. A glyph like U+1F441 lands on whatever emoji
font the machine happens to have, which on Windows means a colour bitmap that ignores
the theme and sits at the wrong weight next to the table's text. Drawing it means one
appearance everywhere, at any row height, in whatever colour the theme is using.

Cached by (colour, size), because a table model asks for its decoration once per
visible row on every repaint.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

#: Keyed by every argument that changes the result.
_CACHE: dict[tuple[str, str, int, int], QIcon] = {}


def _pixmap(size: int, ratio: int) -> QPixmap:
    """A transparent pixmap at the device ratio, so the drawing is not blurry."""
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(float(ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap


def eye_icon(colour: str, size: int = 16, ratio: int = 2) -> QIcon:
    """An open eye: an almond outline with a filled pupil.

    Two symmetrical curves rather than an ellipse. An ellipse with a dot in it reads
    as a target or a radio button; the pointed corners are what make it an eye.
    """
    key = ("eye", colour, size, ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pixmap = _pixmap(size, ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    pen = QPen(QColor(colour))
    pen.setWidthF(size / 11.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    inset = size * 0.08
    left = inset
    right = size - inset
    middle = size / 2.0
    lid = size * 0.30  # how far the curves bow from the centre line

    path = QPainterPath(QPointF(left, middle))
    path.quadTo(QPointF(middle, middle - lid * 2), QPointF(right, middle))
    path.quadTo(QPointF(middle, middle + lid * 2), QPointF(left, middle))
    painter.drawPath(path)

    pupil = size * 0.30
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(colour))
    painter.drawEllipse(QRectF(middle - pupil / 2, middle - pupil / 2, pupil, pupil))
    painter.end()

    icon = QIcon(pixmap)
    _CACHE[key] = icon
    return icon


def speaker_icon(colour: str, size: int = 16, ratio: int = 2, *, muted: bool = False) -> QIcon:
    """A speaker cone with two arcs, or a cone with a slash when muted.

    The one glyph everybody reads as "volume". It is drawn rather than taken from the
    font for the reason at the top of this module, which bites hardest here: every
    speaker character on Windows is in Segoe UI Emoji, so U+1F50A arrives as a colour
    bitmap -- teal, and the muted one bright red -- sitting among a row of monochrome
    theme-coloured controls.
    """
    key = (f"speaker{'-muted' if muted else ''}", colour, size, ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pixmap = _pixmap(size, ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    tint = QColor(colour)
    stroke = size / 11.0

    # The cone: a small rectangle at the left opening out into a trapezium.
    body = QPainterPath()
    body.moveTo(QPointF(size * 0.08, size * 0.38))
    body.lineTo(QPointF(size * 0.28, size * 0.38))
    body.lineTo(QPointF(size * 0.52, size * 0.16))
    body.lineTo(QPointF(size * 0.52, size * 0.84))
    body.lineTo(QPointF(size * 0.28, size * 0.62))
    body.lineTo(QPointF(size * 0.08, size * 0.62))
    body.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(tint)
    painter.drawPath(body)

    pen = QPen(tint)
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if muted:
        # A slash, drawn across the waves rather than the cone so the cone stays
        # readable as a speaker at 16px.
        painter.drawLine(QPointF(size * 0.62, size * 0.30), QPointF(size * 0.92, size * 0.70))
        painter.drawLine(QPointF(size * 0.92, size * 0.30), QPointF(size * 0.62, size * 0.70))
    else:
        for radius in (size * 0.20, size * 0.34):
            box = QRectF(size * 0.46 - radius, size * 0.5 - radius, radius * 2, radius * 2)
            # Qt takes angles in sixteenths of a degree; this opens to the right.
            painter.drawArc(box, -55 * 16, 110 * 16)
    painter.end()

    icon = QIcon(pixmap)
    _CACHE[key] = icon
    return icon


def pencil_icon(colour: str, size: int = 16, ratio: int = 2) -> QIcon:
    """A pencil on the diagonal: the one shape that reads as "draw on this".

    Drawn, not a glyph, for the reason at the top of this module. U+270E is in Segoe UI
    Emoji on Windows and arrives as a colour bitmap, which beside a row of monochrome
    theme-coloured controls looks like a mistake rather than a tool.
    """
    key = ("pencil", colour, size, ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pixmap = _pixmap(size, ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    tint = QColor(colour)

    # The barrel, corner to corner, with the tip at the bottom left.
    barrel = QPainterPath()
    barrel.moveTo(QPointF(size * 0.78, size * 0.10))
    barrel.lineTo(QPointF(size * 0.92, size * 0.24))
    barrel.lineTo(QPointF(size * 0.40, size * 0.76))
    barrel.lineTo(QPointF(size * 0.24, size * 0.60))
    barrel.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(tint)
    painter.drawPath(barrel)

    # The sharpened tip, left unfilled so it reads as a point rather than a block.
    tip = QPainterPath()
    tip.moveTo(QPointF(size * 0.22, size * 0.62))
    tip.lineTo(QPointF(size * 0.38, size * 0.78))
    tip.lineTo(QPointF(size * 0.14, size * 0.86))
    tip.closeSubpath()
    painter.drawPath(tip)
    painter.end()

    icon = QIcon(pixmap)
    _CACHE[key] = icon
    return icon
