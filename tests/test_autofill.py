"""Autofill and the timecode utilities.

The event's position in the media comes from the player, so what is left here is
the Area guess -- where a wrong answer is worse than no answer, because it gets
written into an evidence record -- and the timecode formatting the whole UI reads.
"""

from __future__ import annotations

import pytest

from evidence_review.autofill import guess_area, looks_like_a_timestamp
from evidence_review.util import classify_media, format_timecode, parse_timecode

# --------------------------------------------------------------------------- #
# Area guessing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:/case/Basement/cam2_20250914.mp4", "Basement"),
        ("C:/case/Master Bedroom/clip.mp4", "Master Bedroom"),
        ("C:/case/videos/Attic_Stairs_cam1.mp4", "Attic Stairs"),
        # Ordinary room words that were once stripped as recorder noise.
        ("C:/case/Front Room/clip.mp4", "Front Room"),
        ("C:/case/Main Hall/clip.mp4", "Main Hall"),
        ("C:/case/Night Nursery/clip.mp4", "Night Nursery"),
        ("C:/case/Day Room/clip.mp4", "Day Room"),
    ],
)
def test_guesses_a_plausible_area(path: str, expected: str) -> None:
    assert guess_area(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "C:/case/videos/20250914_213045.mp4",
        "C:/case/2025-09-14/ch01_00000.dav",
        "C:/DCIM/IMG_1234.jpg",
        "C:/case/recordings/0001.mp4",
    ],
)
def test_returns_nothing_rather_than_a_bad_guess(path: str) -> None:
    """A wrong prefill is worse than an empty field the reviewer must fill in."""
    assert guess_area(path) == ""


def test_camera_tokens_with_digits_are_stripped() -> None:
    """Recorders write "cam1" and "ch02" as one token, which a plain word
    boundary would not match."""
    assert guess_area("C:/case/Attic Stairs/Attic_Stairs_cam1.mp4") == "Attic Stairs"


# --------------------------------------------------------------------------- #
# Date-named folders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "2025-09-14",
        "20250914",
        "20250914_213045",
        "2025-09-14 21-30-45",
        "VID_20250914_213045.mp4",
    ],
)
def test_recognises_date_named_components(name: str) -> None:
    assert looks_like_a_timestamp(name)


@pytest.mark.parametrize(
    "name", ["Basement", "Front Room", "clip.mp4", "chapter_12", "v1.2.3", "Room 2025"]
)
def test_does_not_mistake_room_names_for_dates(name: str) -> None:
    assert not looks_like_a_timestamp(name)


# --------------------------------------------------------------------------- #
# Timecodes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00.000"),
        (83.4, "00:01:23.400"),
        (3661.5, "01:01:01.500"),
        (59.9996, "00:01:00.000"),  # rounding must carry, not produce :60
    ],
)
def test_format_timecode(seconds: float, expected: str) -> None:
    assert format_timecode(seconds) == expected


def test_format_timecode_handles_none() -> None:
    assert format_timecode(None) == "--:--:--"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:01:23.400", 83.4),
        ("1:23", 83.0),
        ("01:01:01", 3661.0),
        ("83.4", 83.4),
        ("00:00:00,250", 0.25),
    ],
)
def test_parse_timecode(text: str, expected: float) -> None:
    assert parse_timecode(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["nonsense", "", "99:99:99", "1:2:3:4", "1:2x:03"])
def test_parse_timecode_rejects_garbage(text: str) -> None:
    assert parse_timecode(text) is None


def test_timecode_round_trips() -> None:
    assert parse_timecode(format_timecode(1234.567)) == pytest.approx(1234.567, abs=0.001)


# --------------------------------------------------------------------------- #
# Media classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("a.mp4", "video"),
        ("a.mkv", "video"),
        ("a.dav", "video"),
        ("a.wav", "audio"),
        ("a.flac", "audio"),
        ("a.jpg", "image"),
        ("a.heic", "image"),
        ("a.txt", None),
    ],
)
def test_media_classification(name: str, kind: str | None) -> None:
    assert classify_media(name) == kind
