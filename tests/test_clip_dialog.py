"""The dialog a reviewer actually meets when exporting a clip.

The engine is covered in test_clip.py. What matters here is the one thing the dialog
exists to do: say what the clip will be *before* it is written. Cutting compressed video
without re-compressing it can only begin at a keyframe, so an original-data clip can
start several seconds before the marked moment. A clip that begins early is fine. A clip
that begins early without saying so is a clip somebody mis-describes in a statement.

These need the bundled ffmpeg, because the lead-in is read from a real file rather than
guessed. They skip without it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evidence_review.builtin_templates import PARANORMAL_TEMPLATE
from evidence_review.media.clip import ClipMode, find_ffmpeg, find_ffprobe
from evidence_review.models import MediaKind
from evidence_review.ui.clip_dialog import ClipExportDialog

from .conftest import AREA, OBSERVATION, make_entry

ffmpeg = find_ffmpeg()

needs_ffmpeg = pytest.mark.skipif(
    ffmpeg is None or find_ffprobe() is None,
    reason="ffmpeg is not present; run installer/fetch_ffmpeg.ps1",
)


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    """A minute of H.264 with a keyframe every five seconds, as CCTV tends to be."""
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path_factory.mktemp("media") / "Cam4 rear.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=60:size=320x240:rate=25",
            "-c:v", "libopenh264",
            "-g", "125", "-keyint_min", "125", "-sc_threshold", "0", str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


@pytest.fixture
def dialog(qt_app, recording: Path, tmp_path: Path) -> ClipExportDialog:
    entry = make_entry(
        case_id="harbour-street",
        file_name=recording.name,
        media_path=str(recording),
        media_kind=MediaKind.VIDEO,
        event_offset_seconds=23.9,
        event_duration_seconds=4.0,
        media_duration_seconds=60.0,
        investigator_name="Jane Doe",
        media_sha256="c" * 64,
        values={AREA: "North gate", OBSERVATION: "Figure crosses the yard."},
    )
    return ClipExportDialog(
        entry, template=PARANORMAL_TEMPLATE, default_directory=tmp_path
    )


def retype(dialog: ClipExportDialog, timecode: str) -> None:
    """Edit the start the way a reviewer does.

    ``set_seconds`` is deliberately silent -- it is how the dialog fills the field in the
    first place. A real edit ends with editingFinished, which is what triggers the
    recalculation, and that is the path worth testing.
    """
    dialog.start_edit.setText(timecode)
    dialog.start_edit.editingFinished.emit()


# --------------------------------------------------------------------------- #
# What it promises before writing anything
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_the_lead_in_is_on_screen_before_anything_is_written(dialog) -> None:
    text = dialog.lead_in_label.text()
    assert "before the marked moment" in text
    assert "3.90" in text, "with the figure, not just a vague warning"
    assert "00:00:20" in text, "and the keyframe it will actually start at"


@needs_ffmpeg
def test_the_dialog_opens_on_the_entrys_own_span(dialog) -> None:
    """Pre-filled from the entry, so the common case is one click."""
    assert dialog.start_edit.seconds() == 23.9
    assert dialog.length_edit.seconds() == 4.0


@needs_ffmpeg
def test_a_moment_that_lands_on_a_keyframe_says_so(dialog) -> None:
    """Silence would read as "no warning because nothing was checked"."""
    retype(dialog, "00:00:20.000")
    assert "exactly there" in dialog.lead_in_label.text()


@needs_ffmpeg
def test_editing_the_start_recalculates_the_lead_in(dialog) -> None:
    """A stale figure is worse than none: it is a promise about a different clip."""
    retype(dialog, "00:00:20.000")
    assert "before the marked moment" not in dialog.lead_in_label.text()
    retype(dialog, "00:00:23.900")
    assert "3.90" in dialog.lead_in_label.text()


@needs_ffmpeg
def test_frame_exact_has_no_keyframe_warning_to_give(dialog) -> None:
    dialog.exact_radio.setChecked(True)
    assert dialog.lead_in_label.text() == ""
    dialog.original_radio.setChecked(True)
    assert "before the marked moment" in dialog.lead_in_label.text()


@needs_ffmpeg
def test_the_suggested_file_names_the_camera_and_the_moment(dialog) -> None:
    name = Path(dialog.target_edit.text()).name
    assert name.startswith("Cam4_rear_")
    assert "000023_900" in name, "to the fraction, so two close events do not collide"


# --------------------------------------------------------------------------- #
# What it refuses
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_a_clip_with_no_length_is_refused_rather_than_written(dialog, tmp_path) -> None:
    """Entries logged as a moment rather than a span have no duration, so this is the
    ordinary case, not a mistake."""
    dialog.length_edit.set_seconds(0.0)
    dialog._export()
    assert "length" in dialog.status_label.text().lower()
    assert not list(tmp_path.glob("*.mkv")), "nothing may be written"
    assert dialog.result_clip is None


# --------------------------------------------------------------------------- #
# A real export, through the dialog's own worker thread
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_exporting_writes_the_clip_and_its_provenance(dialog, qt_app, tmp_path) -> None:
    """End to end, including the sidecar being filled from the entry rather than from
    whatever the reviewer happens to type."""
    target = tmp_path / "exported.mkv"
    dialog.target_edit.setText(str(target))
    dialog.sidecar_check.setChecked(True)

    done: list[bool] = []
    dialog.accepted.connect(lambda: done.append(True))
    dialog._export()

    from PySide6.QtCore import QDeadlineTimer

    deadline = QDeadlineTimer(60_000)
    while not done and not deadline.hasExpired():
        qt_app.processEvents()
    assert done, f"the export never completed: {dialog.status_label.text()}"

    clip = dialog.result_clip
    assert clip is not None
    assert clip.path.is_file() and clip.path.stat().st_size > 0
    assert clip.mode is ClipMode.ORIGINAL
    assert clip.lead_in == pytest.approx(3.9, abs=0.05), (
        "the clip has to match what the dialog promised"
    )
    # The dialog copies the result to attach the sidecar. Everything the engine measured
    # has to survive that copy, or the provenance file describes a different clip.
    assert clip.measured_duration is not None, (
        "the measured length was lost on the way back from the worker"
    )
    assert clip.measured_duration == pytest.approx(clip.duration, abs=0.5)
    assert clip.source_sha256 == "c" * 64
    assert clip.requested_start == pytest.approx(23.9)

    assert clip.sidecar is not None and clip.sidecar.is_file()
    side = clip.sidecar.read_text(encoding="utf-8")
    assert "Jane Doe" in side, "the investigator recorded against the entry"
    assert "harbour-street" in side
    assert "Figure crosses the yard" in side, "the entry's own summary, via the template"
    assert "c" * 64 in side, "the source hash the entry already held"
    assert "3.900 s before the marked moment" in side
