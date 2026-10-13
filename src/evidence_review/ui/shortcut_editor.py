"""The Shortcuts tab: rebind any action to any key.

Two rules keep this out of trouble. A key can only mean one thing, so assigning a
key that is already taken removes it from the old action and says so rather than
leaving an ambiguous binding Qt would resolve arbitrarily. And every action can be
reset individually, so experimenting is never a one-way door.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import shortcuts as registry
from .theme import ACCENT, TEXT, TEXT_MUTED, WARNING
from .widgets import hint_label

COLUMN_ACTION = 0
COLUMN_PRIMARY = 1
COLUMN_ALTERNATE = 2
COLUMN_RESET = 3


class _KeyEdit(QKeySequenceEdit):
    """A single-chord capture box that reports which action it belongs to."""

    changed_for = Signal(str, int)  # action_id, column

    def __init__(self, action_id: str, column: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.action_id = action_id
        self.column = column
        # One chord per slot: "Ctrl+K, Ctrl+B" style multi-strokes would be a
        # nightmare to discover and to display beside a menu item.
        self.setMaximumSequenceLength(1)
        self.keySequenceChanged.connect(lambda _s: self.changed_for.emit(action_id, column))


class ShortcutEditor(QWidget):
    """Table of every action and the keys bound to it."""

    def __init__(self, bindings: registry.Bindings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, tuple[_KeyEdit, _KeyEdit]] = {}
        self._suppress = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(8)

        layout.addWidget(
            hint_label(
                "Click a key box and press the combination you want. Clear removes a "
                "binding entirely; Reset restores the original. A key can only mean one "
                "thing, so assigning one that is already taken removes it from the "
                "other action."
            )
        )

        top = QHBoxLayout()
        top.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Find an action…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.filter_edit, 1)

        reset_all = QPushButton("Restore all defaults")
        reset_all.clicked.connect(self.confirm_reset_all)
        top.addWidget(reset_all)
        layout.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Action", "Key", "Alternate", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_ACTION, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COLUMN_PRIMARY, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COLUMN_ALTERNATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COLUMN_RESET, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COLUMN_PRIMARY, 150)
        self.table.setColumnWidth(COLUMN_ALTERNATE, 150)
        self.table.setColumnWidth(COLUMN_RESET, 130)
        layout.addWidget(self.table, 1)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        self.note_label.setObjectName("hintLabel")
        layout.addWidget(self.note_label)

        self._build_rows(bindings)
        self._refresh_warnings()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_rows(self, bindings: registry.Bindings) -> None:
        self.table.setRowCount(0)
        for group in registry.GROUP_ORDER:
            actions = [action for action in registry.ACTIONS if action.group == group]
            if not actions:
                continue
            self._add_group_row(group)
            for action in actions:
                self._add_action_row(action, bindings.get(action.id, []))
        self.table.resizeRowsToContents()

    def _add_group_row(self, group: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(group)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor(TEXT_MUTED))
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.table.setItem(row, COLUMN_ACTION, item)
        self.table.setSpan(row, 0, 1, 4)

    def _add_action_row(self, action: registry.ActionSpec, keys: list[str]) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        label = QTableWidgetItem("    " + action.label)
        label.setData(Qt.ItemDataRole.UserRole, action.id)
        if action.description:
            label.setToolTip(action.description)
        label.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, COLUMN_ACTION, label)

        primary = _KeyEdit(action.id, COLUMN_PRIMARY)
        alternate = _KeyEdit(action.id, COLUMN_ALTERNATE)
        for index, edit in enumerate((primary, alternate)):
            if index < len(keys):
                edit.setKeySequence(QKeySequence(keys[index]))
            edit.changed_for.connect(self._on_key_changed)
        self.table.setCellWidget(row, COLUMN_PRIMARY, primary)
        self.table.setCellWidget(row, COLUMN_ALTERNATE, alternate)
        self._rows[action.id] = (primary, alternate)

        buttons = QWidget()
        button_row = QHBoxLayout(buttons)
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(4)

        clear = QPushButton("Clear")
        clear.setToolTip("Unbind this action entirely")
        clear.clicked.connect(lambda _checked=False, a=action: self._clear_action(a))
        button_row.addWidget(clear)

        reset = QPushButton("Reset")
        reset.setToolTip(f"Restore the default: {', '.join(action.defaults) or 'unbound'}")
        reset.clicked.connect(lambda _checked=False, a=action: self._reset_action(a))
        button_row.addWidget(reset)

        self.table.setCellWidget(row, COLUMN_RESET, buttons)

    # ------------------------------------------------------------------ #
    # Editing
    # ------------------------------------------------------------------ #

    def _on_key_changed(self, action_id: str, column: int) -> None:
        """Take a newly assigned key away from whatever held it before."""
        if self._suppress:
            return
        edit = self._rows[action_id][0 if column == COLUMN_PRIMARY else 1]
        sequence = registry.normalise(
            edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        )
        if not sequence:
            self.note_label.setText("")
            return

        stolen_from: list[str] = []
        self._suppress = True
        try:
            for other_id, edits in self._rows.items():
                for other in edits:
                    if other is edit:
                        continue
                    other_sequence = registry.normalise(
                        other.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                    )
                    if other_sequence and other_sequence == sequence:
                        other.clear()
                        stolen_from.append(registry.ACTIONS_BY_ID[other_id].label)
        finally:
            self._suppress = False

        parts: list[str] = []
        if stolen_from:
            parts.append(f"{sequence} was taken from {', '.join(sorted(set(stolen_from)))}.")

        warning = registry.warn_about(sequence)
        if warning is not None:
            prefix = "will not work" if warning.is_blocked else "may cause trouble"
            parts.append(f"{sequence} {prefix}: {warning.message}")

        self.note_label.setText(" ".join(parts))
        self.note_label.setStyleSheet(
            f"color:{ACCENT};" if (warning and warning.is_blocked) or stolen_from else ""
        )
        self._refresh_warnings()

    def _clear_action(self, action: registry.ActionSpec) -> None:
        """Unbind an action. Distinct from resetting, which restores the default."""
        self._suppress = True
        try:
            for edit in self._rows[action.id]:
                edit.clear()
        finally:
            self._suppress = False
        self.note_label.setText(f"{action.label} is now unbound.")
        self.note_label.setStyleSheet("")
        self._refresh_warnings()

    def _refresh_warnings(self) -> None:
        """Mark rows whose keys the system is likely to intercept.

        The note line only reports the edit just made; without this a warning
        scrolls out of sight the moment the next key is assigned.
        """
        flagged = registry.warnings_for(self.bindings())
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COLUMN_ACTION)
            if item is None:
                continue
            action_id = item.data(Qt.ItemDataRole.UserRole)
            if action_id is None:
                continue

            action = registry.ACTIONS_BY_ID[action_id]
            warning = flagged.get(action_id)
            if warning is None:
                item.setText("    " + action.label)
                item.setForeground(QColor(TEXT))
                item.setToolTip(action.description)
            else:
                item.setText("  \u26a0 " + action.label)
                item.setForeground(QColor(ACCENT if warning.is_blocked else WARNING))
                item.setToolTip(warning.message)

    def _reset_action(self, action: registry.ActionSpec) -> None:
        self._suppress = True
        try:
            primary, alternate = self._rows[action.id]
            defaults = list(action.defaults)
            primary.setKeySequence(QKeySequence(defaults[0]) if defaults else QKeySequence())
            alternate.setKeySequence(
                QKeySequence(defaults[1]) if len(defaults) > 1 else QKeySequence()
            )
        finally:
            self._suppress = False
        # Resetting can reintroduce a clash with something the user moved.
        self._on_key_changed(action.id, COLUMN_PRIMARY)

    def confirm_reset_all(self) -> None:
        """Ask, then reset. Wired to the button; `reset_all` stays the plain action.

        Undoing this means cancelling the whole Settings dialog, which throws away
        unrelated edits too, so it is worth one question. The confirmation lives
        here rather than in `reset_all` because a modal inside the action itself
        would hang any headless caller - a test included.
        """
        if self.bindings() != registry.default_bindings():
            answer = QMessageBox.question(
                self,
                "Restore all shortcuts?",
                "Every key you have customised goes back to its default.\n\nRestore them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        self.reset_all()

    def reset_all(self) -> None:
        self._suppress = True
        try:
            for action in registry.ACTIONS:
                primary, alternate = self._rows[action.id]
                defaults = list(action.defaults)
                primary.setKeySequence(QKeySequence(defaults[0]) if defaults else QKeySequence())
                alternate.setKeySequence(
                    QKeySequence(defaults[1]) if len(defaults) > 1 else QKeySequence()
                )
        finally:
            self._suppress = False
        self.note_label.setText("All shortcuts restored to their defaults.")
        self.note_label.setStyleSheet("")
        self._refresh_warnings()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COLUMN_ACTION)
            if item is None:
                continue
            action_id = item.data(Qt.ItemDataRole.UserRole)
            if action_id is None:
                # A group header: shown only when nothing is being filtered.
                self.table.setRowHidden(row, bool(needle))
                continue
            action = registry.ACTIONS_BY_ID[action_id]
            haystack = f"{action.label} {action.description} {action.group}".lower()
            self.table.setRowHidden(row, bool(needle) and needle not in haystack)

    # ------------------------------------------------------------------ #
    # Result
    # ------------------------------------------------------------------ #

    def bindings(self) -> registry.Bindings:
        """The bindings as currently shown, canonicalised."""
        result: registry.Bindings = {}
        for action_id, (primary, alternate) in self._rows.items():
            keys = [
                edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                for edit in (primary, alternate)
            ]
            result[action_id] = registry.normalise_all(keys)
        return result

    def conflicts(self) -> dict[str, list[str]]:
        return registry.conflicts(self.bindings())

    def blocked_keys(self) -> dict[str, registry.KeyWarning]:
        """Actions bound to keys the system will never deliver."""
        return {
            action_id: warning
            for action_id, warning in registry.warnings_for(self.bindings()).items()
            if warning.is_blocked
        }
