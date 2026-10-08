"""The whole of one log entry, on one screen.

The session log shows a handful of columns because it has to stay scannable, which
means most of what an entry holds is not on screen -- and on a template with nine
fields, most of the record is invisible. This is where the rest of it lives: every
answer under the label it was asked by, the position in the media, the captured frame,
and the provenance a reader needs to trust the row.

Editing happens from here too, and the dialog reloads itself afterwards, so the
reviewer sees the change they just made rather than being dropped back to the list to
go and look for it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..autofill import MediaContext
from ..config import Settings
from ..models import EntryRow
from ..store import LocalStore
from ..templates import LogTemplate
from ..util import NO_VALUE, format_timecode, to_iso
from .log_dialog import LogEntryDialog
from .log_dock import SYNC_GLYPHS
from .theme import TEXT_MUTED, status_colour
from .widgets import field_label, fit_to_screen, hint_label, scrollable

log = logging.getLogger(__name__)


class EntryDetailDialog(QDialog):
    """Read-only view of one entry, with a way into the editor.

    ``commit`` is the caller's save path rather than a direct store write, so an edit
    made here goes through exactly the same steps as one made anywhere else: written
    locally, the log refreshed, the sync worker nudged.
    """

    goto_requested = Signal(object)  # EntryRow
    clip_requested = Signal(object)
    delete_requested = Signal(object)
    restore_requested = Signal(object)

    def __init__(
        self,
        entry: EntryRow,
        *,
        store: LocalStore,
        settings: Settings,
        commit: Callable[[EntryRow], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._store = store
        self._settings = settings
        self._commit = commit
        self._edited = False

        self.setWindowTitle(f"Entry — {entry.file_name}")
        self.setModal(True)
        self.setMinimumWidth(720)
        self.setSizeGripEnabled(True)

        self._build_ui()
        self._load()
        fit_to_screen(self)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet("font-size:15px; font-weight:600;")
        outer.addWidget(self.heading)

        self.subheading = hint_label("")
        outer.addWidget(self.subheading)

        body_container = QWidget()
        body = QHBoxLayout(body_container)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(18)
        outer.addWidget(scrollable(body_container), 1)

        # The answers, which are the point of the dialog, get the room.
        self._answers_panel = QWidget()
        self._answers = QVBoxLayout(self._answers_panel)
        self._answers.setContentsMargins(0, 0, 0, 0)
        self._answers.setSpacing(12)
        body.addWidget(self._answers_panel, 1)

        side = QWidget()
        side.setMaximumWidth(260)
        self._side = QVBoxLayout(side)
        self._side.setContentsMargins(0, 0, 0, 0)
        self._side.setSpacing(12)
        body.addWidget(side)

        # Laid out by hand. A QDialogButtonBox orders by role, which here put Close
        # between the two actions -- so the thing that dismisses the dialog sat in the
        # middle of the things that do something.
        buttons = QHBoxLayout()

        self.delete_button = QPushButton("Delete entry")
        self.delete_button.setToolTip(
            "Flag this entry as deleted. It is kept, not erased, and can be put back."
        )
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)

        self.restore_button = QPushButton("Restore entry")
        self.restore_button.setToolTip("Put this entry back into the log")
        self.restore_button.clicked.connect(self._restore)
        buttons.addWidget(self.restore_button)

        buttons.addStretch(1)

        self.clip_button = QPushButton("Export clip…")
        self.clip_button.setToolTip(
            "Cut this event out of the recording as a file of its own"
        )
        self.clip_button.clicked.connect(lambda: self.clip_requested.emit(self._entry))
        buttons.addWidget(self.clip_button)

        self.goto_button = QPushButton("Go to this moment")
        self.goto_button.setToolTip("Move the player to where this event is")
        self.goto_button.clicked.connect(self._goto)
        buttons.addWidget(self.goto_button)

        self.edit_button = QPushButton("Edit entry…")
        self.edit_button.setToolTip("Change what was recorded")
        self.edit_button.clicked.connect(self._edit)
        buttons.addWidget(self.edit_button)

        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        outer.addLayout(buttons)

    # ------------------------------------------------------------------ #
    # Content
    # ------------------------------------------------------------------ #

    def _clear(self, layout: QVBoxLayout) -> None:
        """Empty a column, now rather than whenever the event loop gets round to it.

        deleteLater() alone leaves each widget parented and painted until the loop
        runs, so a reload would briefly show the old answers under the new ones.
        """
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _load(self) -> None:
        """Rebuild the whole dialog from the entry, so a reload needs no bookkeeping."""
        entry = self._entry
        template = self._store.get_template(entry.template_id)

        self._clear(self._answers)
        self._clear(self._side)

        summary = template.summarise(entry.values) if template else ""
        self.heading.setText(summary or entry.file_name)

        parts = [entry.file_name]
        if entry.event_offset_seconds is not None:
            span = format_timecode(entry.event_offset_seconds, millis=False)
            if entry.event_duration_seconds:
                span += f" for {format_timecode(entry.event_duration_seconds, millis=False)}"
            parts.append(span)
        if entry.is_deleted:
            parts.append("deleted")
        self.subheading.setText("  ·  ".join(parts))

        self._add_answers(entry, template)
        self._add_position(entry)
        self._add_provenance(entry, template)
        self._add_snapshot(entry)

        self.goto_button.setEnabled(entry.event_offset_seconds is not None)
        # A still image has no span to cut, and an entry with no position has nowhere
        # to cut from.
        self.clip_button.setEnabled(
            entry.event_offset_seconds is not None and entry.media_kind.value != "image"
        )
        self.edit_button.setEnabled(not entry.is_deleted)
        if entry.is_deleted:
            self.edit_button.setToolTip("Restore this entry before editing it")
        self.delete_button.setVisible(not entry.is_deleted)
        self.restore_button.setVisible(entry.is_deleted)

    def _add_answers(self, entry: EntryRow, template: LogTemplate | None) -> None:
        self._answers.addWidget(field_label("Recorded"))

        shown = set()
        if template is not None:
            status_field = template.status_field
            for field in template.ordered_fields:
                shown.add(field.field_id)
                value = field.display(entry.values.get(field.field_id))
                if not value:
                    continue
                colour = None
                if field is status_field:
                    option = field.option_for(entry.values.get(field.field_id))
                    colour = (option.colour if option and option.colour else None) or status_colour(
                        value
                    )
                self._answers.addWidget(_Answer(field.label, value, colour=colour))

        # Anything this client has no field for. It is a colleague's answer on a
        # template version not pulled yet, and showing it under its raw id is better
        # than a reader never knowing it is there.
        for field_id, value in entry.values.items():
            if field_id in shown or value in (None, ""):
                continue
            self._answers.addWidget(
                _Answer(f"{field_id}", str(value), hint="field not known to this client")
            )

        if not any(
            isinstance(self._answers.itemAt(index).widget(), _Answer)
            for index in range(self._answers.count())
        ):
            self._answers.addWidget(hint_label("Nothing was filled in on this entry."))

        self._answers.addStretch(1)

    def _add_position(self, entry: EntryRow) -> None:
        rows: list[tuple[str, str]] = [
            (
                "Event Timestamp",
                format_timecode(entry.event_offset_seconds)
                if entry.event_offset_seconds is not None
                else NO_VALUE,
            ),
            (
                "Event Duration",
                format_timecode(entry.event_duration_seconds)
                if entry.event_duration_seconds
                else NO_VALUE,
            ),
            (
                "Ends at",
                format_timecode(entry.event_end_seconds)
                if entry.event_end_seconds is not None
                else NO_VALUE,
            ),
            (
                "Media length",
                format_timecode(entry.media_duration_seconds, millis=False)
                if entry.media_duration_seconds
                else NO_VALUE,
            ),
        ]
        self._side.addWidget(_Section("Where in the media", rows, mono=True))

    def _add_provenance(self, entry: EntryRow, template: LogTemplate | None) -> None:
        glyph = SYNC_GLYPHS.get(entry.sync_state)
        sync = glyph[2] if glyph else entry.sync_state.value
        if entry.sync_error:
            sync += f" — {entry.sync_error}"

        rows: list[tuple[str, str]] = [
            ("Investigator", entry.investigator_name or NO_VALUE),
            ("Date reviewed", entry.date_reviewed.isoformat()),
            ("Case", entry.case_id),
            (
                "Form",
                f"{template.name} v{entry.template_version}" if template else entry.template_id,
            ),
            ("Logged", to_iso(entry.created_at_utc) or NO_VALUE),
            ("Last changed", to_iso(entry.updated_at_utc) or NO_VALUE),
            ("Sync", sync),
        ]
        self._side.addWidget(_Section("Provenance", rows))

        rows = [
            ("File", entry.media_path or entry.file_name),
            ("SHA-256", entry.media_sha256 or NO_VALUE),
            ("Entry ID", entry.entry_id),
            (
                "Logged with",
                " ".join(filter(None, [entry.app_version, entry.machine_name])) or NO_VALUE,
            ),
        ]
        self._side.addWidget(_Section("The media file", rows, mono=True))
        self._side.addStretch(1)

    def _add_snapshot(self, entry: EntryRow) -> None:
        if not entry.snapshot_path or not Path(entry.snapshot_path).is_file():
            return
        pixmap = QPixmap(entry.snapshot_path)
        if pixmap.isNull():
            return

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(field_label("Captured frame"))

        # The marks drawn on this frame, drawn over it here too -- by the same
        # renderer the player and the export use. Until 2026-10-07 the entry view
        # showed the bare frame, and the marks could only be seen over the video.
        marks = self._store.annotations_for(entry.entry_id)
        scaled = pixmap.scaledToWidth(240, Qt.TransformationMode.SmoothTransformation)
        if marks:
            from PySide6.QtGui import QPainter

            from ..annotations import render

            overlay = render(marks, scaled.width(), scaled.height())
            painter = QPainter(scaled)
            painter.drawImage(0, 0, overlay)
            painter.end()

        preview = QLabel()
        preview.setPixmap(scaled)
        preview.setCursor(Qt.CursorShape.PointingHandCursor)
        preview.setToolTip("Open the full-size frame")
        preview.mousePressEvent = lambda _event: QDesktopServices.openUrl(
            Path(entry.snapshot_path).absolute().as_uri()
        )
        layout.addWidget(preview)
        for mark in marks:
            import html

            words = f": {html.escape(mark.text)}" if mark.text else ""
            line = QLabel(f"<span style='color:{html.escape(mark.colour)}'>&#9632;</span> "
                          f"{mark.kind.value.title()}{words}")
            line.setWordWrap(True)
            layout.addWidget(line)
        # Inserted at the top of the side column: a picture of the moment is the
        # fastest way to recognise which entry this is.
        self._side.insertWidget(0, panel)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _edit(self) -> None:
        entry = self._entry
        context = MediaContext(
            path=Path(entry.media_path),
            kind=entry.media_kind.value,
            duration_seconds=entry.media_duration_seconds,
            sha256=entry.media_sha256,
        )
        dialog = LogEntryDialog(
            context=context,
            settings=self._settings,
            store=self._store,
            event_offset_seconds=entry.event_offset_seconds,
            snapshot_path=entry.snapshot_path,
            existing=entry,
            parent=self,
        )
        if dialog.exec() != LogEntryDialog.DialogCode.Accepted or dialog.entry is None:
            return

        self._commit(dialog.entry)
        self._edited = True
        # Re-read rather than trusting the dialog's copy: the store coerces values and
        # bumps the revision, so what it holds is the record and this is not.
        reloaded = self._store.get(dialog.entry.entry_id)
        self._entry = reloaded or dialog.entry
        self._load()

    def _delete(self) -> None:
        """Ask to delete, and close only if it was actually deleted.

        The dialog cannot know the answer itself: the window owns the confirmation, so
        it reports back whether the entry went. Closing regardless would make a
        cancelled confirmation look like it had worked.
        """
        self.delete_requested.emit(self._entry)
        reloaded = self._store.get(self._entry.entry_id)
        if reloaded is not None and reloaded.is_deleted:
            self._entry = reloaded
            self._edited = True
            self.accept()

    def _restore(self) -> None:
        self.restore_requested.emit(self._entry)
        reloaded = self._store.get(self._entry.entry_id)
        if reloaded is not None:
            self._entry = reloaded
            self._edited = True
            self._load()

    def _goto(self) -> None:
        self.goto_requested.emit(self._entry)
        self.accept()

    @property
    def entry(self) -> EntryRow:
        """The entry as it now stands, which an edit may have changed."""
        return self._entry

    @property
    def was_edited(self) -> bool:
        return self._edited


def _sized_font(widget: QWidget, pixels: int) -> QFont:
    """The widget's font at a given size.

    Set on the font rather than through a stylesheet, because a stylesheet size never
    reaches fontMetrics(): the widget then tells the layout it needs the default
    size's width, and a value too long for its column is silently clipped instead of
    wrapped.
    """
    font = QFont(widget.font())
    font.setPixelSize(pixels)
    return font


class _Answer(QFrame):
    """One answer: its label, then its value, wrapped if it is long."""

    def __init__(
        self,
        label: str,
        value: str,
        *,
        colour: str | None = None,
        hint: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        caption = QLabel(label)
        caption.setFont(_sized_font(caption, 11))
        caption.setWordWrap(True)
        caption.setStyleSheet(f"color:{TEXT_MUTED};")
        layout.addWidget(caption)

        text = QLabel(value)
        text.setFont(_sized_font(text, 13))
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if colour:
            text.setStyleSheet(f"color:{colour}; font-weight:600;")
        layout.addWidget(text)

        if hint:
            layout.addWidget(hint_label(hint))


class _Section(QFrame):
    """A titled block of label/value pairs, for the narrow side column."""

    def __init__(
        self,
        title: str,
        rows: list[tuple[str, str]],
        *,
        mono: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(field_label(title))

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)

        for row, (name, value) in enumerate(rows):
            caption = QLabel(name)
            caption.setFont(_sized_font(caption, 11))
            caption.setWordWrap(True)
            caption.setStyleSheet(f"color:{TEXT_MUTED};")
            grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)

            text = QLabel(value)
            font = _sized_font(text, 11)
            if mono:
                font.setFamily("Consolas")
                font.setStyleHint(font.StyleHint.Monospace)
            text.setFont(font)
            # Always. A SHA-256 wants hundreds of pixels in a column a third that
            # wide, and without this it was simply chopped off -- the one thing a
            # provenance panel cannot do, since checking a hash means reading all of
            # it.
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(text, row, 1)

        layout.addLayout(grid)
