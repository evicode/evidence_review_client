"""Reading the shape of a recording, so a reviewer can see where the sound is.

EVP review is hours of near-silence with a two-second whisper somewhere in it. Scrubbing
a seek bar blind is the wrong tool for that; a waveform is how you find the places worth
listening to.

Three decisions shape this, and all three were measured rather than assumed:

**Decoded at 22 kHz, not 8.** A cheap decode low-passes, and a 6 kHz whisper decoded at
8 kHz stands out only five times above the noise floor instead of seventeen. The quiet
high-frequency event is precisely what this view exists to reveal, so the rate has to
keep it. 22 kHz costs about 60% more than 8 kHz and loses nothing that matters; 44 kHz
costs more again and measured no better.

**One pass, then cached.** A three-hour recording takes around twenty seconds to read,
which is far too long to do on every open and perfectly acceptable once. The cache is
keyed on size and modification time, like the media hash cache, so a re-encoded or
replaced file is read again rather than trusted.

**An overview plus detail on demand.** The stored envelope is 100 buckets a second --
10 ms, 4 MB for three hours. That is enough to navigate and to zoom a long way. Below
about a second on screen it would start to look like stairs, so that span is re-read
from the file at the resolution actually being drawn, which is quick because the span is
short.
"""

from __future__ import annotations

import array
import hashlib
import logging
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .clip import _NO_WINDOW, find_ffmpeg, find_ffprobe

log = logging.getLogger(__name__)

#: Decode rate. See the module docstring: low enough to be quick, high enough that a
#: quiet high-frequency event still shows up in the envelope.
DECODE_RATE = 22050

#: Buckets per second in the stored envelope. 10 ms resolution.
PEAKS_PER_SECOND = 100

#: Below this many seconds on screen, the stored envelope is coarser than the pixels
#: being drawn, so the visible span is re-read from the file instead.
DETAIL_THRESHOLD_SECONDS = 1.5

_MAGIC = b"EVRWAVE1"


class WaveformError(RuntimeError):
    """Raised when a recording's shape cannot be read. The message is shown."""


