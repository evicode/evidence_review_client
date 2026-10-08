"""The session log: every entry for the current case, and a way to navigate back to it."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..models import EntryRow, SyncState
from ..templates import FieldRole, FieldType, LogTemplate, TemplateField
from ..util import NO_VALUE, format_timecode
from .icons import eye_icon
from .theme import TEXT_MUTED, status_colour

#: Per-row sync state. These sit a few centimetres from the status bar's own
#: indicator, so they must not reuse its glyphs for different meanings: the bar
#: uses ↻ for "a cycle is running right now", which this column once used for
#: "this row has not been uploaded". A row that is waiting is now an open circle,
#: matching the bar's ○ for "nothing is going out".
SYNC_GLYPHS = {
    SyncState.SYNCED: ("●", "#3fb950", "Synced to the server"),
    SyncState.PENDING: ("○", "#e3a008", "Waiting to upload"),
    SyncState.ERROR: ("✕", "#e5484d", "Upload failed - will retry"),
    SyncState.CONFLICT: ("⚠", "#bc8cff", "Conflicting server version"),
}


class Column:
    """One column of the log table.

    ``field`` is set for the columns a template contributes; the rest are the
    universal ones, which every template has and none can remove.
    """

    __slots__ = ("field", "kind", "title", "width")

    def __init__(self, title: str, width: int, kind: str, field: TemplateField | None = None):
        self.title = title
        self.width = width
        self.kind = kind
        self.field = field


#: The universal columns, which exist whatever the template says.
_LEADING_COLUMNS = (
    Column("", 30, "view"),
    Column("", 28, "sync"),
    Column("Event Timestamp", 100, "offset"),
    Column("Duration", 80, "duration"),
)
_TRAILING_COLUMNS = (
    Column("File", 150, "file"),
    Column("Investigator", 120, "investigator"),
)


def columns_for(template: LogTemplate | None) -> list[Column]:
    """Build the table's columns from a template.

    Ordered: the universal position columns, then whichever fields the template
    marks for the table, then the summary field as the wide one, then the file and
    reviewer. No column name is hardcoded, so a template nobody has written yet
    gets a sensible table for free.
    """
    columns = list(_LEADING_COLUMNS)
    if template is not None:
        summary = template.summary_field
        for field in template.ordered_fields:
            if not field.in_table or field is summary:
                continue
            width = 120 if field.role is FieldRole.STATUS else 110
            columns.append(Column(field.label, width, "field", field))
        if summary is not None:
            columns.append(Column(summary.label, 320, "field", summary))
    return columns + list(_TRAILING_COLUMNS)


class LogTableModel(QAbstractTableModel):
    """Read-only table over the current case's entries."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[EntryRow] = []
        self._template: LogTemplate | None = None
        #: Every template in play, so an entry written on a different one than the
        #: case currently uses still renders from its own definition.
        self._templates: dict[str, LogTemplate] = {}
        self._columns: list[Column] = columns_for(None)

    @property
    def columns(self) -> list[Column]:
        return self._columns

    # -- Qt model interface -------------------------------------------------- #

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._columns)

    def headerData(  # noqa: N802 - Qt naming
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ):
        header = orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole
        if header and 0 <= section < len(self._columns):
            return self._columns[section].title
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        column = self._column(index.column())
        if column is None:
            return None
        is_status = column.field is not None and column.field.role is FieldRole.STATUS

        if role == Qt.ItemDataRole.DecorationRole and column.kind == "view":
            return eye_icon(TEXT_MUTED)

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(entry, column)

        if role == Qt.ItemDataRole.FontRole and entry.is_deleted:
            font = QFont()
            font.setStrikeOut(True)
            font.setItalic(True)
            return font

        if role == Qt.ItemDataRole.ForegroundRole:
            if entry.is_deleted:
                # Struck through and dimmed, so a deleted row cannot be misread as a
                # live one when the toggle is on.
                return QColor(TEXT_MUTED)
            if column.kind == "sync":
                return QColor(SYNC_GLYPHS.get(entry.sync_state, ("", TEXT_MUTED, ""))[1])
            if is_status:
                return QColor(self._status_colour(entry, column.field))
            # Any choice field, not only the status one. The editor offers a colour on
            # every choice's options, so a colour set on one that is not the status
            # field has to do something -- otherwise the setting is a lie.
            colour = self._option_colour(entry, column.field)
            return QColor(colour) if colour else None

        if role == Qt.ItemDataRole.FontRole and is_status:
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.ToolTipRole:
            if entry.is_deleted:
                return "Deleted. The record is kept; right-click to put it back."
            return self._tooltip(entry, column)

        if role == Qt.ItemDataRole.TextAlignmentRole and column.kind in (
            "view",
            "sync",
            "offset",
        ):
            return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.UserRole:
            return entry

        return None

    # -- rendering ----------------------------------------------------------- #

    def _column(self, index: int) -> Column | None:
        return self._columns[index] if 0 <= index < len(self._columns) else None

    def _template_for(self, entry: EntryRow) -> LogTemplate | None:
        return self._templates.get(entry.template_id) or self._template

    def _option_colour(self, entry: EntryRow, field: TemplateField | None) -> str | None:
        """The colour this entry's answer carries, when the field defines one."""
        if field is None or field.field_type is not FieldType.CHOICE:
            return None
        option = field.option_for(entry.values.get(field.field_id))
        return option.colour if option else None

    def _status_colour(self, entry: EntryRow, field: TemplateField | None) -> str:
        """The option's own colour, falling back to a hash for an unlisted value.

        A status the template does not list still has to be legible -- it may have
        been written before the option was removed, or on a newer version of the
        template than this client has pulled.
        """
        value = entry.values.get(field.field_id) if field else None
        text = "" if value is None else str(value)
        option = field.option_for(text) if field else None
        if option is not None and option.colour:
            return option.colour
        return status_colour(text)

    def _display(self, entry: EntryRow, column: Column) -> str:
        if column.kind == "view":
            return ""
        if column.kind == "sync":
            return SYNC_GLYPHS.get(entry.sync_state, ("?", "", ""))[0]
        if column.kind == "offset":
            if entry.event_offset_seconds is None:
                return NO_VALUE
            return format_timecode(entry.event_offset_seconds, millis=False)
        if column.kind == "duration":
            if not entry.event_duration_seconds:
                return NO_VALUE
            return format_timecode(entry.event_duration_seconds, millis=False)
        if column.kind == "file":
            return entry.file_name
        if column.kind == "investigator":
            return entry.investigator_name
        if column.field is not None:
            # Resolved against the entry's own template, so a field renamed since
            # it was written still shows the value it holds.
            field = column.field
            template = self._template_for(entry)
            if template is not None:
                field = template.field(column.field.field_id) or column.field
            return " ".join(field.display(entry.values.get(field.field_id)).split())
        return ""

    def _tooltip(self, entry: EntryRow, column: Column) -> str:
        if column.kind == "view":
            return "Show everything recorded for this entry"
        if column.kind == "sync":
            glyph = SYNC_GLYPHS.get(entry.sync_state)
            base = glyph[2] if glyph else entry.sync_state.value
            return f"{base}\n{entry.sync_error}" if entry.sync_error else base
        if column.kind == "offset" and entry.event_offset_seconds is not None:
            return "Where in the file this event is. Double-click to jump there."
        if column.kind == "duration" and entry.event_end_seconds is not None:
            return f"Ends at {format_timecode(entry.event_end_seconds)}"
        if column.kind == "file":
            return entry.media_path
        if column.field is not None and column.field.role is FieldRole.SUMMARY:
            template = self._template_for(entry)
            parts = [column.field.display(entry.values.get(column.field.field_id))]
            detail = template.field_for_role(FieldRole.DETAIL) if template else None
            if detail is not None:
                text = detail.display(entry.values.get(detail.field_id))
                if text:
                    parts.append(f"\n{detail.label}:\n{text}")
            return "\n".join(part for part in parts if part)
        if column.field is not None:
            value = column.field.display(entry.values.get(column.field.field_id))
            # Multi-line answers are squashed into the cell, so the tooltip is the
            # only place the reviewer can read all of one.
            return value if "\n" in value or len(value) > 40 else ""
        return ""

    # -- data management ----------------------------------------------------- #

    def set_template(self, template: LogTemplate | None, others: dict[str, LogTemplate]) -> None:
        """Rebuild the columns for a template. Resets the model."""
        self.beginResetModel()
        self._template = template
        self._templates = dict(others)
        self._columns = columns_for(template)
        self.endResetModel()

    def set_entries(self, entries: list[EntryRow]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def column_at(self, index: int) -> Column | None:
        return self._column(index)

    def summary_field_for(self, entry: EntryRow) -> TemplateField | None:
        template = self._template_for(entry)
        return template.summary_field if template else None

    def summary_text(self, entry: EntryRow) -> str:
        template = self._template_for(entry)
        return template.summarise(entry.values) if template else ""

    def entry_at(self, row: int) -> EntryRow | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    @property
    def entries(self) -> list[EntryRow]:
        return list(self._entries)


class LogDock(QDockWidget):
    """Dockable log panel. Double-clicking a row jumps the player to that moment."""

    entry_activated = Signal(object)  # EntryRow
    view_requested = Signal(object)
    edit_requested = Signal(object)
    delete_requested = Signal(object)
    restore_requested = Signal(object)
    clip_requested = Signal(object)
    annotate_requested = Signal(object)
    filter_changed = Signal(str)
    show_deleted_changed = Signal(bool)
    all_files_changed = Signal(bool)
    #: The full Cases and log window was asked for.
    full_log_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Session Log", parent)
        self.setObjectName("logDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter entries…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.filter_changed)
        filter_row.addWidget(self.search_edit, 1)
        # Without this a deletion simply made the row vanish, and the promise that
        # nothing is ever erased was something the reviewer had to take on faith.
        # The log used to show the whole case whatever was open, so every file showed
        # another file's work and the panel beside it -- the marks on the timeline --
        # showed none of it.
        self.all_files_check = QCheckBox("All files")
        self.all_files_check.setToolTip(
            "Show every entry in the case, not only the ones logged against the file "
            "that is open."
        )
        self.all_files_check.toggled.connect(self.all_files_changed)
        filter_row.addWidget(self.all_files_check)

        self.show_deleted_check = QCheckBox("Show deleted")
        self.show_deleted_check.setToolTip(
            "Deleted entries are kept, never erased. Turn this on to see them, and to put one back."
        )
        self.show_deleted_check.toggled.connect(self.show_deleted_changed)
        filter_row.addWidget(self.show_deleted_check)

        #: Whether what is shown is the open file's entries or the whole case.
        self._scoped_to_file = False

        self.count_label = QLabel("0 entries")
        self.count_label.setObjectName("hintLabel")
        filter_row.addWidget(self.count_label)
        # This strip is a quick view under the player. Everything else -- sorting,
        # filters, moving and changing many entries, every case -- is in the full
        # window, one click away.
        full = QPushButton("Open full log")
        full.setToolTip("Every case and entry in one window: sort, filter, move, change many at once")
        full.clicked.connect(self.full_log_requested)
        filter_row.addWidget(full)
        layout.addLayout(filter_row)

        self.model = LogTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.clicked.connect(self._on_clicked)
        self.table.doubleClicked.connect(self._on_double_clicked)

        self.table.horizontalHeader().setHighlightSections(False)
        self._apply_column_layout()

        layout.addWidget(self.table)
        self.setWidget(container)

    # -- API ----------------------------------------------------------------- #

    def set_template(self, template: LogTemplate | None, others: dict[str, LogTemplate]) -> None:
        """Rebuild the table for a template, then re-apply the column widths."""
        self.model.set_template(template, others)
        self._apply_column_layout()

    def _apply_column_layout(self) -> None:
        """Widths and resize modes, from the columns the template produced.

        The summary column stretches, because it holds the prose; the eye and the sync
        glyph are fixed, because each is one symbol wide and resizing it is pointless.
        """
        header = self.table.horizontalHeader()
        for index, column in enumerate(self.model.columns):
            self.table.setColumnWidth(index, column.width)
            if column.kind in ("view", "sync"):
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
            elif column.field is not None and column.field.role is FieldRole.SUMMARY:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)

    def set_entries(
        self, entries: list[EntryRow], *, deleted: int = 0, scoped_to_file: bool = False
    ) -> None:
        """Show these entries. ``deleted`` is how many the case is hiding, if any."""
        self._scoped_to_file = scoped_to_file
        self.model.set_entries(entries)
        count = len(entries)
        pending = sum(1 for entry in entries if entry.sync_state is not SyncState.SYNCED)
        showing_deleted = self.show_deleted_check.isChecked()

        # Said whether or not there is anything on screen: "deleted" with no way to see
        # what was deleted is the state that made people think the record had gone.
        hidden = ""
        if deleted and not showing_deleted:
            hidden = f"  ·  {deleted} deleted, hidden"

        if not count:
            # "0 entries" looked identical whether the case was empty or a filter
            # had hidden everything in it, and said nothing about how to add one.
            if self.search_edit.text().strip():
                self.count_label.setText("Nothing matches the filter" + hidden)
            elif hidden:
                self.count_label.setText(
                    f"No entries — {deleted} deleted. Tick Show deleted to see them."
                )
            elif self._scoped_to_file:
                # Otherwise this reads as "the case is empty", which is a different
                # and much more alarming claim than "nothing logged against this one".
                self.count_label.setText(
                    "Nothing logged against this file yet — press LOG, "
                    "or tick All files to see the rest of the case"
                )
            else:
                self.count_label.setText("No entries yet — press LOG to record one")
            return

        text = f"{count} {'entry' if count == 1 else 'entries'}"
        if self._scoped_to_file:
            text += " for this file"
        if pending:
            text += f"  ·  {pending} unsynced"
        if showing_deleted and deleted:
            text += f"  ·  {deleted} deleted"
        else:
            text += hidden
        self.count_label.setText(text)

    @property
    def show_deleted(self) -> bool:
        return self.show_deleted_check.isChecked()

    @property
    def all_files(self) -> bool:
        """Whether the whole case is being shown rather than the open file alone."""
        return self.all_files_check.isChecked()

    def selected_entry(self) -> EntryRow | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self.model.entry_at(indexes[0].row())

    # -- interaction --------------------------------------------------------- #

    def _on_clicked(self, index: QModelIndex) -> None:
        """A single click on the eye opens the entry. Every other column ignores it.

        One click, because the eye is a button drawn in a cell: asking for two on
        something that looks like a button is the kind of thing nobody finds.
        """
        column = self.model.column_at(index.column())
        if column is None or column.kind != "view":
            return
        entry = self.model.entry_at(index.row())
        if entry is not None:
            self.view_requested.emit(entry)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        # The eye has already acted on the first of the two clicks; jumping the player
        # as well would be two different things from one gesture.
        column = self.model.column_at(index.column())
        if column is not None and column.kind == "view":
            return
        entry = self.model.entry_at(index.row())
        if entry is not None:
            self.entry_activated.emit(entry)

    def _show_context_menu(self, position) -> None:  # QPoint
        index = self.table.indexAt(position)
        entry = self.model.entry_at(index.row()) if index.isValid() else None
        if entry is None:
            return
        self.context_menu_for(entry).exec(self.table.viewport().mapToGlobal(position))

    def context_menu_for(self, entry: EntryRow) -> QMenu:
        """Build the menu for one row, without showing it.

        Separate from showing it so what the menu offers can be checked without a
        modal: QMenu.exec blocks, and on a Qt type it cannot be patched out.
        """
        menu = QMenu(self)

        view = QAction("View full entry…", self)
        view.triggered.connect(lambda: self.view_requested.emit(entry))
        menu.addAction(view)

        jump = QAction("Go to this moment", self)
        jump.triggered.connect(lambda: self.entry_activated.emit(entry))
        menu.addAction(jump)

        edit = QAction("Edit entry…", self)
        edit.triggered.connect(lambda: self.edit_requested.emit(entry))
        menu.addAction(edit)

        if entry.event_offset_seconds is not None and entry.media_kind.value != "image":
            # Not for a recording: annotations are drawn on a frame and burned into a
            # picture, and audio has neither. Offering it led to a dead end -- the
            # frame capture fails and the reviewer gets a warning for trying something
            # the menu invited them to try.
            if entry.media_kind.value == "video":
                annotate = QAction("Annotate…", self)
                annotate.setToolTip("Draw text, arrows and circles over this event")
                annotate.triggered.connect(lambda: self.annotate_requested.emit(entry))
                menu.addAction(annotate)

            clip = QAction("Export clip…", self)
            clip.triggered.connect(lambda: self.clip_requested.emit(entry))
            menu.addAction(clip)

        if entry.snapshot_path and Path(entry.snapshot_path).is_file():
            snapshot = QAction("Open captured frame", self)
            snapshot.triggered.connect(
                lambda: QDesktopServices.openUrl(Path(entry.snapshot_path).absolute().as_uri())
            )
            menu.addAction(snapshot)

        menu.addSeparator()

        # Named after whatever the template calls its summary field, because
        # "Copy observation" is wrong on a template that has no observation.
        summary = self.model.summary_field_for(entry)
        label = summary.label if summary is not None else "summary"
        copy = QAction(f"Copy {label.lower()}", self)
        copy.triggered.connect(lambda: self._copy(self.model.summary_text(entry)))
        menu.addAction(copy)

        menu.addSeparator()

        if entry.is_deleted:
            restore = QAction("Restore entry", self)
            restore.triggered.connect(lambda: self.restore_requested.emit(entry))
            menu.addAction(restore)
        else:
            delete = QAction("Delete entry", self)
            delete.triggered.connect(lambda: self.delete_requested.emit(entry))
            menu.addAction(delete)

        return menu

    @staticmethod
    def _copy(text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
