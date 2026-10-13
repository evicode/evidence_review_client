"""Cases and log: the whole investigation in one window.

Built 2026-10-07. Until then the log was a strip under the player that showed one
file's entries at a time with no sorting and one search box, cases lived in a
status-bar dropdown, an entry could never change case, nothing could be done to
more than one entry at once, and the form editor hid in the File menu.

This window is the place to manage the work:

* every case on the left, with entry counts; new, rename, archive, delete; the
  form each case logs on; the form editor one click away
* the whole log of the chosen case (or every case) in a full-size table: click a
  heading to sort, filter by status, investigator and file, search any answer
* select one entry or many: open, edit, jump to the moment, move to another case,
  set status, delete, restore, export the selection
* the selected entry in full on the right: every answer, its frame, its marks

It holds no state of its own that matters: everything is read from the store and
written back through it, and the main window is told so it can refresh and sync.
"""

from __future__ import annotations

import csv
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Case, EntryRow
from ..store import LocalStore
from ..templates import FieldRole
from ..util import NO_VALUE, format_timecode, slugify_case_id
from .log_dock import LogTableModel
from .widgets import field_label, hint_label

log = logging.getLogger(__name__)

#: The list entry that shows every case's entries together.
ALL_CASES = "\x00all"
#: Cases that came from someone else's account carry this in their name
#: (sync._pull_cases). Entries are not moved into them: the server keeps an entry
#: with the account that owns it, so it would land in a same-named case of this
#: account's instead.
SHARED_MARK = "(shared by "


@dataclass
class Host:
    """What this window asks of the main window. Callables, so it can be tested alone."""

    current_case: Callable[[], str]
    switch_case: Callable[[str, str], None]
    view_entry: Callable[[EntryRow], None]
    edit_entry: Callable[[EntryRow], None]
    goto_entry: Callable[[EntryRow], None]
    edit_templates: Callable[[], None]
    changed: Callable[[], None]


class _SortProxy(QSortFilterProxyModel):
    """Sort timecodes and durations as times, everything else as text."""

    def lessThan(self, left, right) -> bool:  # noqa: N802 - Qt naming
        model = self.sourceModel()
        column = model.column_at(left.column())
        if column is not None and column.kind in ("offset", "duration"):
            a, b = model.entry_at(left.row()), model.entry_at(right.row())
            attr = "event_offset_seconds" if column.kind == "offset" else "event_duration_seconds"
            return (getattr(a, attr) or -1.0) < (getattr(b, attr) or -1.0)
        return str(left.data() or "").casefold() < str(right.data() or "").casefold()


