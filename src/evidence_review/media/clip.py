"""Cutting the span of one logged event out of its source media.

A two-hour recording with a twelve-second event in it is not a useful thing to hand
anybody. This produces the twelve seconds as a file of its own.

Two ways to cut, and the difference matters more here than it would anywhere else:

**Original data** (the default) copies the recorded streams across untouched. Nothing is
decoded and nothing is re-compressed, so the pixels in the clip are the pixels the
camera wrote. Compressed video can only be cut at a keyframe, so the clip begins at the
keyframe at or before the marked moment -- up to several seconds early on CCTV, which
often places them far apart. That is not imprecision to be hidden: the lead-in is
measured, reported, and written into the sidecar, so the clip says exactly where in
itself the event starts.

**Frame-exact** re-wraps the span starting precisely on the marked frame, encoded with
FFV1 -- mathematically lossless, so the result is pixel-identical to the source even
though it has been through an encoder, and the format archives use to preserve video.
Exact boundaries, much larger files.

Neither mode re-compresses the picture. Forensic practice is that a re-compressed copy
is not evidence of what it shows -- the second pass invents artefacts that can be read
as detail, and can bury the traces that reveal an edit.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..annotations import Annotation
from ..util import format_timecode

log = logging.getLogger(__name__)

#: Stops a console window flashing up for every ffmpeg call on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

#: Long enough to find a keyframe even where they are very far apart, short enough
#: that the probe stays quick. CCTV at one keyframe every ten seconds is common.
KEYFRAME_SEARCH_SECONDS = 60.0


class ClipError(RuntimeError):
    """Raised when a clip cannot be produced. The message is shown to the reviewer."""


class ClipMode(str, Enum):
    ORIGINAL = "original"
    EXACT = "exact"
    #: Re-encoded H.264, for a clip that has to be small enough to send. The only
    #: mode that deliberately loses picture information, so nothing picks it by
    #: default and the provenance file says plainly that it was re-compressed.
    COMPRESSED = "compressed"

    @property
    def rewrites_the_picture(self) -> bool:
        """Does this mode decode and re-encode, rather than copy the recorded bytes?"""
        return self is not ClipMode.ORIGINAL


@dataclass(frozen=True, slots=True)
class ClipResult:
    """What was actually produced, which is not always what was asked for."""

    path: Path
    mode: ClipMode
    #: Where the clip begins in the source. Equals ``requested_start`` for EXACT.
    actual_start: float
    requested_start: float
    #: The span that was asked for: the lead-in plus the length of the event.
    duration: float
    #: How long the clip runs before reaching the marked moment.
    lead_in: float
    source: Path
    source_sha256: str | None
    sidecar: Path | None = None
    #: How many marks were drawn into the picture. Zero means the picture carries
    #: nothing that was not in the recording, which is what the provenance file has to
    #: be able to state either way.
    burned_annotations: int = 0
    #: The audio cleanup applied to this clip, as an ffmpeg specification, or "" for
    #: the recording's own audio. A processed export is a derived object and the
    #: provenance file has to say so.
    audio_filters: str = ""
    #: The length of the file that was actually written, read back from it.
    #:
    #: Not the same as ``duration``: a cut lands on whole frames and whole packets, so
    #: the file usually runs a little past the span asked for. The provenance file has
    #: to state the length somebody would measure if they checked, not the length that
    #: was requested. None if the file could not be probed.
    measured_duration: float | None = None

    @property
    def is_exact(self) -> bool:
        return self.lead_in < 0.001


# --------------------------------------------------------------------------- #
# Finding the tools
# --------------------------------------------------------------------------- #


def _candidate_directories() -> list[Path]:
    """Every place worth looking, most specific first.

    Mirrors how libmpv is found, because the two are shipped the same way: beside the
    application once installed, under vendor/ in a checkout.
    """
    candidates: list[Path] = []

    override = os.environ.get("EVREV_FFMPEG_DIR")
    if override:
        candidates.append(Path(override))

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.extend([Path(meipass), Path(meipass) / "ffmpeg"])
        exe_dir = Path(sys.executable).parent
        candidates.extend([exe_dir, exe_dir / "_internal", exe_dir / "_internal" / "ffmpeg"])

    package_root = Path(__file__).resolve().parent.parent
    candidates.append(package_root / "resources" / "ffmpeg")

    repo_root = package_root.parent.parent
    candidates.extend([repo_root / "vendor" / "ffmpeg", repo_root / "vendor"])

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _find_tool(stem: str) -> Path | None:
    name = f"{stem}.exe" if os.name == "nt" else stem
    for directory in _candidate_directories():
        candidate = directory / name
        if candidate.is_file():
            return candidate

    # Last resort: whatever is on PATH. Someone running from source may already have
    # a working install, and refusing to use it would be obstinate.
    from shutil import which

    found = which(stem)
    return Path(found) if found else None


def find_ffmpeg() -> Path | None:
    return _find_tool("ffmpeg")


def find_ffprobe() -> Path | None:
    return _find_tool("ffprobe")


INSTALL_HINT = (
    "ffmpeg could not be found.\n\n"
    "The installer normally places it beside the application. If you are running "
    "from source, fetch it once with:\n\n"
    "    powershell -ExecutionPolicy Bypass -File installer\\fetch_ffmpeg.ps1\n\n"
    "or set EVREV_FFMPEG_DIR to the folder containing ffmpeg.exe."
)


def describe_availability() -> str:
    """One line for the diagnostics panel."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return "ffmpeg: not found — clips cannot be exported"
    version = "unknown version"
    try:
        result = _run([str(ffmpeg), "-hide_banner", "-version"], timeout=15)
        first = result.stdout.splitlines()[0] if result.stdout else ""
        match = re.search(r"ffmpeg version (\S+)", first)
        if match:
            version = match.group(1)
    except (OSError, subprocess.SubprocessError, IndexError):  # pragma: no cover
        pass  # a diagnostics line must never be the thing that fails
    return f"ffmpeg: {version} at {ffmpeg}"


