"""Editors for template fields.

One class per concern: a :class:`FieldEditor` owns the widget for one
:class:`~evidence_review.templates.TemplateField`, knows how to read a value out
of it and put one back, and reports when the reviewer changed something.

This is where a field's *type* stops being a label and starts meaning something: a
number gets a spin box that will not accept prose, a date gets a calendar, a choice
gets exactly the options the template allows. A renamed text box could never do
any of that, which is the whole reason types exist here.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QLineEdit,
    QSizePolicy,
    QTextEdit,
    QWidget,
)

from ..templates import FieldType, TemplateField
from ..util import NO_VALUE

#: Stands in for "no date". A date edit cannot be empty, so its floor doubles as the
#: unanswered state and is displayed as NO_VALUE.
EMPTY_DATE = QDate(1900, 1, 1)

#: Rows of text a multiline field shows when the template does not say.
DEFAULT_ROWS = 3
#: Pixels per row of a multiline editor, which is close enough at the app's font.
ROW_HEIGHT = 24


class FieldEditor(QWidget):
    """A single template field's editor.

    A QWidget rather than a bare factory function so the editor can carry its own
    ``value_changed`` signal regardless of which widget is inside it -- Qt's text,
    combo, spin and check boxes all announce changes differently, and nothing
    upstream should have to care which.
    """

    value_changed = Signal()

    def __init__(
        self,
        field: TemplateField,
        *,
        suggestions: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.field = field
        self.inner: QWidget = self._build(field, suggestions or [])

        # The editor is the widget: no layout, no margins, nothing between the form
        # row and the control the reviewer clicks.
        self.inner.setParent(self)
        from PySide6.QtWidgets import QVBoxLayout

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.inner)

        if field.help_text:
            self.inner.setToolTip(field.help_text)

    # -- construction -------------------------------------------------------- #

    def _build(self, field: TemplateField, suggestions: list[str]) -> QWidget:
        kind = field.field_type

        if kind is FieldType.MULTILINE:
            edit = QTextEdit()
            edit.setAcceptRichText(False)
            rows = field.config.get("rows", DEFAULT_ROWS)
            edit.setMinimumHeight(int(rows) * ROW_HEIGHT + 14)
            if field.help_text:
                edit.setPlaceholderText(field.help_text)
            edit.textChanged.connect(self.value_changed)
            return edit

        if kind is FieldType.CHOICE:
            combo = QComboBox()
            # A blank first row whenever the template names no default, required or
            # not. Without it a required choice opened on its first option, so an
            # entry saved without touching the field recorded a choice nobody made
            # -- and validation could not object, because a value was there.
            # Starting blank means the save is refused until somebody chooses.
            if field.default_value in (None, ""):
                combo.addItem("", "")
            for option in sorted(field.options, key=lambda o: o.position):
                combo.addItem(option.label, option.value)
            combo.currentIndexChanged.connect(self.value_changed)
            return combo

        if kind is FieldType.NUMBER:
            spin = QDoubleSpinBox()
            spin.setDecimals(int(field.config.get("decimals", 2)))
            spin.setMinimum(float(field.config.get("minimum", -1_000_000)))
            spin.setMaximum(float(field.config.get("maximum", 1_000_000)))
            spin.setSpecialValueText("")  # the minimum reads as "no value"
            spin.setMaximumWidth(180)
            spin.valueChanged.connect(self.value_changed)
            return spin

        if kind is FieldType.DATE:
            date = QDateEdit()
            date.setDisplayFormat("yyyy-MM-dd")
            date.setCalendarPopup(True)
            date.setMaximumWidth(170)
            # A date edit cannot be empty, so its floor stands in for "not answered"
            # and is shown as a dash. Without this an untouched optional date was
            # recorded as 2000-01-01 -- a date nobody entered, in an evidence record.
            date.setMinimumDate(EMPTY_DATE)
            date.setSpecialValueText(NO_VALUE)
            date.setDate(EMPTY_DATE)
            date.dateChanged.connect(self.value_changed)
            return date

        if kind is FieldType.BOOLEAN:
            check = QCheckBox(field.help_text or "")
            check.toggled.connect(self.value_changed)
            return check

        if kind is FieldType.TIME:
            # A QLineEdit and not a QTimeEdit: a time edit cannot be empty, so an
            # optional time field would silently record 00:00 for every entry
            # nobody filled in.
            edit = QLineEdit()
            edit.setPlaceholderText("23:14")
            edit.setMaximumWidth(120)
            edit.textChanged.connect(self.value_changed)
            return edit

        # TEXT, with a vocabulary that grows out of what has been typed before.
        if field.suggests_from_history:
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.addItems(suggestions)
            combo.setCurrentText("")
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if field.help_text:
                combo.lineEdit().setPlaceholderText(field.help_text)
            combo.currentTextChanged.connect(self.value_changed)
            return combo

        edit = QLineEdit()
        if field.help_text:
            edit.setPlaceholderText(field.help_text)
        limit = field.max_length
        if limit:
            edit.setMaxLength(limit)
        edit.textChanged.connect(self.value_changed)
        return edit

    # -- value --------------------------------------------------------------- #

    def value(self) -> Any:
        """The field's current answer, already in the field's own type."""
        widget = self.inner
        if isinstance(widget, QTextEdit):
            return self.field.coerce(widget.toPlainText())
        if isinstance(widget, QComboBox):
            if self.field.field_type is FieldType.CHOICE:
                data = widget.currentData()
                return self.field.coerce(data if data is not None else widget.currentText())
            return self.field.coerce(widget.currentText())
        if isinstance(widget, QDoubleSpinBox):
            # The minimum doubles as "unanswered"; see setSpecialValueText above.
            if widget.value() == widget.minimum():
                return None
            return float(widget.value())
        if isinstance(widget, QDateEdit):
            # The floor is the sentinel for "no date"; see the editor above.
            if widget.date() == EMPTY_DATE:
                return None
            return widget.date().toPython()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QLineEdit):
            return self.field.coerce(widget.text())
        return None

    def set_value(self, value: Any) -> None:
        widget = self.inner
        if isinstance(widget, QTextEdit):
            widget.setPlainText("" if value is None else str(value))
            return
        if isinstance(widget, QComboBox):
            if self.field.field_type is FieldType.CHOICE:
                text = "" if value is None else str(value)
                index = widget.findData(text)
                if index < 0:
                    # A value the template no longer lists -- written before an
                    # option was removed, or on a newer template version. It is
                    # shown rather than quietly replaced with the default, because
                    # the reviewer needs to see what is actually recorded.
                    widget.addItem(text, text)
                    index = widget.count() - 1
                widget.setCurrentIndex(index)
            else:
                widget.setCurrentText("" if value is None else str(value))
            return
        if isinstance(widget, QDoubleSpinBox):
            widget.setValue(widget.minimum() if value is None else float(value))
            return
        if isinstance(widget, QDateEdit):
            if isinstance(value, _dt.date):
                widget.setDate(QDate(value.year, value.month, value.day))
            else:
                # Back to the sentinel, not to today. Clearing a date must leave the
                # field unanswered rather than quietly answering it with today.
                widget.setDate(EMPTY_DATE)
            return
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
            return
        if isinstance(widget, QLineEdit):
            widget.setText("" if value is None else str(value))

    def set_suggestions(self, suggestions: list[str]) -> None:
        """Refresh a history-backed vocabulary without losing what is typed."""
        widget = self.inner
        if not isinstance(widget, QComboBox) or self.field.field_type is FieldType.CHOICE:
            return
        current = widget.currentText()
        widget.clear()
        widget.addItems(suggestions)
        widget.setCurrentText(current)

    def focus(self) -> None:
        self.inner.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def is_prose(self) -> bool:
        """Whether losing this field's content would lose typing, not a click."""
        return self.field.field_type.is_prose
