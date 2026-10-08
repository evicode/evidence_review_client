"""Cutting an event out of its recording.

What matters here is not that ffmpeg exits cleanly but that the clip is honest: the
event is inside it, the picture has not been through a second compression, and the
provenance file says exactly where in the clip the event begins.

These need the bundled ffmpeg. They skip without it rather than fail, so a checkout
that has not run fetch_ffmpeg.ps1 still has a green suite -- but the skip is loud.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evidence_review.media.clip import (
    ClipError,
    ClipMode,
    export_clip,
    find_ffmpeg,
    find_ffprobe,
    keyframe_at_or_before,
    suggested_name,
    write_sidecar,
)

ffmpeg = find_ffmpeg()
ffprobe = find_ffprobe()

needs_ffmpeg = pytest.mark.skipif(
    ffmpeg is None or ffprobe is None,
    reason="ffmpeg is not present; run installer/fetch_ffmpeg.ps1",
)


def probe(path: Path) -> dict:
    """Codec and duration, straight from ffprobe."""
    result = subprocess.run(
        [
            str(ffprobe), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(result.stdout or "{}")


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    """A minute of H.264 with a keyframe every five seconds, as CCTV tends to be.

    Widely spaced keyframes are the point: they are what makes a lossless cut start
    before the marked moment, which is the behaviour most of this file is about.
    """
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path_factory.mktemp("media") / "camera.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=60:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
            "-c:v", "libopenh264",
            "-g", "125", "-keyint_min", "125", "-sc_threshold", "0",
            "-c:a", "aac", str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


# --------------------------------------------------------------------------- #
# Where a lossless cut can start
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_a_copy_starts_at_or_before_the_moment_asked_for(recording: Path) -> None:
    """Never after. Starting late would cut the beginning off the event, which is the
    one failure that loses evidence rather than adding context."""
    for asked in (0.0, 7.0, 12.4, 23.9, 48.2):
        found = keyframe_at_or_before(recording, asked)
        assert found <= asked + 0.001, f"asked {asked}, got {found}"
        assert found >= 0.0


@needs_ffmpeg
def test_the_lead_in_is_real_on_a_source_with_spaced_keyframes(recording: Path) -> None:
    """If this ever came back zero the reporting would be vacuous."""
    assert keyframe_at_or_before(recording, 23.9) == pytest.approx(20.0, abs=0.2)


def test_asking_before_the_start_cannot_go_negative(tmp_path: Path) -> None:
    assert keyframe_at_or_before(tmp_path / "missing.mkv", -5.0) == 0.0


# --------------------------------------------------------------------------- #
# The default: original recorded data
# --------------------------------------------------------------------------- #


def packet_sizes(path: Path, start: float, count: int) -> list[int]:
    """The encoded size of each video packet from `start`, in order.

    Comparing codec names cannot tell a copy from a re-compression -- re-encoding H.264
    gives H.264 back. The packets can: a stream copy carries the encoded bytes across
    untouched, so their sizes match the source exactly, and any re-encode produces its
    own.
    """
    result = subprocess.run(
        [
            str(ffprobe), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=size",
            "-read_intervals", f"{start:.3f}%+#{count}",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    packets = json.loads(result.stdout or "{}").get("packets", [])
    return [int(packet["size"]) for packet in packets]


@needs_ffmpeg
def test_a_clip_carries_the_original_encoded_bytes(recording: Path, tmp_path: Path) -> None:
    """Nothing is decoded and nothing re-compressed, so the encoded packets in the clip
    are the packets in the source. A second compression invents artefacts that can be
    read as detail, and can bury the traces that reveal an edit."""
    target = tmp_path / "clip.mkv"
    result = export_clip(
        source=recording, target=target, start_seconds=23.9, duration_seconds=4.0,
        mode=ClipMode.ORIGINAL,
    )

    assert probe(target)["streams"][0]["codec_name"] == probe(recording)["streams"][0]["codec_name"]

    from_source = packet_sizes(recording, result.actual_start, 40)
    from_clip = packet_sizes(target, 0.0, 40)
    assert from_clip, "the clip has no video packets"
    assert from_clip == from_source, (
        "the packets differ, so the picture was re-compressed rather than copied"
    )


@needs_ffmpeg
def test_the_clip_contains_the_whole_event(recording: Path, tmp_path: Path) -> None:
    target = tmp_path / "clip.mkv"
    result = export_clip(
        source=recording, target=target, start_seconds=23.9, duration_seconds=4.0,
        mode=ClipMode.ORIGINAL,
    )
    duration = float(probe(target)["format"]["duration"])
    # Long enough for the lead-in and the event, never shorter than the event.
    assert duration >= 4.0 - 0.1
    assert duration == pytest.approx(result.duration, abs=0.3)
    assert result.actual_start <= 23.9
    assert result.lead_in > 0, "this source has spaced keyframes, so there must be one"


@needs_ffmpeg
def test_audio_only_media_can_be_clipped(tmp_path: Path) -> None:
    """A voice recorder is media too, and an EVP is the paranormal template's bread
    and butter."""
    source = tmp_path / "recorder.m4a"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:a", "aac", str(source)],
        check=True, capture_output=True,
    )
    target = tmp_path / "clip.m4a"
    export_clip(
        source=source, target=target, start_seconds=10.0, duration_seconds=5.0,
        mode=ClipMode.ORIGINAL,
    )
    assert target.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Frame-exact
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_frame_exact_starts_on_the_moment_and_is_lossless(
    recording: Path, tmp_path: Path
) -> None:
    """FFV1 rather than x264: it is mathematically lossless, it is in the LGPL build
    we ship, and it is what archives preserve video in."""
    target = tmp_path / "exact.mkv"
    result = export_clip(
        source=recording, target=target, start_seconds=23.9, duration_seconds=4.0,
        mode=ClipMode.EXACT,
    )
    assert result.lead_in == 0.0
    assert result.actual_start == 23.9
    assert probe(target)["streams"][0]["codec_name"] == "ffv1"
    assert float(probe(target)["format"]["duration"]) == pytest.approx(4.0, abs=0.15)


@needs_ffmpeg
def test_frame_exact_is_written_as_matroska(recording: Path, tmp_path: Path) -> None:
    """FFV1 has no place in an .mp4, so asking for one must not produce a file nothing
    will open."""
    result = export_clip(
        source=recording, target=tmp_path / "exact.mp4", start_seconds=10.0,
        duration_seconds=2.0, mode=ClipMode.EXACT,
    )
    assert result.path.suffix == ".mkv"
    assert result.path.is_file()


# --------------------------------------------------------------------------- #
# The provenance file
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_the_sidecar_says_where_in_the_clip_the_event_starts(
    recording: Path, tmp_path: Path
) -> None:
    """A clip that begins early is fine. A clip that begins early without saying so is
    a clip somebody will mis-describe in a statement."""
    result = export_clip(
        source=recording, target=tmp_path / "clip.mkv", start_seconds=23.9,
        duration_seconds=4.0, mode=ClipMode.ORIGINAL, source_sha256="b" * 64,
    )
    sidecar = write_sidecar(
        result, entry_id="abc-123", case_id="harbour-street",
        investigator="Jane Doe", summary="Male enters frame.", app_version="9.9.9",
    )
    text = sidecar.read_text(encoding="utf-8")

    assert str(recording) in text, "the source has to be named"
    assert "b" * 64 in text, "and its hash"
    assert "re-compressed" in text, "and that the picture was not re-compressed"

    # The sentence, not the number on its own: "3.900" is a substring of the "23.900"
    # printed higher up the same file, so looking for the figure alone matched even
    # when the explanation had been removed entirely.
    assert f"{result.lead_in:.3f} s before the marked moment" in text
    assert "within the clip" in text, "and where in the clip to look for the event"
    assert "abc-123" in text and "harbour-street" in text and "Jane Doe" in text
    assert sidecar.name.endswith(".mkv.txt"), "it sits beside the clip it describes"


@needs_ffmpeg
def test_the_sidecar_states_the_length_the_file_really_is(
    recording: Path, tmp_path: Path
) -> None:
    """A cut lands on whole frames and packets, so the file runs a little past the span
    asked for. The provenance file has to state the length somebody would measure if
    they checked it, not the length that was requested."""
    target = tmp_path / "clip.mkv"
    result = export_clip(
        source=recording, target=target, start_seconds=23.9, duration_seconds=4.0,
        mode=ClipMode.ORIGINAL,
    )
    measured = float(probe(target)["format"]["duration"])

    assert result.measured_duration is not None, "the clip has to be read back"
    assert result.measured_duration == pytest.approx(measured, abs=0.01)

    text = write_sidecar(result).read_text(encoding="utf-8")
    length_line = next(ln for ln in text.splitlines() if "Clip length" in ln)
    assert f"{measured:.3f} s" in length_line, length_line


@needs_ffmpeg
def test_the_sidecar_for_an_exact_clip_says_it_was_re_encoded(
    recording: Path, tmp_path: Path
) -> None:
    """Honest in the other direction: this one did go through an encoder, losslessly."""
    result = export_clip(
        source=recording, target=tmp_path / "exact.mkv", start_seconds=10.0,
        duration_seconds=2.0, mode=ClipMode.EXACT,
    )
    text = write_sidecar(result).read_text(encoding="utf-8")
    assert "FFV1" in text
    assert "identical to the source" in text


# --------------------------------------------------------------------------- #
# What goes wrong
# --------------------------------------------------------------------------- #


def test_a_missing_source_is_refused_with_the_path(tmp_path: Path) -> None:
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    with pytest.raises(ClipError, match="not where the entry says"):
        export_clip(
            source=tmp_path / "gone.mkv", target=tmp_path / "out.mkv",
            start_seconds=1.0, duration_seconds=2.0,
        )


def test_an_event_with_no_length_is_refused(tmp_path: Path) -> None:
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    source = tmp_path / "any.mkv"
    source.write_bytes(b"not really media")
    with pytest.raises(ClipError, match="needs a length"):
        export_clip(
            source=source, target=tmp_path / "out.mkv",
            start_seconds=1.0, duration_seconds=0.0,
        )


@needs_ffmpeg
def test_a_file_that_is_not_media_fails_with_ffmpegs_reason(tmp_path: Path) -> None:
    """The message has to carry something the reviewer can act on, not just a code."""
    source = tmp_path / "notes.txt"
    source.write_text("this is not a video", encoding="utf-8")
    with pytest.raises(ClipError) as raised:
        export_clip(
            source=source, target=tmp_path / "out.mkv",
            start_seconds=0.0, duration_seconds=1.0,
        )
    assert "ffmpeg" in str(raised.value).lower()
    assert len(str(raised.value)) > 40, "a bare exit code is not an explanation"


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def test_the_suggested_name_says_which_file_and_which_moment() -> None:
    """A clip gets emailed on its own; the name is all the context it carries."""
    name = suggested_name(Path(r"C:\evidence\Cam4 rear.mkv"), 1883.4)
    assert name.startswith("Cam4_rear_")
    assert "003123" in name, "the timecode of the moment"
    assert name.endswith(".mkv")


def test_two_events_in_the_same_second_get_different_names() -> None:
    """A reviewer marking two things 0.8 s apart would otherwise be offered an overwrite
    prompt that reads like it is replacing a stale file, and lose the first clip."""
    source = Path(r"C:\evidence\Cam4.mkv")
    first = suggested_name(source, 23.1)
    second = suggested_name(source, 23.9)
    assert first != second, f"both events proposed {first}"
    assert suggested_name(source, 23.0) == "Cam4_000023.mkv", (
        "a whole second keeps the plain name"
    )


def test_the_suggested_name_survives_a_hostile_filename() -> None:
    name = suggested_name(Path("a b/c:d*?.mp4".replace("/", "_")), 0.0)
    assert "/" not in name and ":" not in name and "*" not in name and "?" not in name
    assert name.endswith(".mp4")


@pytest.fixture(scope="module")
def plain_recording(tmp_path_factory) -> Path:
    """A flat dark recording, for the tests that look for a drawn mark by its colour.

    The `recording` fixture uses ffmpeg's test pattern, which contains magenta bars of
    its own -- so "is there magenta in this frame" cannot tell a burned-in mark from the
    source. A single dark colour makes the question unambiguous.
    """
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path_factory.mktemp("plain") / "flat.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=#101820:size=320x240:duration=30:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-c:v", "libopenh264", "-g", "125", "-keyint_min", "125",
            "-sc_threshold", "0", "-c:a", "aac", str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


# --------------------------------------------------------------------------- #
# Annotations burned into the picture
# --------------------------------------------------------------------------- #


def a_mark(**overrides):
    """A magenta circle, which no test source contains by accident."""
    from evidence_review.annotations import Annotation, AnnotationKind

    base = {
        "entry_id": "e1", "kind": AnnotationKind.ELLIPSE,
        "x1": 0.3, "y1": 0.3, "x2": 0.7, "y2": 0.7,
        "colour": "#ff00ff", "stroke": 0.02,
        "start_seconds": 0.0, "end_seconds": 60.0,
    }
    base.update(overrides)
    return Annotation.model_validate(base)


def mark_pixels(clip: Path, when: float, tmp_path: Path) -> int:
    """How much magenta is in the frame at ``when`` seconds into ``clip``."""
    from PySide6.QtGui import QImage

    frame = tmp_path / f"probe_{abs(hash((str(clip), when))) % 100000}.png"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{when:.3f}", "-i", str(clip), "-frames:v", "1", str(frame)],
        check=False, capture_output=True,
    )
    if not frame.is_file():
        return -1
    image = QImage(str(frame))
    if image.isNull():
        return -1
    image = image.convertToFormat(QImage.Format.Format_RGB32)
    count = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            colour = image.pixelColor(x, y)
            if colour.red() > 170 and colour.blue() > 170 and colour.green() < 90:
                count += 1
    return count


@needs_ffmpeg
def test_a_clip_exported_without_annotations_carries_none_of_them(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """The default, and the one that must never drift. A clip of the recording is the
    recording -- not a later reader's circles."""
    target = tmp_path / "plain.mkv"
    result = export_clip(
        source=plain_recording, target=target, start_seconds=10.0, duration_seconds=4.0,
        mode=ClipMode.ORIGINAL, annotations=[a_mark()], burn_annotations=False,
    )
    assert result.burned_annotations == 0
    assert mark_pixels(target, 1.0, tmp_path) == 0


