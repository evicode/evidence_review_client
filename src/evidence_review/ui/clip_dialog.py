"""Exporting the span of one event as a media file of its own.

The choice this dialog exists to put in front of the reviewer is not a technical
preference. Cutting compressed video without re-compressing it can only begin at a
keyframe, so the clip starts a little before the marked moment -- and on CCTV that can
be several seconds. The alternative starts exactly on the frame but has been through an
encoder.

So the lead-in is worked out and shown for the actual file before anything is written,
rather than being discovered afterwards or left unmentioned.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..annotations import Annotation
from ..media.clip import (
    ClipError,
    ClipMode,
    ClipResult,
    export_clip,
    find_ffmpeg,
    keyframe_at_or_before,
    suggested_name,
    write_sidecar,
)
from ..models import EntryRow
from ..templates import LogTemplate
from ..util import format_timecode
from ..version import APP_VERSION
from .widgets import TimecodeEdit, field_label, fit_to_screen, hint_label, scrollable

log = logging.getLogger(__name__)


class _Worker(QObject):
    """Runs the cut off the UI thread. ffmpeg on a long file is not instant."""

    finished = Signal(object)  # ClipResult
    failed = Signal(str)

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            self.finished.emit(export_clip(**self._kwargs))
        except ClipError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("Clip export failed")
            self.failed.emit(f"The clip could not be written.\n\n{exc}")


class ClipExportDialog(QDialog):
    """Cut this entry's event out of its source media."""

    def __init__(
        self,
        entry: EntryRow,
        *,
        template: LogTemplate | None = None,
        annotations: list[Annotation] | None = None,
        default_directory: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._template = template
        # Only the marks that fall inside this event's span. Burning in a mark that
        # belongs to a different part of the recording would draw nothing and leave
        # the reviewer wondering where it went.
        span_start = entry.event_offset_seconds or 0.0
        span_end = span_start + (entry.event_duration_seconds or 0.0)
        self._annotations = [
            a for a in (annotations or [])
            if not a.is_deleted and a.overlaps(span_start, max(span_end, span_start))
        ]
        # What this entry was written while listening through, if anything.
        self._audio_filters = entry.audio_filters or ""
        self._source = Path(entry.media_path)
        self._result: ClipResult | None = None
        self._thread: QThread | None = None

        self.setWindowTitle("Export clip")
        self.setModal(True)
        self.setMinimumWidth(620)

        self._build_ui(default_directory)
        self._refresh_lead_in()
        fit_to_screen(self)

    # ------------------------------------------------------------------ #

    def _build_ui(self, default_directory: Path | None) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        summary = self._template.summarise(self._entry.values) if self._template else ""
        heading = QLabel(summary or self._entry.file_name)
        heading.setWordWrap(True)
        heading.setStyleSheet("font-size:14px; font-weight:600;")
        outer.addWidget(heading)
        outer.addWidget(hint_label(f"from {self._source}"))

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)

        self.start_edit = TimecodeEdit()
        self.start_edit.set_seconds(self._entry.event_offset_seconds or 0.0)
        self.start_edit.value_changed.connect(lambda _v: self._refresh_lead_in())
        form.addRow(field_label("Event starts"), self.start_edit)

        self.length_edit = TimecodeEdit()
        self.length_edit.set_seconds(self._entry.event_duration_seconds or 0.0)
        form.addRow(field_label("Length"), self.length_edit)
        if not self._entry.event_duration_seconds:
            form.addRow(
                "",
                hint_label(
                    "This entry has no recorded duration, so give the clip a length."
                ),
            )

        # -- how to cut ----------------------------------------------------- #
        self.original_radio = QRadioButton("Original recorded data")
        self.original_radio.setChecked(True)
        self.original_radio.toggled.connect(lambda _on: self._refresh_lead_in())
        self.exact_radio = QRadioButton("Frame-exact")

        modes = QVBoxLayout()
        modes.setSpacing(2)
        modes.addWidget(self.original_radio)
        modes.addWidget(
            hint_label(
                "Copies the recorded streams without decoding them, so the picture is "
                "the picture the camera wrote. Starts at the nearest keyframe at or "
                "before the event."
            )
        )
        modes.addSpacing(6)
        modes.addWidget(self.exact_radio)
        modes.addWidget(
            hint_label(
                "Starts exactly on the marked frame, encoded losslessly (FFV1), so the "
                "picture is identical frame for frame. Much larger files."
            )
        )
        modes.addSpacing(6)
        self.compressed_radio = QRadioButton("Compressed, for sending")
        self.compressed_radio.toggled.connect(lambda _on: self._refresh_lead_in())
        modes.addWidget(self.compressed_radio)
        modes.addWidget(
            hint_label(
                "Re-compressed as H.264 to keep the file small enough to email. This "
                "one does lose picture detail — for examination, use one of the above."
            )
        )
        container = QWidget()
        container.setLayout(modes)
        form.addRow(field_label("How to cut"), container)

        self.lead_in_label = QLabel()
        self.lead_in_label.setWordWrap(True)
        form.addRow("", self.lead_in_label)

        # -- annotations ------------------------------------------------------ #
        # The default is off. A clip of the recording is the thing somebody normally
        # wants; a clip with a later reader's circles drawn into it is a different
        # object, and it should take a deliberate click to produce one.
        self.burn_check = QCheckBox("Draw the annotations into the picture")
        self.burn_check.setChecked(False)
        self.burn_check.toggled.connect(self._on_burn_toggled)
        burn_box = QVBoxLayout()
        burn_box.setSpacing(2)
        burn_box.addWidget(self.burn_check)
        self.burn_hint = hint_label("")
        self.burn_hint.setWordWrap(True)
        burn_box.addWidget(self.burn_hint)
        burn_container = QWidget()
        burn_container.setLayout(burn_box)
        form.addRow(field_label("Annotations"), burn_container)

        count = len(self._annotations)
        self.burn_check.setEnabled(bool(count))
        if not count:
            self.burn_check.setText("No annotations on this event")
            self.burn_hint.setText(
                "Nothing has been drawn on this part of the recording, so there is "
                "nothing to burn in."
            )
        else:
            self.burn_check.setText(
                f"Draw the {count} annotation{'s' if count != 1 else ''} into the picture"
            )
            self._refresh_burn_hint()

        # -- where ----------------------------------------------------------- #
        directory = default_directory or self._source.parent
        self.target_edit = QLineEdit(
            str(directory / suggested_name(self._source, self._entry.event_offset_seconds or 0.0))
        )
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        target_row = QHBoxLayout()
        target_row.setSpacing(6)
        target_row.addWidget(self.target_edit, 1)
        target_row.addWidget(browse)
        wrapper = QWidget()
        wrapper.setLayout(target_row)
        form.addRow(field_label("Save as"), wrapper)

        # -- audio cleanup ---------------------------------------------------- #
        self.clean_check = QCheckBox("Apply the audio cleanup this entry was logged with")
        self.clean_check.setChecked(False)
        self.clean_check.toggled.connect(self._on_clean_toggled)
        clean_box = QVBoxLayout()
        clean_box.setSpacing(2)
        clean_box.addWidget(self.clean_check)
        self.clean_hint = hint_label("")
        self.clean_hint.setWordWrap(True)
        clean_box.addWidget(self.clean_hint)
        clean_container = QWidget()
        clean_container.setLayout(clean_box)
        form.addRow(field_label("Audio"), clean_container)

        if self._audio_filters:
            from ..media.audio_filters import AudioChain

            described = AudioChain.from_spec(self._audio_filters).describe()
            self.clean_check.setText("Apply the cleanup this entry was logged with")
            self.clean_hint.setText(
                f"This entry was written while listening through: {described}. "
                "The clip will contain the recording's own audio unless you tick this."
            )
        else:
            self.clean_check.setEnabled(False)
            self.clean_check.setText("This entry was logged on the unprocessed audio")
            self.clean_hint.setText(
                "There is no cleanup recorded against it, so there is nothing to apply."
            )

        self.sidecar_check = QCheckBox("Write a provenance file beside the clip")
        self.sidecar_check.setChecked(True)
        self.sidecar_check.setToolTip(
            "A short text file naming the source, its hash, and exactly where in the "
            "clip the event begins."
        )
        form.addRow("", self.sidecar_check)

        # In a scroll area, so a short screen cannot cut the bottom of the form off;
        # the Export and Cancel buttons stay put below it.
        form_page = QWidget()
        form_page.setLayout(form)
        outer.addWidget(scrollable(form_page), 1)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        buttons = QDialogButtonBox()
        self.export_button = buttons.addButton("Export", QDialogButtonBox.ButtonRole.AcceptRole)
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._export)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------------ #

    def _browse(self) -> None:
        current = Path(self.target_edit.text())
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save clip as", str(current), "Media files (*.mkv *.mp4 *.mov *.m4a);;All files (*)"
        )
        if chosen:
            self.target_edit.setText(chosen)

    def _export_chain(self) -> str:
        """The entry's cleanup, minus anything that does not belong in a file.

        Playing slower is a listening aid: applied to an export it would cut the event
        short and make the timings in the provenance file wrong.
        """
        from ..media.audio_filters import AudioChain

        return AudioChain.from_spec(self._audio_filters).for_export().to_spec()

    def _on_clean_toggled(self, on: bool) -> None:
        """Processing the audio means decoding it, so the stream copy cannot stay."""
        if on and self.original_radio.isChecked():
            self.exact_radio.setChecked(True)
        self.original_radio.setEnabled(not on and not self.burn_check.isChecked())
        if on:
            from ..media.audio_filters import AudioChain

            message = (
                "The clip's audio will be processed, and its provenance file will say "
                "so and give the exact chain. Copying the original recorded data is "
                "not possible while doing this."
            )
            # Said rather than silently done. Slowing is left out of exports because it
            # would cut the event short, and a reviewer who set it should be told it is
            # not coming with them rather than finding out from the file.
            if AudioChain.from_spec(self._audio_filters).slows_playback:
                message += (
                    "\n\nPlaying slower is left out: it stretches the audio, so the "
                    "clip would hold only part of the event. The clip will be at the "
                    "recording's own speed."
                )
            self.clean_hint.setText(message)
        elif self._audio_filters:
            from ..media.audio_filters import AudioChain

            described = AudioChain.from_spec(self._audio_filters).describe()
            self.clean_hint.setText(
                f"This entry was written while listening through: {described}. "
                "The clip will contain the recording's own audio unless you tick this."
            )
        self._refresh_lead_in()

    def _on_burn_toggled(self, on: bool) -> None:
        """Burning in means re-encoding, so the stream-copy mode cannot stay selected.

        Moved for the reviewer rather than refused at export time, with the reason on
        screen: the two are genuinely incompatible, and discovering that after waiting
        for a cut to finish would be worse.
        """
        if on and self.original_radio.isChecked():
            self.exact_radio.setChecked(True)
        self.original_radio.setEnabled(not on and not self.clean_check.isChecked())
        self._refresh_burn_hint()
        self._refresh_lead_in()

    def _refresh_burn_hint(self) -> None:
        if not self._annotations:
            return
        if self.burn_check.isChecked():
            self.burn_hint.setText(
                "The marks will be part of the picture in this clip and cannot be "
                "removed from it. The recording itself is untouched, and the "
                "provenance file will say what was drawn. Copying the original "
                "recorded data is not possible while doing this."
            )
        else:
            self.burn_hint.setText(
                "The clip will contain the recording only. The marks stay saved "
                "against the entry."
            )

    def _refresh_lead_in(self) -> None:
        """Say what the chosen start will actually produce, before anything is written."""
        if self.exact_radio.isChecked():
            self.lead_in_label.setText("")
            return
        start = self.start_edit.seconds()
        if start is None or not self._source.is_file():
            self.lead_in_label.setText("")
            return

        keyframe = keyframe_at_or_before(self._source, start)
        lead_in = max(0.0, start - keyframe)
        if lead_in < 0.001:
            self.lead_in_label.setStyleSheet("color:#3fb950;")
            self.lead_in_label.setText(
                "A keyframe falls on the marked moment, so the clip will start exactly there."
            )
        else:
            self.lead_in_label.setStyleSheet("color:#e3a008;")
            self.lead_in_label.setText(
                f"This clip will begin {lead_in:.2f} s before the marked moment, at "
                f"{format_timecode(keyframe)} — the nearest keyframe. The event will be "
                f"{format_timecode(lead_in)} into the clip, and the provenance file will "
                "say so."
            )

    # ------------------------------------------------------------------ #

    def _export(self) -> None:
        if find_ffmpeg() is None:
            from ..media.clip import INSTALL_HINT

            QMessageBox.critical(self, "ffmpeg not found", INSTALL_HINT)
            return

        start = self.start_edit.seconds()
        length = self.length_edit.seconds()
        target = Path(self.target_edit.text().strip())

        problems = []
        if start is None:
            problems.append("The start is not a valid timecode.")
        if not length:
            problems.append("Give the clip a length.")
        if not target.name:
            problems.append("Choose where to save the clip.")
        if problems:
            self.status_label.setStyleSheet("color:#e5484d; font-weight:600;")
            self.status_label.setText("\n".join(f"• {problem}" for problem in problems))
            return

        if target.exists():
            answer = QMessageBox.question(
                self,
                "Replace that file?",
                f"{target.name} already exists.\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return

        self._set_busy(True)
        if self.original_radio.isChecked():
            mode = ClipMode.ORIGINAL
        elif self.exact_radio.isChecked():
            mode = ClipMode.EXACT
        else:
            mode = ClipMode.COMPRESSED

        self._thread = QThread(self)
        worker = _Worker(
            source=self._source,
            target=target,
            start_seconds=start,
            duration_seconds=length,
            mode=mode,
            source_sha256=self._entry.media_sha256,
            annotations=self._annotations,
            burn_annotations=self.burn_check.isChecked(),
            audio_filters=self._export_chain() if self.clean_check.isChecked() else "",
        )
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        # Kept alive by the dialog: a worker only the thread refers to is collected
        # mid-run and takes the signal connections with it.
        self._worker = worker
        self._thread.start()

    def _set_busy(self, busy: bool) -> None:
        self.export_button.setEnabled(not busy)
        for widget in (self.start_edit, self.length_edit, self.target_edit,
                       self.original_radio, self.exact_radio, self.sidecar_check):
            widget.setEnabled(not busy)
        if busy:
            self.status_label.setStyleSheet("")
            self.status_label.setText("Cutting the clip…")

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None

    def _on_finished(self, result: ClipResult) -> None:
        self._stop_thread()
        sidecar = None
        if self.sidecar_check.isChecked():
            try:
                summary = (
                    self._template.summarise(self._entry.values) if self._template else None
                )
                sidecar = write_sidecar(
                    result,
                    annotations=self._annotations if result.burned_annotations else None,
                    entry_id=self._entry.entry_id,
                    case_id=self._entry.case_id,
                    investigator=self._entry.investigator_name,
                    summary=summary,
                    app_version=APP_VERSION,
                )
            except OSError as exc:
                # The clip is written and that is the thing that mattered.
                QMessageBox.warning(
                    self,
                    "Clip saved, provenance file not",
                    f"The clip was written, but the provenance file could not be:\n\n{exc}",
                )

        # replace() rather than rebuilding field by field: the result is frozen, and a
        # hand-written copy silently drops any field added to it later.
        self._result = replace(result, sidecar=sidecar)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._stop_thread()
        self._set_busy(False)
        self.status_label.setStyleSheet("color:#e5484d; font-weight:600;")
        self.status_label.setText(message)

    def reject(self) -> None:
        self._stop_thread()
        super().reject()

    @property
    def result_clip(self) -> ClipResult | None:
        """What was written, or None if the dialog was cancelled."""
        return self._result