# --------------------------------------------------------------------------- #
# Running it
# --------------------------------------------------------------------------- #


def _run(command: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    log.debug("running %s", " ".join(command))
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_NO_WINDOW,
        check=False,
    )


def _ffmpeg_complaint(result: subprocess.CompletedProcess) -> str:
    """The useful line out of ffmpeg's output, rather than all of it.

    Its last lines carry the actual problem; the rest is build configuration nobody
    reading an error dialog needs.
    """
    lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    interesting = [
        line
        for line in lines
        if not line.startswith(("ffmpeg version", "built with", "configuration:", "lib"))
    ]
    return "\n".join((interesting or lines)[-4:]) or "ffmpeg failed without saying why."


def keyframe_at_or_before(source: Path, seconds: float) -> float:
    """The latest keyframe at or before ``seconds``, which is where a copy can start.

    Falls back to the requested time when it cannot be determined. That only makes the
    reported lead-in optimistic, never the clip wrong -- ffmpeg still starts where it
    must, and the sidecar records what was asked for.
    """
    ffprobe = find_ffprobe()
    if ffprobe is None or seconds <= 0:
        return max(0.0, seconds)

    window_start = max(0.0, seconds - KEYFRAME_SEARCH_SECONDS)
    command = [
        str(ffprobe),
        "-v", "error",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-read_intervals", f"{window_start:.3f}%{seconds + 0.001:.3f}",
        "-of", "json",
        str(source),
    ]
    try:
        result = _run(command, timeout=120)
        frames = json.loads(result.stdout or "{}").get("frames", [])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:  # pragma: no cover
        log.info("Could not read keyframes from %s: %s", source.name, exc)
        return max(0.0, seconds)

    times = []
    for frame in frames:
        raw = frame.get("best_effort_timestamp_time")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= seconds + 0.001:
            times.append(value)
    return max(times) if times else max(0.0, seconds)