@needs_ffmpeg
def test_burning_into_a_stream_copy_is_refused_not_quietly_dropped(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """Copying the recorded bytes and drawing on them are incompatible. A reviewer who
    asked for both and silently got one would not know which."""
    with pytest.raises(ClipError, match="cannot be burned"):
        export_clip(
            source=plain_recording, target=tmp_path / "x.mkv", start_seconds=10.0,
            duration_seconds=4.0, mode=ClipMode.ORIGINAL,
            annotations=[a_mark()], burn_annotations=True,
        )


@needs_ffmpeg
def test_a_burned_clip_really_has_the_mark_in_the_picture(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    target = tmp_path / "burned.mkv"
    result = export_clip(
        source=plain_recording, target=target, start_seconds=10.0, duration_seconds=4.0,
        mode=ClipMode.EXACT, annotations=[a_mark()], burn_annotations=True,
    )
    assert result.burned_annotations == 1
    assert mark_pixels(target, 1.0, tmp_path) > 100, "the mark is not in the picture"


@needs_ffmpeg
@pytest.mark.parametrize("mode", [ClipMode.EXACT, ClipMode.COMPRESSED])
def test_a_burned_clip_is_the_length_that_was_asked_for(
    qt_app, plain_recording: Path, tmp_path: Path, mode
) -> None:
    """The length limit has to survive the extra inputs that burning adds.

    ffmpeg reads -t as an option for whatever comes next, so putting it before the
    overlay inputs made it an option on a PNG rather than the output's length. The clip
    then ran on past the event through the rest of the recording: a seven-second event
    produced a twenty-five gigabyte file, and every assertion about the marks in it
    still passed.
    """
    target = tmp_path / f"length_{mode.value}.mkv"
    export_clip(
        source=plain_recording, target=target, start_seconds=5.0, duration_seconds=3.0,
        mode=mode, annotations=[a_mark()], burn_annotations=True,
    )
    measured = float(probe(target)["format"]["duration"])
    assert measured == pytest.approx(3.0, abs=0.3), (
        f"asked for 3 s and got {measured:.2f} s"
    )


@needs_ffmpeg
def test_a_mark_is_burned_in_at_the_right_moment(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """The conversion most likely to be silently wrong.

    Seeking with -ss before -i makes the clip start at zero, so a mark recorded at 11 s
    of the recording has to be drawn at 1 s of a clip that starts at 10 s. Using the
    absolute time would put every mark past the end of the clip, where nothing fails and
    nothing is drawn.
    """
    target = tmp_path / "timed.mkv"
    export_clip(
        source=plain_recording, target=target, start_seconds=10.0, duration_seconds=6.0,
        mode=ClipMode.EXACT, burn_annotations=True,
        annotations=[a_mark(start_seconds=11.0, end_seconds=13.0)],
    )
    assert mark_pixels(target, 2.0, tmp_path) > 100, "missing while it should be shown"
    assert mark_pixels(target, 5.0, tmp_path) == 0, "still shown after its window ended"


@needs_ffmpeg
def test_a_mark_outside_the_clip_is_simply_not_drawn(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.mkv"
    result = export_clip(
        source=plain_recording, target=target, start_seconds=10.0, duration_seconds=4.0,
        mode=ClipMode.EXACT, burn_annotations=True,
        annotations=[a_mark(start_seconds=50.0, end_seconds=55.0)],
    )
    assert result.path.is_file(), "an out-of-range mark must not break the export"
    assert mark_pixels(target, 1.0, tmp_path) == 0


@needs_ffmpeg
def test_a_compressed_clip_is_much_smaller_and_still_carries_the_mark(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """Lossless annotated clips are too large to send, which is the whole reason this
    mode exists. It is the only one that discards picture detail, so it is never the
    default and the sidecar says what it did."""
    lossless = tmp_path / "lossless.mkv"
    compressed = tmp_path / "small.mp4"
    export_clip(source=plain_recording, target=lossless, start_seconds=10.0,
                duration_seconds=4.0, mode=ClipMode.EXACT,
                annotations=[a_mark()], burn_annotations=True)
    result = export_clip(source=plain_recording, target=compressed, start_seconds=10.0,
                         duration_seconds=4.0, mode=ClipMode.COMPRESSED,
                         annotations=[a_mark()], burn_annotations=True)

    assert probe(compressed)["streams"][0]["codec_name"] == "h264"
    assert mark_pixels(compressed, 1.0, tmp_path) > 100
    assert compressed.stat().st_size < lossless.stat().st_size / 2
    text = write_sidecar(result).read_text(encoding="utf-8")
    assert "RE-COMPRESSED" in text, "a lossy copy has to say so"


@needs_ffmpeg
def test_the_sidecar_declares_what_was_drawn_into_the_picture(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """Somebody holding the clip has to be able to tell the recording from a later
    reader's commentary on it."""
    marks = [a_mark(text="Figure in doorway", start_seconds=10.0, end_seconds=13.0)]
    result = export_clip(
        source=plain_recording, target=tmp_path / "burned.mkv", start_seconds=10.0,
        duration_seconds=4.0, mode=ClipMode.EXACT,
        annotations=marks, burn_annotations=True,
    )
    text = write_sidecar(result, annotations=marks).read_text(encoding="utf-8")

    assert "\nAnnotations\n" in text, "the section a reader scans for has to be there"
    assert "DRAWN INTO the picture" in text
    assert "not part of the recording" in text
    assert "source file is unchanged" in text
    assert "Figure in doorway" in text, "and what each mark said"


@needs_ffmpeg
def test_a_clean_clip_says_plainly_that_nothing_was_drawn(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """Stated either way. A reader should not have to notice the absence of a line."""
    result = export_clip(
        source=plain_recording, target=tmp_path / "clean.mkv", start_seconds=10.0,
        duration_seconds=4.0, mode=ClipMode.ORIGINAL,
    )
    text = write_sidecar(result).read_text(encoding="utf-8")
    assert "\nAnnotations\n" in text, "stated under the same heading either way"
    assert "Nothing has been drawn on this picture" in text


@needs_ffmpeg
def test_asking_to_burn_nothing_is_not_an_error(
    qt_app, plain_recording: Path, tmp_path: Path
) -> None:
    """An entry with no marks is the ordinary case, and the checkbox may still be on."""
    result = export_clip(
        source=plain_recording, target=tmp_path / "none.mkv", start_seconds=10.0,
        duration_seconds=4.0, mode=ClipMode.EXACT, annotations=[], burn_annotations=True,
    )
    assert result.path.is_file()
    assert result.burned_annotations == 0


@needs_ffmpeg
def test_audio_only_media_cannot_have_marks_burned_in(qt_app, tmp_path: Path) -> None:
    """Refused with a reason rather than producing a file with nothing drawn on it."""
    source = tmp_path / "recorder.m4a"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:a", "aac", str(source)],
        check=True, capture_output=True,
    )
    with pytest.raises(ClipError, match="no video stream"):
        export_clip(
            source=source, target=tmp_path / "out.mkv", start_seconds=5.0,
            duration_seconds=4.0, mode=ClipMode.EXACT,
            annotations=[a_mark()], burn_annotations=True,
        )