class CasesAndLogWindow(QMainWindow):
    def __init__(self, store: LocalStore, host: Host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._host = host
        self._entries: list[EntryRow] = []
        self.setWindowTitle("Cases and log")
        self.resize(1400, 820)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_cases_panel())
        splitter.addWidget(self._build_log_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([270, 820, 340])
        self.setCentralWidget(splitter)

        self.reload()

    # ------------------------------------------------------------------ #
    # Building
    # ------------------------------------------------------------------ #

    def _build_cases_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(220)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 6, 10)
        layout.addWidget(field_label("Cases"))

        self.case_list = QListWidget()
        self.case_list.setSpacing(3)
        self.case_list.setStyleSheet("QListWidget { font-size: 14px; }")
        self.case_list.currentItemChanged.connect(lambda *_: self._on_case_chosen())
        self.case_list.itemDoubleClicked.connect(lambda _item: self._make_current())
        layout.addWidget(self.case_list, 1)

        self.show_archived = QCheckBox("Show archived cases")
        self.show_archived.toggled.connect(lambda _on: self._load_cases())
        layout.addWidget(self.show_archived)

        row = QHBoxLayout()
        for text, slot in (("New…", self._new_case), ("Rename…", self._rename_case)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.archive_button = QPushButton("Archive")
        self.archive_button.clicked.connect(self._toggle_archive)
        row.addWidget(self.archive_button)
        self.delete_case_button = QPushButton("Delete…")
        self.delete_case_button.clicked.connect(self._delete_case)
        row.addWidget(self.delete_case_button)
        layout.addLayout(row)

        self.current_button = QPushButton("Work on this case")
        self.current_button.setToolTip("New entries you log go into the case you are working on")
        self.current_button.clicked.connect(self._make_current)
        layout.addWidget(self.current_button)

        layout.addSpacing(10)
        layout.addWidget(field_label("Form for this case"))
        self.form_combo = QComboBox()
        self.form_combo.activated.connect(lambda _i: self._set_form())
        layout.addWidget(self.form_combo)
        forms = QPushButton("Create or edit forms…")
        forms.clicked.connect(self._edit_forms)
        layout.addWidget(forms)
        layout.addWidget(hint_label("New entries in the case use this form. Entries already logged keep theirs."))
        return panel

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 10, 6, 10)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search any answer or file name…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._load_entries())
        filters.addWidget(self.search, 2)
        self.status_filter = QComboBox()
        self.investigator_filter = QComboBox()
        self.file_filter = QComboBox()
        for combo in (self.status_filter, self.investigator_filter, self.file_filter):
            combo.setMinimumWidth(130)
            combo.currentIndexChanged.connect(lambda _i: self._apply_filters())
            filters.addWidget(combo, 1)
        self.show_deleted = QCheckBox("Show deleted")
        self.show_deleted.toggled.connect(lambda _on: self._load_entries())
        filters.addWidget(self.show_deleted)
        layout.addLayout(filters)

        actions = QHBoxLayout()
        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(lambda: self._one(self._host.view_entry))
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(lambda: self._one(self._host.edit_entry))
        self.goto_button = QPushButton("Go to moment")
        self.goto_button.clicked.connect(lambda: self._one(self._host.goto_entry))
        self.move_button = QToolButton()
        self.move_button.setText("Move to case")
        self.move_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.move_menu = QMenu(self.move_button)
        self.move_menu.aboutToShow.connect(self._fill_move_menu)
        self.move_button.setMenu(self.move_menu)
        self.status_button = QToolButton()
        self.status_button.setText("Set status")
        self.status_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.status_menu = QMenu(self.status_button)
        self.status_menu.aboutToShow.connect(self._fill_status_menu)
        self.status_button.setMenu(self.status_menu)
        self.link_button = QToolButton()
        self.link_button.setText("Also show in")
        self.link_button.setToolTip("Show the selected entries in another case as well; they stay in their own")
        self.link_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.link_menu = QMenu(self.link_button)
        self.link_menu.aboutToShow.connect(self._fill_link_menu)
        self.link_button.setMenu(self.link_menu)
        self.unlink_button = QPushButton("Remove from this case")
        self.unlink_button.setToolTip("Stop showing these linked entries here; they stay in their own case")
        self.unlink_button.clicked.connect(self._unlink_selected)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete_selected)
        self.restore_button = QPushButton("Restore")
        self.restore_button.clicked.connect(self._restore_selected)
        self.export_button = QPushButton("Export selected…")
        self.export_button.clicked.connect(self._export_selected)
        for widget in (self.open_button, self.edit_button, self.goto_button, self.move_button, self.link_button,
                       self.unlink_button, self.status_button, self.delete_button, self.restore_button,
                       self.export_button):
            actions.addWidget(widget)
        actions.addStretch(1)
        self.count_label = QLabel()
        actions.addWidget(self.count_label)
        layout.addLayout(actions)

        self.model = LogTableModel(self)
        self.proxy = _SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setWordWrap(False)
        self.table.doubleClicked.connect(lambda _i: self._one(self._host.view_entry))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._on_selection())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        select_all = QAction(self)
        select_all.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all.triggered.connect(self.table.selectAll)
        self.addAction(select_all)
        delete = QAction(self)
        delete.setShortcut(QKeySequence.StandardKey.Delete)
        delete.triggered.connect(self._delete_selected)
        self.addAction(delete)
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 10, 10, 10)
        layout.addWidget(field_label("Selected entry"))
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        layout.addWidget(self.detail, 1)
        return panel

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def reload(self) -> None:
        """Re-read everything: after a change here, in the main window, or a sync."""
        self._load_cases()
        self._load_forms()

    def _selected_case_id(self) -> str | None:
        item = self.case_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return None if value == ALL_CASES else value

    def _load_cases(self) -> None:
        keep = self._selected_case_id() or self._host.current_case()
        counts = self._store.case_entry_counts()
        current = self._host.current_case()
        self.case_list.blockSignals(True)
        self.case_list.clear()
        everything = QListWidgetItem(f"All cases  ({sum(counts.values())})")
        everything.setData(Qt.ItemDataRole.UserRole, ALL_CASES)
        self.case_list.addItem(everything)
        chosen = None
        cases = sorted(self._store.list_cases(), key=lambda c: c.name.casefold())
        for case in cases:
            if case.is_archived and not self.show_archived.isChecked() and case.case_id != current:
                continue
            label = f"{case.name}  ({counts.get(case.case_id, 0)})"
            if case.case_id == current:
                label = "● " + label
            if case.is_archived:
                label += "  [archived]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, case.case_id)
            item.setToolTip("The case you are working on" if case.case_id == current else "")
            self.case_list.addItem(item)
            if case.case_id == keep:
                chosen = item
        self.case_list.setCurrentItem(chosen or everything)
        self.case_list.blockSignals(False)
        self._on_case_chosen()

    def _load_forms(self) -> None:
        self.form_combo.blockSignals(True)
        self.form_combo.clear()
        for template in self._store.list_templates():
            self.form_combo.addItem(template.name, template.template_id)
        self.form_combo.blockSignals(False)
        self._show_case_form()

    def _show_case_form(self) -> None:
        case_id = self._selected_case_id()
        self.form_combo.setEnabled(case_id is not None)
        if case_id is None:
            return
        template = self._store.template_for_case(case_id)
        index = self.form_combo.findData(template.template_id)
        self.form_combo.blockSignals(True)
        self.form_combo.setCurrentIndex(max(index, 0))
        self.form_combo.blockSignals(False)

    def _on_case_chosen(self) -> None:
        case_id = self._selected_case_id()
        case = self._store.get_case(case_id) if case_id else None
        is_case = case is not None
        for button in (self.archive_button, self.delete_case_button, self.current_button):
            button.setEnabled(is_case)
        self.archive_button.setText("Unarchive" if case and case.is_archived else "Archive")
        self.current_button.setEnabled(is_case and case_id != self._host.current_case())
        self._show_case_form()
        self._load_entries()

    def _load_entries(self) -> None:
        case_id = self._selected_case_id()
        self._entries = self._store.list_entries(
            case_id=case_id,
            search=self.search.text().strip() or None,
            include_deleted=self.show_deleted.isChecked(),
        )
        template = self._store.template_for_case(case_id or self._host.current_case())
        self.model.set_template(template, {t.template_id: t for t in self._store.list_templates()})
        self._fill_filter(self.status_filter, "Any status", self._status_values())
        self._fill_filter(self.investigator_filter, "Anyone", sorted({e.investigator_name for e in self._entries if e.investigator_name}, key=str.casefold))
        self._fill_filter(self.file_filter, "Any file", sorted({e.file_name for e in self._entries}, key=str.casefold))
        self._apply_filters()

    def _status_of(self, entry: EntryRow) -> str:
        template = self._store.get_template(entry.template_id)
        field = template.field_for_role(FieldRole.STATUS) if template else None
        value = entry.values.get(field.field_id) if field else None
        return "" if value is None else str(value)

    def _status_values(self) -> list[str]:
        values = {self._status_of(e) for e in self._entries}
        return sorted((v for v in values if v), key=str.casefold)

    @staticmethod
    def _fill_filter(combo: QComboBox, everything: str, values: list[str]) -> None:
        keep = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(everything, None)
        for value in values:
            combo.addItem(value, value)
        index = combo.findData(keep) if keep is not None else 0
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _apply_filters(self) -> None:
        status = self.status_filter.currentData()
        who = self.investigator_filter.currentData()
        file = self.file_filter.currentData()
        shown = [
            e for e in self._entries
            if (status is None or self._status_of(e) == status)
            and (who is None or e.investigator_name == who)
            and (file is None or e.file_name == file)
        ]
        self.model.set_entries(shown)
        for index, column in enumerate(self.model.columns):
            # This window has room: wide enough that no heading is clipped.
            width = column.width
            if column.kind in ("offset", "duration"):
                width = max(width, 135)
            width = max(width, self.table.fontMetrics().horizontalAdvance(column.title) + 34)
            self.table.setColumnWidth(index, width)
        self._on_selection()

    # ------------------------------------------------------------------ #
    # Selection and the detail pane
    # ------------------------------------------------------------------ #

    def selected_entries(self) -> list[EntryRow]:
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()})
        return [e for e in (self.model.entry_at(r) for r in rows) if e is not None]

    def _on_selection(self) -> None:
        chosen = self.selected_entries()
        one = len(chosen) == 1
        for widget in (self.open_button, self.edit_button, self.goto_button):
            widget.setEnabled(one)
        for widget in (self.move_button, self.link_button, self.status_button, self.delete_button, self.export_button):
            widget.setEnabled(bool(chosen))
        here = self._selected_case_id()
        self.unlink_button.setVisible(any(here is not None and e.case_id != here for e in chosen))
        self.restore_button.setVisible(self.show_deleted.isChecked())
        self.restore_button.setEnabled(any(e.is_deleted for e in chosen))
        shown = self.model.rowCount()
        self.count_label.setText(
            f"{shown} entr{'y' if shown == 1 else 'ies'}" + (f" · {len(chosen)} selected" if chosen else "")
        )
        self.detail.setHtml(self._detail_html(chosen[0]) if one else (
            f"<p style='color:#9aa3ad'>{len(chosen)} entries selected.</p>" if chosen
            else "<p style='color:#9aa3ad'>Select an entry to see it here.</p>"))

    def _detail_html(self, entry: EntryRow) -> str:
        e = html.escape
        template = self._store.get_template(entry.template_id)
        case = self._store.get_case(entry.case_id)
        when = format_timecode(entry.event_offset_seconds) if entry.event_offset_seconds is not None else NO_VALUE
        if entry.event_duration_seconds:
            when += f" for {format_timecode(entry.event_duration_seconds, millis=False)}"
        parts = [f"<h3 style='margin:0'>{e(entry.file_name)}</h3>",
                 f"<p style='color:#9aa3ad;margin:2px 0 10px'>{e(case.name if case else entry.case_id)} · {e(when)}"
                 + (" · <b style='color:#e5484d'>deleted</b>" if entry.is_deleted else "") + "</p>"]
        for field in (template.ordered_fields if template else []):
            value = entry.values.get(field.field_id)
            if value in (None, ""):
                continue
            parts.append(f"<p style='margin:0 0 8px'><span style='color:#9aa3ad'>{e(field.label)}</span><br>"
                         f"{e(str(value)).replace(chr(10), '<br>')}</p>")
        if entry.also_in:
            names = [(self._store.get_case(c).name if self._store.get_case(c) else c) for c in entry.also_in]
            parts.append(f"<p style='color:#9aa3ad;margin:0 0 8px'>Also shown in: {e(', '.join(names))}</p>")
        parts.append(f"<p style='color:#9aa3ad;margin:8px 0'>{e(entry.investigator_name)}</p>")
        if entry.snapshot_path and Path(entry.snapshot_path).is_file():
            parts.append(f"<img src='{Path(entry.snapshot_path).as_uri()}' width='300'>")
        marks = self._store.annotations_for(entry.entry_id)
        if marks:
            parts.append("<p style='color:#9aa3ad;margin:10px 0 4px'>Marks on the frame</p>")
            for mark in marks:
                text = f": {e(mark.text)}" if getattr(mark, "text", "") else ""
                parts.append(f"<p style='margin:0 0 4px'><span style='color:{e(mark.colour)}'>■</span> "
                             f"{e(mark.kind.value.title())}{text}</p>")
        return "".join(parts)

    def _one(self, action: Callable[[EntryRow], None]) -> None:
        chosen = self.selected_entries()
        if len(chosen) == 1:
            action(self._store.get(chosen[0].entry_id) or chosen[0])
            self.reload()

    def _context_menu(self, position) -> None:
        chosen = self.selected_entries()
        if not chosen:
            return
        menu = QMenu(self)
        if len(chosen) == 1:
            menu.addAction("Open", lambda: self._one(self._host.view_entry))
            menu.addAction("Edit…", lambda: self._one(self._host.edit_entry))
            menu.addAction("Go to moment", lambda: self._one(self._host.goto_entry))
            menu.addSeparator()
        menu.addMenu(self.move_menu).setText("Move to case")
        menu.addMenu(self.status_menu).setText("Set status")
        menu.addSeparator()
        if any(e.is_deleted for e in chosen):
            menu.addAction("Restore", self._restore_selected)
        menu.addAction("Delete", self._delete_selected)
        menu.addAction("Export selected…", self._export_selected)
        self._fill_move_menu()
        self._fill_status_menu()
        menu.exec(self.table.viewport().mapToGlobal(position))

    # ------------------------------------------------------------------ #
    # Acting on entries
    # ------------------------------------------------------------------ #

    def _after_change(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)
        self._host.changed()
        self.reload()

    def _fill_move_menu(self) -> None:
        self.move_menu.clear()
        here = {e.case_id for e in self.selected_entries()}
        targets = [c for c in sorted(self._store.list_cases(), key=lambda c: c.name.casefold())
                   if not c.is_archived and SHARED_MARK not in c.name and here != {c.case_id}]
        if not targets:
            self.move_menu.addAction("No other case to move to").setEnabled(False)
        for case in targets:
            self.move_menu.addAction(case.name, lambda cid=case.case_id, name=case.name: self._move_to(cid, name))

    def _move_to(self, case_id: str, name: str) -> None:
        moved = self._store.move_entries([e.entry_id for e in self.selected_entries()], case_id)
        self._after_change(f"Moved {moved} entr{'y' if moved == 1 else 'ies'} to {name}.")

    def _fill_link_menu(self) -> None:
        self.link_menu.clear()
        chosen = self.selected_entries()
        for case in sorted(self._store.list_cases(), key=lambda c: c.name.casefold()):
            if case.is_archived or SHARED_MARK in case.name or all(x.case_id == case.case_id for x in chosen):
                continue
            self.link_menu.addAction(case.name, lambda cid=case.case_id, name=case.name: self._link_to(cid, name))
        if self.link_menu.isEmpty():
            self.link_menu.addAction("No other case").setEnabled(False)

    def _link_to(self, case_id: str, name: str) -> None:
        changed = self._store.set_links([e.entry_id for e in self.selected_entries()], case_id, linked=True)
        self._after_change(f"{changed} entr{'y' if changed == 1 else 'ies'} now also shown in {name}.")

    def _unlink_selected(self) -> None:
        here = self._selected_case_id()
        if here is None:
            return
        linked = [e.entry_id for e in self.selected_entries() if e.case_id != here]
        changed = self._store.set_links(linked, here, linked=False)
        self._after_change(f"{changed} no longer shown in this case; they stay in their own.")

    def _fill_status_menu(self) -> None:
        self.status_menu.clear()
        chosen = self.selected_entries()
        template = self._store.get_template(chosen[0].template_id) if chosen else None
        field = template.field_for_role(FieldRole.STATUS) if template else None
        values = [o.value for o in field.options] if field and field.options else self._status_values()
        if not values:
            self.status_menu.addAction("This form has no status").setEnabled(False)
        for value in values:
            self.status_menu.addAction(value, lambda v=value: self._set_status(v))

    def _set_status(self, value: str) -> None:
        changed = 0
        for entry in self.selected_entries():
            template = self._store.get_template(entry.template_id)
            field = template.field_for_role(FieldRole.STATUS) if template else None
            if field is not None:
                changed += self._store.set_answer([entry.entry_id], field.field_id, value)
        self._after_change(f"Set {changed} entr{'y' if changed == 1 else 'ies'} to {value}.")

    def _delete_selected(self) -> None:
        chosen = [e for e in self.selected_entries() if not e.is_deleted]
        if not chosen:
            return
        answer = QMessageBox.question(
            self, "Delete entries?",
            f"Delete {len(chosen)} entr{'y' if len(chosen) == 1 else 'ies'}?\n\n"
            "They are kept, not erased: tick Show deleted to see and restore them.",
        )
        if answer is QMessageBox.StandardButton.Yes:
            for entry in chosen:
                self._store.soft_delete(entry.entry_id)
            self._after_change(f"Deleted {len(chosen)}.")

    def _restore_selected(self) -> None:
        chosen = [e for e in self.selected_entries() if e.is_deleted]
        for entry in chosen:
            self._store.restore(entry.entry_id)
        if chosen:
            self._after_change(f"Restored {len(chosen)}.")

    def _export_selected(self) -> None:
        chosen = self.selected_entries()
        if not chosen:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export selected entries", "entries.csv", "CSV (*.csv)")
        if not path:
            return
        columns = [c for c in self.model.columns if c.kind not in ("view", "sync")]
        rows = {e.entry_id for e in chosen}
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow([c.title for c in columns] + ["Case", "Entry ID"])
            for row in range(self.model.rowCount()):
                entry = self.model.entry_at(row)
                if entry is None or entry.entry_id not in rows:
                    continue
                cells = [self.model.data(self.model.index(row, self.model.columns.index(c))) or "" for c in columns]
                writer.writerow([*cells, entry.case_id, entry.entry_id])
        self.statusBar().showMessage(f"Exported {len(chosen)} to {path}", 6000)

    # ------------------------------------------------------------------ #
    # Acting on cases
    # ------------------------------------------------------------------ #

    def _new_case(self) -> None:
        name, ok = QInputDialog.getText(self, "New case", "Case name")
        name = name.strip()
        if not ok or not name:
            return
        case_id = slugify_case_id(name, max_length=60)
        if self._store.get_case(case_id) is not None:
            QMessageBox.information(self, "That case exists", f"There is already a case called {name}.")
            return
        self._store.upsert_case(Case(case_id=case_id, name=name))
        self._after_change(f"Case {name} created.")
        self._select_case(case_id)

    def _select_case(self, case_id: str) -> None:
        for row in range(self.case_list.count()):
            if self.case_list.item(row).data(Qt.ItemDataRole.UserRole) == case_id:
                self.case_list.setCurrentRow(row)
                return

    def _rename_case(self) -> None:
        case = self._store.get_case(self._selected_case_id() or "")
        if case is None:
            return
        name, ok = QInputDialog.getText(self, "Rename case", "Case name", text=case.name)
        name = name.strip()
        if ok and name and name != case.name:
            self._store.upsert_case(Case(case_id=case.case_id, name=name, template_id=case.template_id))
            if case.case_id == self._host.current_case():
                self._host.switch_case(case.case_id, name)
            self._after_change(f"Renamed to {name}.")

    def _toggle_archive(self) -> None:
        case = self._store.get_case(self._selected_case_id() or "")
        if case is None:
            return
        if not case.is_archived and case.case_id == self._host.current_case():
            QMessageBox.information(self, "This is the case you are working on",
                                    "Choose another case to work on first, then archive this one.")
            return
        self._store.set_case_archived(case.case_id, not case.is_archived)
        self._after_change(f"{case.name} {'brought back' if case.is_archived else 'archived; every entry is kept'}.")

    def _delete_case(self) -> None:
        case = self._store.get_case(self._selected_case_id() or "")
        if case is None:
            return
        live = self._store.case_entry_counts().get(case.case_id, 0)
        if live:
            QMessageBox.information(self, "The case is not empty",
                                    f"{case.name} still has {live} entr{'y' if live == 1 else 'ies'}. "
                                    "Move them to another case or delete them first.")
            return
        if case.case_id == self._host.current_case():
            QMessageBox.information(self, "This is the case you are working on",
                                    "Choose another case to work on first.")
            return
        typed, ok = QInputDialog.getText(self, "Delete case", f"Type {case.name} to delete it for good:")
        if ok and typed.strip() == case.name:
            self._store.delete_case(case.case_id)
            self._after_change(f"{case.name} deleted.")
        elif ok:
            QMessageBox.information(self, "Not deleted", "The name did not match, so nothing was deleted.")

    def _make_current(self) -> None:
        case = self._store.get_case(self._selected_case_id() or "")
        if case is not None and case.case_id != self._host.current_case():
            self._host.switch_case(case.case_id, case.name)
            self._after_change(f"Now working on {case.name}.")

    def _set_form(self) -> None:
        case_id = self._selected_case_id()
        template_id = self.form_combo.currentData()
        if case_id and template_id:
            self._store.set_case_template(case_id, template_id)
            self._after_change(f"New entries in this case use the {self.form_combo.currentText()} form.")

    def _edit_forms(self) -> None:
        self._host.edit_templates()
        self.reload()
