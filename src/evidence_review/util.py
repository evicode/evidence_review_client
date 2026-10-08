"""Small shared helpers: timecodes, datetimes, paths, media classification."""

from __future__ import annotations

import datetime as _dt
import os
import re
import socket
import unicodedata
from pathlib import Path

# --------------------------------------------------------------------------- #
# Media classification
# --------------------------------------------------------------------------- #

VIDEO_EXTENSIONS = frozenset(
    """
    .mp4 .m4v .mov .mkv .avi .wmv .flv .f4v .webm .mpg .mpeg .mpe .m2v .m2ts .mts .ts
    .vob .3gp .3g2 .asf .rm .rmvb .ogv .divx .dv .mxf .h264 .hevc .264 .265 .dav .264dav
    """.split()
)

AUDIO_EXTENSIONS = frozenset(
    """
    .mp3 .wav .wma .m4a .aac .flac .ogg .oga .opus .aiff .aif .aifc .ape .wv .mpc
    .amr .au .ra .dts .ac3 .m4b .caf .voc .gsm .spx
    """.split()
)

IMAGE_EXTENSIONS = frozenset(
    """
    .jpg .jpeg .jpe .png .gif .bmp .tif .tiff .webp .heic .heif .avif .ico .jfif
    .ppm .pgm .pbm .tga .dds .exr
    """.split()
)

ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS


def classify_media(path: str | os.PathLike[str]) -> str | None:
    """Return ``"video"``, ``"audio"``, ``"image"`` or ``None`` for an unknown type."""
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


# --------------------------------------------------------------------------- #
# Timecodes
# --------------------------------------------------------------------------- #

_TIMECODE_RE = re.compile(
    r"^\s*(?:(?P<h>\d+):)?(?P<m>[0-5]?\d):(?P<s>[0-5]?\d)(?:[.,](?P<ms>\d{1,3}))?\s*$"
)
_BARE_SECONDS_RE = re.compile(r"^\s*(?P<s>\d+(?:[.,]\d+)?)\s*$")


#: Shown where a record genuinely has no value for a field - a still image has no
#: event timestamp, an entry may have no duration. Distinct from ``--:--:--``,
#: which is a *timecode with no reading yet*: a clock before a file is loaded,
#: shaped like the value it will hold. The two were used interchangeably and all
#: three spellings could be on screen at once for the same still image.
NO_VALUE = "—"


def format_timecode(seconds: float | None, *, millis: bool = True) -> str:
    """Format seconds as ``HH:MM:SS.mmm``. Returns ``"--:--:--"`` for ``None``.

    That placeholder is deliberate and is not :data:`NO_VALUE`: it means "no
    reading", as on an unlit clock, and it keeps the monospace width of the
    timecode it stands in for.
    """
    if seconds is None:
        return "--:--:--"
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if millis:
        ms = round((seconds - whole) * 1000)
        # Guard against 999.6ms rounding up into a whole second.
        if ms >= 1000:
            ms = 0
            secs += 1
            if secs == 60:
                secs = 0
                minutes += 1
                if minutes == 60:
                    minutes = 0
                    hours += 1
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_timecode(text: str) -> float | None:
    """Parse ``HH:MM:SS.mmm``, ``MM:SS``, or a bare number of seconds."""
    if not text:
        return None
    match = _TIMECODE_RE.match(text)
    if match:
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m"))
        secs = int(match.group("s"))
        ms_text = match.group("ms") or ""
        ms = int(ms_text.ljust(3, "0")) if ms_text else 0
        return hours * 3600 + minutes * 60 + secs + ms / 1000.0
    match = _BARE_SECONDS_RE.match(text)
    if match:
        return float(match.group("s").replace(",", "."))
    return None


# --------------------------------------------------------------------------- #
# Datetimes
# --------------------------------------------------------------------------- #


def utc_now() -> _dt.datetime:
    """Timezone-aware current UTC time."""
    return _dt.datetime.now(_dt.UTC)


def to_iso(value: _dt.datetime | _dt.date | None) -> str | None:
    """Serialise a date/datetime for storage. Naive datetimes are assumed local."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        return value.isoformat()
    return value.isoformat()


def from_iso_datetime(text: str | None) -> _dt.datetime | None:
    """Parse an ISO 8601 datetime, tolerating a trailing ``Z``."""
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def from_iso_date(text: str | None) -> _dt.date | None:
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def ensure_aware(value: _dt.datetime | None) -> _dt.datetime | None:
    """Attach the local timezone to a naive datetime."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def machine_name() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - defensive
        return "unknown"


def os_user_display_name() -> str:
    """Best guess at the human's name, for prefilling Investigator Name."""
    if os.name == "nt":
        try:
            import ctypes

            size = ctypes.c_ulong(256)
            buffer = ctypes.create_unicode_buffer(size.value)
            # NameDisplay == 3 in the EXTENDED_NAME_FORMAT enum.
            if ctypes.windll.secur32.GetUserNameExW(3, buffer, ctypes.byref(size)):
                name = buffer.value.strip()
                if name:
                    return name
        except Exception:  # noqa: BLE001 - any failure falls through to the env var
            pass
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def safe_filename(text: str, *, fallback: str = "untitled", max_length: int = 120) -> str:
    """Reduce arbitrary text to something safe for a Windows filename."""
    normalised = unicodedata.normalize("NFKD", text)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalised).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:max_length] or fallback).strip() or fallback


#: Case identifiers end up in URLs, filesystem paths and database keys, so they
#: are restricted to a conservative character set at the point they are created.
_SLUG_STRIP = re.compile(r"[^A-Za-z0-9._-]+")
#: Separators that ended up adjacent. " - " strips to "-" plus the hyphen that was
#: already there, so without this "Willow House - October" became
#: "willow-house---october", which then became a folder name and a filename.
_SLUG_RUNS = re.compile(r"-{2,}")


def slugify_case_id(text: str, *, fallback: str = "default", max_length: int = 120) -> str:
    """Turn a human case name into a safe identifier.

    Accents are folded, runs of anything else collapse to a single hyphen. The
    result matches what the server will accept, so a case named "Willow House
    (2025)" cannot produce entries the API rejects.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RUNS.sub("-", _SLUG_STRIP.sub("-", folded)).strip("-._").lower()
    return slug[:max_length].strip("-._") or fallback


def humanise_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"  # pragma: no cover