def export_clip(
    *,
    source: Path,
    target: Path,
    start_seconds: float,
    duration_seconds: float,
    mode: ClipMode = ClipMode.ORIGINAL,
    source_sha256: str | None = None,
    annotations: list[Annotation] | None = None,
    burn_annotations: bool = False,
    audio_filters: str = "",
    timeout: float = 600,
) -> ClipResult:
    """Cut one span out of ``source`` into ``target``.

    With ``burn_annotations`` the marks in ``annotations`` are drawn into the picture.
    That cannot be done while copying the recorded streams, so it requires a mode that
    re-encodes; asking for it with :attr:`ClipMode.ORIGINAL` is refused rather than
    quietly downgraded.

    Raises :class:`ClipError` with something worth reading if it cannot be done.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise ClipError(INSTALL_HINT)
    if not source.is_file():
        raise ClipError(f"The source media is not where the entry says it is:\n\n{source}")
    if duration_seconds <= 0:
        raise ClipError("A clip needs a length. Give the event a duration first.")

    start_seconds = max(0.0, start_seconds)
    burned = 0
    workspace: Path | None = None

    marks = [a for a in (annotations or []) if not a.is_deleted]
    if burn_annotations and not marks:
        burn_annotations = False
    if "atempo" in (audio_filters or ""):
        # Refused rather than silently stripped or silently honoured. Honouring it cuts
        # the event in half (the output is limited to the event's real length, which at
        # half speed holds only half of it); stripping it without saying so would hand
        # back a clip that is not what was asked for. Callers use
        # AudioChain.for_export(), which leaves it out and says why.
        raise ClipError(
            "Playing slower is a listening aid and cannot be exported.\n\n"
            "It stretches the audio, so a clip cut to the length of the event would "
            "contain only part of it, and the timings in the provenance file would no "
            "longer describe the clip.\n\n"
            "The rest of the cleanup can be applied."
        )

    if audio_filters and mode is ClipMode.ORIGINAL:
        # Same reason as the annotations: the recorded audio cannot be copied across
        # untouched *and* filtered. Refused rather than quietly dropped, because a
        # reviewer who asked for a cleaned-up clip and got the raw one would have no
        # way of knowing.
        raise ClipError(
            "Audio cleanup cannot be applied to a clip cut from the original recorded "
            "data, because that mode copies the audio without decoding it.\n\n"
            "Choose frame-exact or compressed to apply it, or export without it."
        )

    if burn_annotations and mode is ClipMode.ORIGINAL:
        # Refused rather than quietly switched. Copying the recorded streams and
        # drawing on the picture are mutually exclusive -- the bytes would have to be
        # decoded to draw on them -- and a reviewer who asked for both and silently got
        # one would not know which.
        raise ClipError(
            "Annotations cannot be burned into a clip cut from the original recorded "
            "data, because that mode copies the picture without decoding it.\n\n"
            "Choose frame-exact or compressed to burn them in, or export without them."
        )

    if mode is ClipMode.EXACT and target.suffix.lower() not in (".mkv", ".mka", ".nut"):
        # FFV1 has no place in an .mp4; Matroska is where it belongs and what plays it.
        target = target.with_suffix(".mkv")
    target.parent.mkdir(parents=True, exist_ok=True)

    if mode is ClipMode.ORIGINAL:
        actual_start = keyframe_at_or_before(source, start_seconds)
        lead_in = max(0.0, start_seconds - actual_start)
        command = [
            str(ffmpeg), "-hide_banner", "-nostdin", "-y",
            # Seeking before -i is the fast path, and with a copy it lands on the
            # keyframe anyway, which is what actual_start already accounts for.
            "-ss", f"{actual_start:.3f}",
            "-i", str(source),
            "-t", f"{lead_in + duration_seconds:.3f}",
            "-map", "0",
            "-c", "copy",
            # Without this a clip starting mid-stream can carry negative timestamps
            # that some players refuse to open.
            "-avoid_negative_ts", "make_zero",
            str(target),
        ]
    else:
        actual_start = start_seconds
        lead_in = 0.0
        if mode is ClipMode.EXACT:
            # FFV1, not x264. It is mathematically lossless, so the picture is
            # identical to the source frame for frame; it is ffmpeg's own codec and
            # so present in the LGPL build we ship, where libx264 is not; and it is
            # what archives preserve video in, which is the same problem as this one.
            # -g 1 makes every frame a keyframe, so any frame can be decoded on its
            # own without the ones before it.
            codec = ["-c:v", "ffv1", "-level", "3", "-g", "1", "-c:a", "flac"]
        else:
            # libopenh264 rather than libx264, which the LGPL build does not carry.
            # The quality setting is high enough that the re-compression is not
            # obvious, but it is a re-compression and the sidecar says so.
            codec = [
                "-c:v", "libopenh264", "-b:v", "8M", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
            ]

        command = [
            str(ffmpeg), "-hide_banner", "-nostdin", "-y",
            "-ss", f"{start_seconds:.3f}",
            "-i", str(source),
        ]

        overlays: list[_Overlay] = []
        if burn_annotations:
            workspace = Path(tempfile.mkdtemp(prefix="evrev_overlay_"))
            overlays = _overlay_inputs(
                marks, source, start_seconds, duration_seconds, workspace
            )

        if overlays:
            # One mark can span several images, so count the spans that were drawn
            # rather than the marks that were offered.
            burned = len({a.annotation_id for overlay in overlays for a in marks
                          if a.overlaps(start_seconds + overlay.start,
                                        start_seconds + overlay.end)})
            for overlay in overlays:
                command += ["-i", str(overlay.image)]
            command += [
                "-filter_complex", _overlay_filter(overlays),
                "-map", "[annotated]",
                # Audio if the recording has any; "?" keeps a silent camera working.
                "-map", "0:a?",
            ]
        else:
            # Nothing to draw -- either none was asked for, or every mark belongs to a
            # part of the recording this clip does not cover. An empty filter graph is
            # an ffmpeg error, so the plain path is taken and the clip comes out clean.
            burned = 0
            command += ["-map", "0"]

        # -t goes here, after every -i, because ffmpeg reads it as an option for
        # whatever comes next: placed before the overlay inputs it became an option on
        # a PNG instead of the output's length, and the clip ran on past the event
        # until it had swallowed the rest of the recording. A 7-second event produced
        # 25 GB before this was found.
        if audio_filters:
            command += ["-af", audio_filters]

        command += ["-t", f"{duration_seconds:.3f}", *codec, str(target)]

    try:
        result = _run(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ClipError(
            f"ffmpeg did not finish within {timeout:.0f} seconds and was stopped."
        ) from exc
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)

    if result.returncode != 0:
        raise ClipError(f"ffmpeg could not write the clip.\n\n{_ffmpeg_complaint(result)}")
    if not target.is_file() or target.stat().st_size == 0:
        raise ClipError(
            "ffmpeg reported success but wrote nothing.\n\n" + _ffmpeg_complaint(result)
        )

    return ClipResult(
        path=target,
        mode=mode,
        actual_start=actual_start,
        requested_start=start_seconds,
        duration=lead_in + duration_seconds,
        lead_in=lead_in,
        source=source,
        source_sha256=source_sha256,
        measured_duration=_measure_duration(target),
        burned_annotations=burned,
        audio_filters=audio_filters,
    )


@dataclass(frozen=True, slots=True)
class _Overlay:
    """One rendered image and the span of the clip it is shown over."""

    image: Path
    start: float  # relative to the start of the clip, which is what ffmpeg sees
    end: float


def video_size(source: Path) -> tuple[int, int] | None:
    """The picture size **as the filter chain will see it**, or None if there is none.

    Not the coded size. Phone footage carries a rotation matrix, ffmpeg applies it when
    decoding, and a quarter-turn swaps the axes: a clip coded 640x360 arrives at the
    filters as 360x640. An overlay rendered to the coded size was then stretched across
    the rotated frame, and a square mark drawn as 0.30 x 0.30 of the picture came out
    0.55 x 0.18 -- distorted and moved off whatever it had been drawn around.

    It was invisible in review because the player gets its geometry from mpv, which has
    already applied the rotation. The preview was right and only the exported file was
    wrong, which is the worst way round for this to fail.
    """
    ffprobe = find_ffprobe()
    if ffprobe is None:
        return None
    try:
        result = _run(
            [
                str(ffprobe), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:stream_side_data=rotation",
                "-of", "json", str(source),
            ],
            timeout=30,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover
        return None
    if result.returncode != 0:
        return None

    try:
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        return None
    if not streams:
        return None

    stream = streams[0]
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None

    rotation = 0.0
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                rotation = float(side_data["rotation"])
            except (TypeError, ValueError):
                rotation = 0.0
            break

    # A quarter turn either way swaps the axes. Half a turn does not.
    if round(abs(rotation) / 90) % 2 == 1:
        width, height = height, width
    return width, height


def _overlay_inputs(
    annotations: list[Annotation],
    source: Path,
    clip_start: float,
    clip_duration: float,
    workspace: Path,
) -> list[_Overlay]:
    """Render one PNG per span over which the visible marks do not change.

    Rendered at the source's own resolution so ffmpeg composites pixel for pixel with
    no scaling of its own: a circle drawn around a face stays around that face.

    The times handed back are relative to the clip, not to the recording. ``-ss`` before
    ``-i`` makes the output start at zero, so an absolute time would put every mark in
    the wrong place -- and for a clip cut from an hour in, nowhere at all.
    """
    from ..annotations import render, segments

    size = video_size(source)
    if size is None:
        raise ClipError(
            "Annotations can only be burned into a recording that has a picture.\n\n"
            f"{source.name} has no video stream."
        )
    width, height = size

    clip_end = clip_start + clip_duration
    spans = segments(annotations, clip_start, clip_end)
    if not spans:
        return []

    workspace.mkdir(parents=True, exist_ok=True)
    overlays: list[_Overlay] = []
    for index, (start, end, showing) in enumerate(spans):
        image = render(showing, width, height)
        path = workspace / f"overlay_{index:03d}.png"
        if not image.save(str(path), "PNG"):
            raise ClipError(f"The annotation overlay could not be written to {path}.")
        overlays.append(_Overlay(path, max(0.0, start - clip_start), end - clip_start))
    return overlays


def _overlay_filter(overlays: list[_Overlay]) -> str:
    """A filter chain compositing each overlay over the span it belongs to.

    ``enable`` rather than one image for the whole clip, so a mark that is only on
    screen for part of the event is only drawn for that part.
    """
    steps: list[str] = []
    current = "0:v"
    for index, overlay in enumerate(overlays):
        label = "annotated" if index == len(overlays) - 1 else f"ov{index}"
        steps.append(
            f"[{current}][{index + 1}:v]"
            f"overlay=0:0:enable='between(t,{overlay.start:.3f},{overlay.end:.3f})'"
            f"[{label}]"
        )
        current = label
    return ";".join(steps)


def _measure_duration(path: Path) -> float | None:
    """How long the written file actually runs, read back from it.

    Probing costs one short ffprobe call and means the provenance file can state a
    length that holds up if somebody measures it themselves.
    """
    ffprobe = find_ffprobe()
    if ffprobe is None:
        return None
    try:
        result = _run(
            [
                str(ffprobe), "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            timeout=30,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover
        return None
    if result.returncode != 0:
        return None
    try:
        return float((result.stdout or "").strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def _stated_length(result: ClipResult) -> float:
    """The length to put in the provenance file.

    The measured length when it could be read, because that is what somebody checking
    the file will see. The requested span otherwise, which is the best that can be said.
    """
    return result.measured_duration if result.measured_duration is not None else result.duration


def write_sidecar(
    result: ClipResult,
    *,
    annotations: list[Annotation] | None = None,
    entry_id: str | None = None,
    case_id: str | None = None,
    investigator: str | None = None,
    summary: str | None = None,
    app_version: str | None = None,
) -> Path:
    """Write a plain-text record beside the clip saying where it came from.

    A clip on its own is an assertion. This is what lets somebody else check it: the
    file it was cut from, that file's hash, the exact span, and -- for an original-data
    clip -- how far before the marked moment it begins.
    """
    lines = [
        "Evidence Review — exported clip",
        "",
        f"Clip file        : {result.path.name}",
        f"Exported (UTC)   : {_dt.datetime.now(_dt.UTC).isoformat(timespec='seconds')}",
    ]
    if app_version:
        lines.append(f"Exported by      : Evidence Review {app_version}")
    if investigator:
        lines.append(f"Investigator     : {investigator}")
    lines += [
        "",
        "Source",
        f"  File           : {result.source}",
        f"  SHA-256        : {result.source_sha256 or 'not recorded'}",
        "",
        "Span",
        f"  Event starts at: {format_timecode(result.requested_start)} "
        f"({result.requested_start:.3f} s into the source)",
        f"  Clip begins at : {format_timecode(result.actual_start)} "
        f"({result.actual_start:.3f} s into the source)",
        f"  Clip length    : {format_timecode(_stated_length(result))} "
        f"({_stated_length(result):.3f} s)",
    ]

    if result.mode is ClipMode.ORIGINAL:
        lines += [
            "",
            "Method",
            "  The recorded streams were copied without being decoded or re-compressed,",
            "  so the picture in this clip is the picture in the source file.",
        ]
        if result.lead_in > 0.001:
            lines += [
                "",
                "  Compressed video can only be cut at a keyframe, so this clip begins",
                f"  {result.lead_in:.3f} s before the marked moment. The event starts at",
                f"  {format_timecode(result.lead_in)} within the clip.",
            ]
        else:
            lines.append("  A keyframe fell on the marked moment, so the clip starts exactly there.")
    elif result.mode is ClipMode.EXACT:
        lines += [
            "",
            "Method",
            "  Re-wrapped from the exact marked frame and encoded with FFV1, which is",
            "  mathematically lossless: the picture is identical to the source frame for",
            "  frame. Every frame is a keyframe and can be decoded on its own.",
        ]
    else:
        lines += [
            "",
            "Method",
            "  Re-wrapped from the exact marked frame and RE-COMPRESSED as H.264 to keep",
            "  the file small enough to send. This is a lossy copy: fine detail has been",
            "  discarded and the encoder has introduced artefacts of its own. For",
            "  examination of the picture itself, go back to the source file.",
        ]

    # Said plainly, and said either way. Somebody holding this clip has to be able to
    # tell what is the recording and what a later reader did to it -- without having to
    # notice the absence of a line.
    lines += ["", "Audio"]
    if result.audio_filters:
        from .audio_filters import AudioChain

        described = AudioChain.from_spec(result.audio_filters).describe()
        lines += [
            "  THE AUDIO IN THIS CLIP HAS BEEN PROCESSED. It is not what the recorder",
            "  captured.",
            "",
            f"  What was applied: {described or result.audio_filters}",
            "",
            "  Exactly, as an ffmpeg filter chain, so it can be reproduced or undone by",
            "  going back to the source:",
            f"    {result.audio_filters}",
            "",
            "  Noise reduction in particular can create detail that was not recorded,",
            "  and what it creates can resemble speech. Any conclusion drawn from this",
            "  clip should be checked against the unprocessed source.",
        ]
    else:
        lines.append("  Unprocessed. The audio is as it was recorded.")

    lines += ["", "Annotations"]
    if result.burned_annotations:
        lines += [
            f"  {result.burned_annotations} mark(s) have been DRAWN INTO the picture of",
            "  this clip. They are not part of the recording: they were added afterwards",
            "  by the reviewer named above to point out what they observed.",
            "  The source file is unchanged and carries none of them.",
        ]
        if annotations:
            for annotation in annotations:
                window = (
                    f"{format_timecode(annotation.start_seconds)}"
                    f"-{format_timecode(annotation.end_seconds)}"
                )
                lines.append(f"    - {annotation.summary}  [{window}]")
    else:
        lines.append("  None. Nothing has been drawn on this picture.")

    if entry_id or case_id or summary:
        lines += ["", "Entry"]
        if summary:
            lines.append(f"  {summary}")
        if case_id:
            lines.append(f"  Case         : {case_id}")
        if entry_id:
            lines.append(f"  Entry ID     : {entry_id}")

    sidecar = result.path.with_suffix(result.path.suffix + ".txt")
    sidecar.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sidecar


def suggested_name(source: Path, start_seconds: float, extension: str | None = None) -> str:
    """A filename that says which file and which moment, so it survives being emailed.

    The fraction is kept when there is one. Two events in the same second of the same
    recording would otherwise propose the same filename, and the reviewer would meet an
    overwrite prompt that looks like it is offering to replace a stale file.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem)[:60].strip("_") or "clip"
    marker = format_timecode(start_seconds, millis=False).replace(":", "")
    millis = round(start_seconds * 1000) % 1000
    if millis:
        marker += f"_{millis:03d}"
    return f"{stem}_{marker}{extension or source.suffix or '.mkv'}"
