"""Work out what we can about a media file so the LOG modal opens mostly filled in.

Where the event sits in the media comes from the player, not from here. What this
module contributes is the Area guess, which is deliberately conservative: an empty
field the reviewer fills in is better than a wrong value written into an evidence
record.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Recognising timestamps in names
# --------------------------------------------------------------------------- #
#
# Used only to reject date-named folders when guessing an Area: "2025-09-14" is a
# folder full of recordings, not a room.

_YEAR = r"(?:19|20)\d{2}"
_MONTH = r"(?:0[1-9]|1[0-2])"
_DAY = r"(?:0[1-9]|[12]\d|3[01])"
_HOUR = r"(?:[01]\d|2[0-3])"
_MINSEC = r"(?:[0-5]\d)"

_TIMESTAMP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 2025-09-14T21:30:45, 2025-09-14 21-30-45, 2025_09_14__21.30.45
    re.compile(rf"{_YEAR}[-_.]{_MONTH}[-_.]{_DAY}[T\s_\-]+{_HOUR}[-_.:]{_MINSEC}[-_.:]{_MINSEC}"),
    # 20250914_213045, 20250914-213045, 20250914213045
    re.compile(rf"(?<!\d){_YEAR}{_MONTH}{_DAY}[T_\-\s]?{_HOUR}{_MINSEC}{_MINSEC}(?!\d)"),
    # Bare dates: 2025-09-14 or 20250914
    re.compile(rf"{_YEAR}[-_.]{_MONTH}[-_.]{_DAY}"),
    re.compile(rf"(?<!\d){_YEAR}{_MONTH}{_DAY}(?!\d)"),
)


def looks_like_a_timestamp(name: str) -> bool:
    """True when a path component is mostly a date or date-time stamp."""
    return any(pattern.search(name) for pattern in _TIMESTAMP_PATTERNS)


# --------------------------------------------------------------------------- #
# Area guessing
# --------------------------------------------------------------------------- #

#: Folder names that describe storage, not a physical area worth prefilling.
_GENERIC_FOLDERS = frozenset(
    {
        "video",
        "videos",
        "audio",
        "images",
        "image",
        "photos",
        "photo",
        "pictures",
        "media",
        "footage",
        "evidence",
        "files",
        "data",
        "clips",
        "recordings",
        "dcim",
        "export",
        "exports",
        "import",
        "new folder",
        "downloads",
        "documents",
        "desktop",
        "temp",
        "tmp",
        "raw",
        "unsorted",
        "misc",
        "case",
        "cases",
        "review",
    }
)

#: Tokens stripped out of a filename before guessing an area from it. The trailing
#: ``\d*`` matters: recorders write "cam1" and "ch02" as single tokens, and a plain
#: word boundary would not match them.
#:
#: "front", "main", "day" and "night" are deliberately absent. They are ordinary
#: room words, and stripping them turned "Front Room" into "Room".
_NOISE_TOKENS = re.compile(
    r"(?ix)"
    r"\b(?:cam|camera|ch|chn|channel|dvr|nvr|rec|recording|clip|vid|video|audio|img|image|"
    r"part|seg|segment|file|evp|sub|stream|hd|sd|fhd|uhd|4k|1080p?|"
    r"720p?|480p?|60fps|30fps|mov|avi|mp4|mkv|wav|mp3)\d*\b"
)

_SEPARATORS = re.compile(r"[_\-.\s]+")


def _titleise(token: str) -> str:
    return " ".join(word.capitalize() if word.islower() else word for word in token.split())


def guess_area(path: str | Path) -> str:
    """Best-effort physical area from the folder name, falling back to the filename.

    Returns an empty string rather than a bad guess; the field is user-editable and
    a wrong prefill is worse than none.
    """
    path = Path(path)

    parent = path.parent.name.strip()
    if parent and parent.lower() not in _GENERIC_FOLDERS and not looks_like_a_timestamp(parent):
        cleaned = _SEPARATORS.sub(" ", parent).strip()
        cleaned = _NOISE_TOKENS.sub("", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")
        # Reject anything that is mostly digits, e.g. a serial or a date folder.
        if sum(char.isalpha() for char in cleaned) >= 3:
            return _titleise(cleaned)

    stem = _SEPARATORS.sub(" ", path.stem)
    stem = re.sub(r"\d{6,}", " ", stem)
    stem = _NOISE_TOKENS.sub("", stem)
    stem = re.sub(r"\b\d{1,4}\b", " ", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip(" -_")
    if sum(char.isalpha() for char in stem) >= 3:
        return _titleise(stem)

    return ""


# --------------------------------------------------------------------------- #
# Bundle used by the LOG modal
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MediaContext:
    """Everything the LOG modal needs to know about the file under review."""

    path: Path
    kind: str
    duration_seconds: float | None = None
    sha256: str | None = None

    @property
    def file_name(self) -> str:
        return self.path.name

    @property
    def folder(self) -> str:
        return str(self.path.parent)
