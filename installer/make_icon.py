"""Generate the application icon.

Kept as a script rather than a committed binary so the icon is reviewable and
regenerable. Run it whenever the mark changes:

    python installer/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "src" / "evidence_review" / "resources" / "app.ico"

BACKGROUND = (22, 24, 27, 255)
FRAME = (52, 58, 65, 255)
ACCENT = (229, 72, 77, 255)
LIGHT = (230, 232, 234, 255)

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> Image.Image:
    """Draw at 4x and downsample, which gives clean edges at every size."""
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Rounded dark plate.
    inset = canvas * 0.04
    radius = canvas * 0.22
    draw.rounded_rectangle(
        [inset, inset, canvas - inset, canvas - inset],
        radius=radius,
        fill=BACKGROUND,
        outline=FRAME,
        width=max(int(canvas * 0.02), 1),
    )

    # Play triangle, offset left to leave room for the log dot.
    cx, cy = canvas * 0.44, canvas * 0.5
    arm = canvas * 0.19
    draw.polygon(
        [
            (cx - arm * 0.75, cy - arm),
            (cx - arm * 0.75, cy + arm),
            (cx + arm * 0.95, cy),
        ],
        fill=LIGHT,
    )

    # The LOG marker: a record dot in the accent colour.
    dot_r = canvas * 0.115
    dot_cx, dot_cy = canvas * 0.73, canvas * 0.71
    draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=ACCENT,
    )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frames = [render(size) for size in SIZES]
    frames[-1].save(TARGET, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Wrote {TARGET} ({TARGET.stat().st_size} bytes, sizes {list(SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
