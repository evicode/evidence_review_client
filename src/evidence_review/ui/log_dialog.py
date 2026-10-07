"""The LOG modal: capture one observation, with as much prefilled as we can justify."""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..autofill import MediaContext, guess_area
from ..config import Settings
from ..models import EntryRow, MediaKind, SyncState
from ..store import LocalStore
from ..templates import FieldRole, LogTemplate, TemplateField
from ..util import NO_VALUE, format_timecode, machine_name, utc_now
from ..version import APP_VERSION
from .field_widgets import FieldEditor
from .widgets import TimecodeEdit, field_label, fit_to_screen, scrollable

log = logging.getLogger(__name__)

#: Longest a form label runs before it is wrapped onto a second line, which keeps
#: the form's label column from stretching to fit "What Was Heard or Seen".
_LABEL_WRAP = 18


def _wrap_label(text: str) -> str:
    """Break a long label at a word boundary, as the fixed form did by hand."""
    if len(text) <= _LABEL_WRAP:
        return text
    words = text.split()
    line, lines = "", []
    for word in words:
        if line and len(line) + len(word) + 1 > _LABEL_WRAP:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "\n".join(lines)


class LogEntryDialog(QDialog):
    """Modal capturing the review fields for one observation.

    Event Timestamp is a position *within the media*, not a wall-clock time: it is
    what lets the reviewer jump straight back to the moment later. Event Duration
    is how long that moment lasted.
    """

    def __init__(
        self,
        *,
        context: MediaContext,
        settings: Settings,
        store: LocalStore,
        event_offset_seconds: float | None,
        event_duration_seconds: float | None = None,
        snapshot_path: str | None = None,
        audio_filters: str = "",
        existing: EntryRow | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._settings = settings
        self._store = store
        self._existing = existing
        self._snapshot_path = snapshot_path
        self._offset = event_offset_seconds
        self._duration = event_duration_seconds
        self._audio_filters = audio_filters
        self._entry: EntryRow | None = None

        # An entry being edited keeps the template it was written on, however the
        # case has been reconfigured since. A new one uses the case's current
        # template.
        self._case_id = existing.case_id if existing is not None else settings.review.case_id
        if existing is not None:
            self._template = store.get_template(existing.template_id) or store.template_for_case(
                self._case_id
            )
        else:
            self._template = store.template_for_case(self._case_id)

        #: One editor per template field, keyed by field_id, with its form label
        #: alongside so a rule can mark it required as the reviewer types.
        self._editors: dict[str, FieldEditor] = {}
        self._field_labels: dict[str, QLabel] = {}
        self._baseline: dict[str, object] = {}

        self.setWindowTitle("Log entry" if existing is None else "Edit entry")
        self.setModal(True)
        self.setMinimumWidth(860)
        self.setSizeGripEnabled(True)

        self._build_ui()
        self._populate(existing)
        self._wire_shortcuts()
        fit_to_screen(self)

        self._focus_first_field()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        # The fields scroll; the buttons below stay put. On a short screen the
        # alternative is a Save entry button under the bottom edge, which loses
        # the observation the reviewer just typed.
        body_container = QWidget()
        body = QHBoxLayout(body_container)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(18)
        outer.addWidget(scrollable(body_container), 1)

        form_container = QWidget()
        form = QFormLayout(form_container)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        body.addWidget(form_container, 1)

        # 1. File Name ---------------------------------------------------- #
        self.file_name_edit = QLineEdit()
        self.file_name_edit.setReadOnly(True)
        copy_button = QToolButton()
        copy_button.setText("⎘")
        copy_button.setToolTip("Copy the full path")
        copy_button.clicked.connect(self._copy_path)
        file_row = QHBoxLayout()
        file_row.setSpacing(6)
        file_row.addWidget(self.file_name_edit, 1)
        file_row.addWidget(copy_button)
        form.addRow(field_label("File Name"), self._wrap(file_row))

        # 2. The template's location field, if it has one ------------------ #
        #
        # It sits up here with the file name because it is context about the
        # recording rather than part of the observation, which is also where Area
        # has always been. Addressed by role, so a template that calls it
        # "Location / Camera" lands in the same place.
        location = self._template.location_field
        if location is not None:
            self._add_field_row(form, location)

        # 3. Event Timestamp: where in the media the event starts --------- #
        self.offset_edit = TimecodeEdit()
        self.offset_edit.setToolTip(
            "Where in this file the event happens, so it can be found again"
        )
        seek_button = QPushButton("Go to")
        seek_button.setToolTip("Move the player to this point")
        seek_button.clicked.connect(self._seek_to_offset)

        self.media_length_label = QLabel("--:--:--")
        self.media_length_label.setObjectName("timeLabel")

        offset_row = QHBoxLayout()
        offset_row.setSpacing(6)
        offset_row.addWidget(self.offset_edit)
        offset_row.addWidget(seek_button)
        offset_row.addSpacing(10)
        of_label = QLabel("of")
        of_label.setObjectName("hintLabel")
        offset_row.addWidget(of_label)
        offset_row.addWidget(self.media_length_label)
        offset_row.addStretch(1)
        form.addRow(field_label("Event Timestamp"), self._wrap(offset_row))

        # 4. Event Duration ----------------------------------------------- #
        self.duration_edit = TimecodeEdit(allow_empty=True)
        self.duration_edit.setPlaceholderText("optional")
        self.duration_edit.setToolTip("How long the event lasted")
        mark_end_button = QPushButton("Use current")
        mark_end_button.setToolTip(
            "Set the duration from where the player is now, i.e. mark the end of the event"
        )
        mark_end_button.clicked.connect(self._use_current_as_end)
        self.duration_hint = QLabel("")
        self.duration_hint.setObjectName("hintLabel")

        duration_row = QHBoxLayout()
        duration_row.setSpacing(6)
        duration_row.addWidget(self.duration_edit)
        duration_row.addWidget(mark_end_button)
        duration_row.addSpacing(8)
        duration_row.addWidget(self.duration_hint)
        duration_row.addStretch(1)
        form.addRow(field_label("Event Duration"), self._wrap(duration_row))
        self.duration_edit.value_changed.connect(lambda _v: self._refresh_duration_hint())

        # 5. Everything else the template asks ---------------------------- #
        for field in self._template.ordered_fields:
            if field is location:
                continue  # already placed above
            self._add_field_row(form, field)

        # 6 & 7. Investigator and Date ------------------------------------ #
        self.investigator_edit = QLineEdit()
        self.date_reviewed_edit = QDateEdit()
        self.date_reviewed_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_reviewed_edit.setCalendarPopup(True)
        self.date_reviewed_edit.setMaximumWidth(150)
        # A review cannot have happened tomorrow, and this date goes into the
        # export as a fact about when the evidence was looked at.
        self.date_reviewed_edit.setMaximumDate(QDate.currentDate())

        who_row = QHBoxLayout()
        who_row.setSpacing(10)
        who_row.addWidget(self.investigator_edit, 1)
        reviewed_label = QLabel("Date Reviewed")
        reviewed_label.setObjectName("fieldLabel")
        who_row.addWidget(reviewed_label)
        who_row.addWidget(self.date_reviewed_edit)
        form.addRow(field_label("Investigator Name"), self._wrap(who_row))

        # -- side panel: snapshot + provenance ---------------------------- #
        body.addWidget(self._build_side_panel())

        # -- validation + buttons ----------------------------------------- #
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color:#e5484d; font-weight:600;")
        self.problem_label.hide()
        outer.addWidget(self.problem_label)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save entry", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setDefault(True)
        self.save_button.setToolTip("Ctrl+Enter")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _has_unsaved_work(self) -> bool:
        """Whether closing now would throw away something the reviewer typed.

        Only prose counts. Everything else was prefilled or is a click, so losing it
        costs a keystroke rather than an observation -- and prompting about a combo
        box the reviewer nudged would train them to dismiss the prompt that matters.

        Compared against the baseline captured after prefill, so a guessed location
        is not mistaken for typing.
        """
        current = self._collect_values()
        prose = {field_id for field_id, editor in self._editors.items() if editor.is_prose}
        return any(current.get(field_id) != self._baseline.get(field_id) for field_id in prose)

    def reject(self) -> None:
        """Esc, Cancel and the window X all land here.

        The dialog advertises Esc on its own Cancel button, and the modal is opened
        the instant the reviewer presses LOG, so the one thing it must not do is
        silently bin a written-up observation - which also deletes the captured
        frame on the way out.
        """
        if self._has_unsaved_work():
            answer = QMessageBox.question(
                self,
                "Discard this observation?",
                "What you have written here has not been saved, and closing will "
                "discard it.\n\nDiscard it?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Discard:
                return
        super().reject()

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        caption = QLabel("Captured frame")
        caption.setObjectName("fieldLabel")
        layout.addWidget(caption)

        self.snapshot_label = QLabel()
        self.snapshot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.snapshot_label.setMinimumHeight(150)
        self.snapshot_label.setFrameShape(QFrame.Shape.StyledPanel)
        # Styled by QLabel#snapshotWell so it follows the theme. Inline styles
        # survive apply_theme clearing the stylesheet, which is how the "system"
        # theme ended up with dark rectangles painted on a light window.
        self.snapshot_label.setObjectName("snapshotWell")
        self.snapshot_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.snapshot_label.mousePressEvent = self._open_snapshot  # type: ignore[method-assign]
        layout.addWidget(self.snapshot_label)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        layout.addWidget(divider)

        self.details_toggle = QToolButton()
        self.details_toggle.setText("▸  Provenance details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setStyleSheet("font-weight:600;")
        self.details_toggle.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_toggle)

        self.details_widget = QWidget()
        self.details_widget.setVisible(False)
        grid = QGridLayout(self.details_widget)
        grid.setContentsMargins(2, 4, 2, 4)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        self._detail_values: dict[str, QLabel] = {}
        for row, key in enumerate(
            ("Entry ID", "Case", "Media kind", "SHA-256", "Machine", "App version")
        ):
            name = QLabel(key)
            name.setObjectName("hintLabel")
            value = QLabel(NO_VALUE)
            value.setObjectName("hintLabel")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self._detail_values[key] = value
        layout.addWidget(self.details_widget)

        layout.addStretch(1)
        return panel

    @staticmethod
    def _wrap(layout) -> QWidget:  # any QLayout
        container = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        container.setLayout(layout)
        return container

    def _wire_shortcuts(self) -> None:
        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self._on_save)

    # ------------------------------------------------------------------ #
    # Prefill
    # ------------------------------------------------------------------ #

    def _populate(self, existing: EntryRow | None) -> None:
        context = self._context
        review = self._settings.review

        self.file_name_edit.setText(context.file_name)
        self.file_name_edit.setToolTip(str(context.path))

        # Every field starts at whatever the template says, then is overwritten by
        # the entry being edited. Doing it in that order means a field added to the
        # template since an entry was written still gets its default.
        for field_id, value in self._template.default_values().items():
            editor = self._editors.get(field_id)
            if editor is not None:
                editor.set_value(value)

        if existing is not None:
            for field_id, editor in self._editors.items():
                editor.set_value(existing.values.get(field_id))
        else:
            self._prefill_location()

        # Event position and duration.
        offset = existing.event_offset_seconds if existing else self._offset
        self.offset_edit.set_seconds(offset)
        self.duration_edit.set_seconds(
            existing.event_duration_seconds if existing else self._duration
        )

        media_length = (
            existing.media_duration_seconds if existing else context.duration_seconds
        ) or 0.0
        self.media_length_label.setText(
            format_timecode(media_length, millis=False) if media_length else NO_VALUE
        )

        if context.kind == "image":
            # A still has no timeline, so neither field applies.
            for widget in (self.offset_edit, self.duration_edit):
                widget.setEnabled(False)
                widget.clear()
                widget.setPlaceholderText(NO_VALUE)

        if existing is not None:
            self.investigator_edit.setText(existing.investigator_name)
            self.date_reviewed_edit.setDate(
                QDate(
                    existing.date_reviewed.year,
                    existing.date_reviewed.month,
                    existing.date_reviewed.day,
                )
            )
            self._snapshot_path = existing.snapshot_path or self._snapshot_path
        else:
            self.investigator_edit.setText(review.investigator_name)
            today = _dt.date.today()
            self.date_reviewed_edit.setDate(QDate(today.year, today.month, today.day))

        self._baseline = self._collect_values()
        self._refresh_required_marks()
        self._refresh_duration_hint()
        self._load_snapshot_preview()
        self._refresh_details()

    def _prefill_location(self) -> None:
        """Guess the location field for a new entry: folder memory, then the path.

        Addressed by role, so this works for a template that calls it Area and one
        that calls it Location / Camera, and does nothing at all for a template with
        no location field.
        """
        field = self._template.location_field
        editor = self._editors.get(field.field_id) if field else None
        if field is None or editor is None:
            return
        guess = self._store.recall_area_for_folder(self._context.folder) or ""
        if not guess and self._settings.review.guess_area_from_path:
            guess = guess_area(self._context.path)
        if guess:
            editor.set_value(guess)

    def _suggestions_for(self, field) -> list[str]:
        """Past values offered as completions for a history-backed field.

        Scoped to this case: areas are the rooms of one building, and offering every
        area from every case makes the list useless once a few are in play.
        """
        known = self._store.distinct_field_values(field.field_id, self._case_id)
        if field.role is FieldRole.LOCATION:
            # The configured area list still seeds the location vocabulary, so a
            # reviewer who set one up in Settings keeps it.
            return list(dict.fromkeys([*self._settings.review.areas, *known]))
        return known

    def _add_field_row(self, form: QFormLayout, field: TemplateField) -> None:
        editor = FieldEditor(field, suggestions=self._suggestions_for(field))
        editor.value_changed.connect(self._refresh_required_marks)
        self._editors[field.field_id] = editor
        label = field_label(_wrap_label(field.label))
        self._field_labels[field.field_id] = label
        form.addRow(label, editor)

    def _collect_values(self) -> dict:
        """What the form currently holds, in each field's own type."""
        return self._template.coerce_values(
            {field_id: editor.value() for field_id, editor in self._editors.items()}
        )

    def _refresh_required_marks(self) -> None:
        """Mark fields a rule has just made required, as the reviewer types.

        The paranormal rule -- a debunked entry needs a reason -- used to be wired
        directly to the status combo. It now comes from the template's rules, so a
        template with three conditional fields gets the same treatment.
        """
        values = self._collect_values()
        required = self._template.required_field_ids(values)
        for field_id, editor in self._editors.items():
            label = self._field_labels.get(field_id)
            if label is None:
                continue
            is_required = field_id in required
            label.setText(_wrap_label(editor.field.label) + (" *" if is_required else ""))

    def _focus_first_field(self) -> None:
        """Start on the field the reviewer is here to write: the summary.

        Falling back to the first editor, because a template is allowed to have no
        summary field even though both built-ins do.
        """
        summary = self._template.summary_field
        editor = self._editors.get(summary.field_id) if summary else None
        if editor is None and self._editors:
            editor = next(iter(self._editors.values()))
        if editor is not None:
            editor.focus()

    def _case_label(self) -> str:
        """The case this entry belongs to, which is not always the current one.

        Editing keeps an entry in the case it was recorded in, so showing the
        configured case name here would misreport where the entry actually lives.
        Only the id travels with an entry, so that is what an older case shows.
        """
        review = self._settings.review
        if self._existing is not None and self._existing.case_id != review.case_id:
            return self._existing.case_id
        return review.case_name

    def _refresh_details(self) -> None:
        entry_id = self._existing.entry_id if self._existing else "(assigned on save)"
        digest = self._context.sha256
        self._detail_values["Entry ID"].setText(entry_id)
        self._detail_values["Case"].setText(self._case_label())
        self._detail_values["Media kind"].setText(self._context.kind)
        self._detail_values["SHA-256"].setText(
            digest
            if digest
            else ("computing…" if self._settings.review.compute_media_hash else "disabled")
        )
        self._detail_values["Machine"].setText(machine_name())
        self._detail_values["App version"].setText(APP_VERSION)

    def set_media_hash(self, digest: str) -> None:
        """Called when the background hasher finishes while the modal is open."""
        self._context.sha256 = digest
        self._detail_values["SHA-256"].setText(digest)

    def _load_snapshot_preview(self) -> None:
        if not self._snapshot_path or not Path(self._snapshot_path).is_file():
            # "No frame captured" read the same in three different situations, so
            # a reviewer who had ticked the box could not tell whether capture had
            # failed, been switched off, or succeeded and then lost the file.
            if self._snapshot_path:
                self.snapshot_label.setText(
                    "The captured frame is missing from disk.\nThe entry is unaffected."
                )
            elif self._settings.review.capture_snapshot_on_log:
                self.snapshot_label.setText(
                    "The frame could not be captured.\nThe entry is unaffected."
                )
            else:
                self.snapshot_label.setText(
                    "Frame capture is off.\nTurn it on in Settings → Review."
                )
            return
        pixmap = QPixmap(self._snapshot_path)
        if pixmap.isNull():
            self.snapshot_label.setText("Preview unavailable")
            return
        self.snapshot_label.setPixmap(
            pixmap.scaled(
                248,
                190,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.snapshot_label.setToolTip(f"{self._snapshot_path}\n(click to open)")

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #

    def _refresh_duration_hint(self) -> None:
        """Spell out the end point, so a duration is easy to sanity-check."""
        start = self.offset_edit.seconds()
        duration = self.duration_edit.seconds()
        if start is None or not duration:
            self.duration_hint.setText("")
            return
        self.duration_hint.setText(f"ends at {format_timecode(start + duration)}")

    def _player(self):
        """The main window, when this dialog was opened from it."""
        parent = self.parent()
        return parent if hasattr(parent, "current_playback_position") else None

    def _seek_to_offset(self) -> None:
        seconds = self.offset_edit.seconds()
        player = self._player()
        if seconds is None or player is None:
            return
        player.seek_to(seconds)

    def _use_current_as_end(self) -> None:
        player = self._player()
        position = player.current_playback_position() if player else None
        start = self.offset_edit.seconds()

        if position is None or start is None:
            QMessageBox.information(
                self, "No position", "The player does not have a current position to use."
            )
            return
        if position <= start:
            QMessageBox.information(
                self,
                "Move the player forward",
                "The player is at or before the start of the event, so there is no "
                "duration to measure. Let it play past the event, then try again.",
            )
            return
        self.duration_edit.set_seconds(position - start)
        self._refresh_duration_hint()

    def _copy_path(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(str(self._context.path))

    def _open_snapshot(self, _event) -> None:  # Qt event
        if self._snapshot_path and Path(self._snapshot_path).is_file():
            QDesktopServices.openUrl(Path(self._snapshot_path).absolute().as_uri())

    def _toggle_details(self, expanded: bool) -> None:
        self.details_widget.setVisible(expanded)
        self.details_toggle.setText(("▾" if expanded else "▸") + "  Provenance details")

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #

    def _timecode_problems(self) -> list[str]:
        """Catch text that looks like a timecode but did not parse.

        TimecodeEdit.seconds() returns None for both empty and unparseable input,
        so without this a typo like "1:2x:03" would be silently discarded and the
        entry saved with no duration and no warning.
        """
        problems: list[str] = []
        for field, label in (
            (self.offset_edit, "Event Timestamp"),
            (self.duration_edit, "Event Duration"),
        ):
            if field.isEnabled() and field.text().strip() and field.seconds() is None:
                problems.append(
                    f"'{label}' is not a valid timecode. Use HH:MM:SS.mmm, MM:SS, or seconds."
                )
        return problems

    def _build_entry(self) -> EntryRow:
        context = self._context
        now = utc_now()
        is_image = context.kind == "image"

        base = {
            "case_id": self._settings.review.case_id,
            "template_id": self._template.template_id,
            "template_version": self._template.version,
            "values": self._collect_values(),
            "file_name": context.file_name,
            "media_path": str(context.path),
            "media_sha256": context.sha256,
            "media_kind": MediaKind(context.kind),
            "event_offset_seconds": None if is_image else self.offset_edit.seconds(),
            "event_duration_seconds": None if is_image else self.duration_edit.seconds(),
            "media_duration_seconds": context.duration_seconds,
            "investigator_name": self.investigator_edit.text().strip(),
            "date_reviewed": self.date_reviewed_edit.date().toPython(),
            "snapshot_path": self._snapshot_path,
            "app_version": APP_VERSION,
            "machine_name": machine_name(),
            "updated_at_utc": now,
            "sync_state": SyncState.PENDING,
            "sync_error": None,
        }

        if self._existing is not None:
            return EntryRow.model_validate(
                {
                    **self._existing.model_dump(),
                    **base,
                    "entry_id": self._existing.entry_id,
                    # An observation belongs to the case it was recorded in. `base`
                    # carries the Case ID from current settings, so without this the
                    # edit would move the entry into whatever case the reviewer
                    # happens to be working on now.
                    "case_id": self._existing.case_id,
                    "created_at_utc": self._existing.created_at_utc,
                    "server_id": self._existing.server_id,
                }
            )
        # Only on a new entry. An edit keeps whatever was being heard when the
        # observation was first written, because that is what the record is about --
        # stamping the current setting would rewrite history every time somebody fixed
        # a typo with different filters switched on.
        return EntryRow.model_validate(
            {**base, "created_at_utc": now, "audio_filters": self._audio_filters}
        )

    def _show_problems(self, problems: list[str]) -> None:
        self.problem_label.setText("\n".join(f"• {problem}" for problem in problems))
        self.problem_label.show()

    def _on_save(self) -> None:
        timecode_problems = self._timecode_problems()
        if timecode_problems:
            self._show_problems(timecode_problems)
            self.offset_edit.setFocus()
            return

        entry = self._build_entry()
        problems = entry.validation_problems(self._template)
        if problems:
            self._show_problems(problems)
            self._focus_first_problem(entry)
            return

        self.problem_label.hide()
        self._entry = entry
        # Remember the location for this folder so the next file in it prefills.
        location = entry.value_for_role(self._template, FieldRole.LOCATION)
        if location:
            self._store.remember_area_for_folder(self._context.folder, str(location))
        self.accept()

    def _focus_first_problem(self, entry: EntryRow) -> None:
        """Put the cursor in the first field that is actually wrong.

        Walks the form in its own order rather than testing fields by name, so it
        lands on the right one whatever the template asks.
        """
        required = self._template.required_field_ids(entry.values)
        for field in self._template.ordered_fields:
            editor = self._editors.get(field.field_id)
            if editor is None:
                continue
            value = entry.values.get(field.field_id)
            empty = value is None or (isinstance(value, str) and not value.strip())
            if (field.field_id in required and empty) or field.problems(value):
                editor.focus()
                return

    @property
    def template(self) -> LogTemplate:
        """The form this dialog is showing."""
        return self._template

    def editor_for(self, field_id: str) -> FieldEditor | None:
        """The editor for one field, addressed by id rather than by attribute name.

        There is no ``observation_edit`` any more: which widgets exist depends on the
        template, so anything reaching into the form has to ask for a field.
        """
        return self._editors.get(field_id)

    @property
    def entry(self) -> EntryRow | None:
        """The saved entry, or ``None`` if the dialog was cancelled."""
        return self._entry

    @property
    def snapshot_path(self) -> str | None:
        return self._snapshot_path