@dataclass(frozen=True, slots=True)
class Waveform:
    """The envelope of a recording: one min/max pair per bucket.

    ``peaks`` is a flat array of int16, two entries per bucket. Flat rather than a list
    of pairs because a three-hour recording is a million buckets, and the difference
    between one array and a million tuples is the difference between four megabytes and
    a hundred.
    """

    peaks: array.array
    peaks_per_second: int
    duration: float
    #: The largest absolute sample seen. Nothing in EVP is recorded near full scale, so
    #: the view normalises against this rather than against the format's range -- a
    #: waveform drawn against 32767 is a flat line and tells the reviewer nothing.
    peak_amplitude: int

    @property
    def bucket_count(self) -> int:
        return len(self.peaks) // 2

    def window(self, start: float, end: float, columns: int) -> list[tuple[int, int]]:
        """Min/max per column for the span ``start``..``end``, ready to draw.

        Several buckets usually fall in one column, so this takes the extreme of each
        -- never an average. Averaging is how a waveform view loses the one-frame click
        that somebody is looking for.
        """
        if columns <= 0 or self.bucket_count == 0:
            return []
        start = max(0.0, start)
        end = max(start + 1e-6, end)

        first = start * self.peaks_per_second
        last = end * self.peaks_per_second
        step = (last - first) / columns

        out: list[tuple[int, int]] = []
        peaks = self.peaks
        total = self.bucket_count
        for column in range(columns):
            lo_index = int(first + column * step)
            hi_index = int(first + (column + 1) * step)
            if hi_index <= lo_index:
                hi_index = lo_index + 1
            lo_index = max(0, min(total, lo_index))
            hi_index = max(0, min(total, hi_index))
            if lo_index >= hi_index:
                out.append((0, 0))
                continue
            lowest = 0
            highest = 0
            for bucket in range(lo_index, hi_index):
                low = peaks[bucket * 2]
                high = peaks[bucket * 2 + 1]
                if low < lowest:
                    lowest = low
                if high > highest:
                    highest = high
            out.append((lowest, highest))
        return out

    def noise_floor(self) -> int:
        """The level the recording sits at when nothing is happening.

        The median rather than the minimum: a recording with a few seconds of true
        digital silence at the top would otherwise report a floor of zero and make
        every faint hiss look like an event.
        """
        if self.bucket_count == 0:
            return 0
        levels = sorted(
            max(abs(self.peaks[b * 2]), abs(self.peaks[b * 2 + 1]))
            for b in range(self.bucket_count)
        )
        return levels[len(levels) // 2]

    def loudest_moments(
        self, count: int = 20, apart: float = 2.0, above_floor: float = 4.0
    ) -> list[float]:
        """Times worth listening to, loudest first, well separated.

        What somebody wants on opening three hours of near-silence. ``apart`` stops one
        long noise filling the list.

        Only moments at least ``above_floor`` times the noise floor are returned, and
        the list is short rather than padded. Filling it to ``count`` regardless sent
        the reviewer to empty air -- on a quiet recording half the list was silence,
        which is worse than an empty list because it looks like an answer.
        """
        if self.bucket_count == 0:
            return []
        peaks = self.peaks
        floor = self.noise_floor()
        # A floor of zero means real digital silence; anything audible then counts.
        threshold = max(floor * above_floor, 1.0)

        candidates = [
            bucket
            for bucket in range(self.bucket_count)
            if max(abs(peaks[bucket * 2]), abs(peaks[bucket * 2 + 1])) >= threshold
        ]
        candidates.sort(key=lambda b: max(abs(peaks[b * 2]), abs(peaks[b * 2 + 1])), reverse=True)

        chosen: list[float] = []
        for bucket in candidates:
            when = bucket / self.peaks_per_second
            if all(abs(when - already) >= apart for already in chosen):
                chosen.append(when)
                if len(chosen) >= count:
                    break
        return sorted(chosen)


# --------------------------------------------------------------------------- #
# Reading it out of a file
# --------------------------------------------------------------------------- #


def audio_duration(path: Path) -> float | None:
    """How long the recording runs, or None if it cannot be read."""
    ffprobe = find_ffprobe()
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float((result.stdout or "").strip())
    except ValueError:
        return None


def extract_peaks(
    path: Path,
    *,
    peaks_per_second: int = PEAKS_PER_SECOND,
    start: float | None = None,
    duration: float | None = None,
    audio_filters: str = "",
    progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Waveform:
    """Read the envelope of ``path``, or of one span of it.

    ``progress`` is called with a fraction as the read proceeds, and ``should_stop``
    is checked between chunks so opening another file can abandon this one rather than
    making the reviewer wait for a recording they have already moved on from.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise WaveformError(
            "ffmpeg could not be found, so the waveform cannot be read.\n\n"
            "The installer normally places it beside the application."
        )
    if not path.is_file():
        raise WaveformError(f"The recording is not where the entry says it is:\n\n{path}")

    total_seconds = duration if duration is not None else audio_duration(path)
    if not total_seconds or total_seconds <= 0:
        raise WaveformError(
            f"{path.name} does not report a length, so there is nothing to draw.\n\n"
            "It may not be an audio file, or it may be truncated."
        )

    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start is not None:
        command += ["-ss", f"{max(0.0, start):.3f}"]
    command += ["-i", str(path)]
    if duration is not None:
        command += ["-t", f"{duration:.3f}"]
    if audio_filters:
        command += ["-af", audio_filters]
    command += [
        # Mono: the overview is for navigating, and a stereo split is a different view
        # rather than a different scale on this one.
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(DECODE_RATE),
        "-",
    ]

    bucket = max(1, DECODE_RATE // max(1, peaks_per_second))
    expected = max(1, int(total_seconds * DECODE_RATE) * 2)

    peaks = array.array("h")
    loudest = 0
    read_bytes = 0

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise WaveformError(f"ffmpeg could not be started.\n\n{exc}") from exc

    try:
        leftover = b""
        while True:
            if should_stop is not None and should_stop():
                process.kill()
                raise WaveformCancelled
            chunk = process.stdout.read(1 << 20)
            if not chunk:
                break
            read_bytes += len(chunk)
            if leftover:
                chunk = leftover + chunk
            usable = len(chunk) - (len(chunk) % 2)
            leftover = chunk[usable:]

            samples = array.array("h")
            samples.frombytes(chunk[:usable])
            for index in range(0, len(samples) - bucket + 1, bucket):
                window = samples[index : index + bucket]
                low = min(window)
                high = max(window)
                peaks.append(low)
                peaks.append(high)
                if -low > loudest:
                    loudest = -low
                if high > loudest:
                    loudest = high

            if progress is not None:
                progress(min(0.99, read_bytes / expected))
        process.wait(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on an abandoned read
            process.kill()

    if process.returncode not in (0, None) and not peaks:
        complaint = (process.stderr.read() or b"").decode("utf-8", "replace").strip()
        raise WaveformError(
            f"The waveform could not be read from {path.name}.\n\n{complaint[-400:]}"
        )
    if not peaks:
        raise WaveformError(f"{path.name} decoded to no audio at all.")

    if progress is not None:
        progress(1.0)

    return Waveform(
        peaks=peaks,
        peaks_per_second=peaks_per_second,
        duration=total_seconds,
        peak_amplitude=max(1, loudest),
    )


class WaveformCancelled(Exception):
    """Raised when a read is abandoned because the reviewer moved on."""


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


def cache_key(path: Path, audio_filters: str = "") -> str | None:
    """Identity of a file for caching, from its size and modification time.

    The same scheme the media hash cache uses, and for the same reason: hashing a
    gigabyte to decide whether to re-read it would cost more than re-reading it.

    The path is digested with blake2b and **not** with the built-in ``hash()``. String
    hashing is randomised per process, so a key built with it differs on every launch:
    the cache could never hit after a restart, every open would re-read the whole
    recording, and the directory would fill with copies nothing would ever read again.
    Nothing failed, which is why it survived a suite that only ever reopened a file
    inside one process.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    digest = hashlib.blake2b(
        str(path.resolve()).lower().encode("utf-8", "replace"), digest_size=8
    ).hexdigest()
    key = f"{digest}_{stat.st_size}_{stat.st_mtime_ns}"
    if audio_filters:
        # A cleaned envelope is a different picture of the same file, so it is cached
        # separately rather than overwriting the recording's own.
        chain = hashlib.blake2b(audio_filters.encode("utf-8", "replace"), digest_size=4).hexdigest()
        key = f"{key}_{chain}"
    return key


def cache_path(directory: Path, path: Path, audio_filters: str = "") -> Path | None:
    key = cache_key(path, audio_filters)
    return None if key is None else directory / f"{key}.peaks"


def save_peaks(waveform: Waveform, target: Path) -> bool:
    """Write an envelope beside the cache. Failure is not worth interrupting anybody."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(_MAGIC)
            handle.write(
                struct.pack(
                    "<idi",
                    waveform.peaks_per_second,
                    waveform.duration,
                    waveform.peak_amplitude,
                )
            )
            handle.write(waveform.peaks.tobytes())
        return True
    except OSError as exc:
        log.info("Could not cache the waveform: %s", exc)
        return False


def load_peaks(source: Path) -> Waveform | None:
    """Read a cached envelope, or None if it is absent or unreadable.

    A cache that cannot be read is a cache miss, never an error: the recording can
    always be read again, and refusing to open a file because a derived file is corrupt
    would be the wrong trade entirely.
    """
    if not source.exists():
        # The ordinary first open of a recording. Not worth a line, and saying
        # "unreadable" about it made routine behaviour look like a fault in the log.
        return None
    try:
        with source.open("rb") as handle:
            if handle.read(len(_MAGIC)) != _MAGIC:
                return None
            header = handle.read(struct.calcsize("<idi"))
            peaks_per_second, duration, peak_amplitude = struct.unpack("<idi", header)
            peaks = array.array("h")
            peaks.frombytes(handle.read())
    except (OSError, struct.error, ValueError) as exc:
        log.info("Ignoring an unreadable waveform cache: %s", exc)
        return None

    if peaks_per_second <= 0 or duration <= 0 or len(peaks) < 2:
        return None
    return Waveform(
        peaks=peaks,
        peaks_per_second=peaks_per_second,
        duration=duration,
        peak_amplitude=max(1, peak_amplitude),
    )


def prune_cache(directory: Path, keep_bytes: int = 256 * 1024 * 1024) -> int:
    """Drop the least recently used envelopes once the cache gets large.

    Four megabytes a recording adds up across a case of long sessions, and nothing else
    would ever delete them. Returns how many files were removed.
    """
    try:
        entries = sorted(
            (p for p in directory.glob("*.peaks") if p.is_file()),
            key=lambda p: p.stat().st_atime,
            reverse=True,
        )
    except OSError:
        return 0

    removed = 0
    running = 0
    for entry in entries:
        try:
            running += entry.stat().st_size
            if running > keep_bytes:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


__all__ = [
    "DETAIL_THRESHOLD_SECONDS",
    "PEAKS_PER_SECOND",
    "Waveform",
    "WaveformCancelled",
    "WaveformError",
    "audio_duration",
    "cache_path",
    "extract_peaks",
    "load_peaks",
    "prune_cache",
    "save_peaks",
]
