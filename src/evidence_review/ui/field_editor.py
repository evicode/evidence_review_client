"""Editing one template field: what it asks, how it is answered, what it means.

Separate from the template editor because a field is the thing with all the detail --
its type, its role, its options and their colours -- and because the same dialog
serves adding and editing.

Everything here is about the *definition* of a field. Editors for a field's **value**
live in :mod:`evidence_review.ui.field_widgets`.
"""

from __future__ import annotations

import re

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..templates import (
    DEFAULT_MAX_LENGTH,
    FieldOption,
    FieldRole,
    FieldType,
    LogTemplate,
    TemplateField,
    new_id,
)
from .widgets import field_label, fit_to_screen, hint_label, scrollable

#: What each type is for, in the reviewer's terms rather than a programmer's.
TYPE_LABELS: dict[FieldType, str] = {
    FieldType.TEXT: "Short text — one line",
    FieldType.MULTILINE: "Long text — a paragraph",
    FieldType.CHOICE: "Choose one — from a list you define",
    FieldType.NUMBER: "Number",
    FieldType.DATE: "Date",
    FieldType.TIME: "Time of day",
    FieldType.BOOLEAN: "Yes / no",
}

#: What each role makes the application do, which is the only reason to set one.
ROLE_LABELS: dict[FieldRole | None, str] = {
    None: "Nothing special",
    FieldRole.SUMMARY: "Summary — the wide column in the log, and what search looks at first",
    FieldRole.STATUS: "Status — colour-codes the row, and can be filtered on",
    FieldRole.LOCATION: "Location — filterable, and remembered per folder",
    FieldRole.DETAIL: "Detail — secondary prose, shown in the tooltip",
}

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify_key(label: str) -> str:
    """A machine name from a label. Only ever a starting point; it is editable."""
    key = _SLUG.sub("_", label.strip().lower()).strip("_")
    return key[:64] or "field"


class FieldEditorDialog(QDialog):
    """Define one question on the form.

    ``template`` is needed to keep ``field_key`` unique within it and to warn when a
    role is already taken, which is the only validation a field cannot do alone.
    """

    def __init__(
        self,
        *,
        template: LogTemplate,
        field: TemplateField | None = None,
        usage: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._template = template
        self._original = field
        #: How many entries already hold a value for this field. Drives the warning
        #: about changing its type.
        self._usage = usage
        self._field: TemplateField | None = None
        self._options: list[FieldOption] = (
            [option.model_copy(deep=True) for option in field.options] if field else []
        )

        self.setWindowTitle("Add field" if field is None else f"Edit field — {field.label}")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._build_ui()
        self._populate(field)
        fit_to_screen(self)

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        body = QWidget()
        # Held onto, because showing and hiding a row means hiding its label too and
        # QFormLayout is the only thing that knows which label belongs to which field.
        self._form = form = QFormLayout(body)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        outer.addWidget(scrollable(body), 1)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("What the reviewer reads, e.g. Vehicles")
        self.label_edit.textChanged.connect(self._on_label_changed)
        form.addRow(field_label("Label"), self.label_edit)

        self.key_edit = QLineEdit()
        self.key_edit.setMaxLength(64)
        form.addRow(field_label("Name"), self.key_edit)
        form.addRow(
            "",
            hint_label(
                "Used in exports and when a template is shared. Renaming it is safe: "
                "answers already recorded are tied to the field itself, not to its name."
            ),
        )

        self.type_combo = QComboBox()
        for kind, text in TYPE_LABELS.items():
            self.type_combo.addItem(text, kind.value)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(field_label("Answered with"), self.type_combo)

        self.type_warning = QLabel()
        self.type_warning.setWordWrap(True)
        self.type_warning.setStyleSheet("color:#e3a008;")
        self.type_warning.hide()
        form.addRow("", self.type_warning)

        self.role_combo = QComboBox()
        for role, text in ROLE_LABELS.items():
            self.role_combo.addItem(text, role.value if role else "")
        self.role_combo.currentIndexChanged.connect(self._refresh_role_hint)
        form.addRow(field_label("Role"), self.role_combo)

        self.role_hint = hint_label("")
        form.addRow("", self.role_hint)

        self.required_check = QCheckBox("An entry cannot be saved without an answer")
        form.addRow(field_label("Required"), self.required_check)

        self.in_table_check = QCheckBox("Give it a column in the session log")
        form.addRow(field_label("In the log"), self.in_table_check)

        self.help_edit = QLineEdit()
        self.help_edit.setPlaceholderText("Shown as a hint inside the box, and as a tooltip")
        form.addRow(field_label("Hint"), self.help_edit)

        # -- choice options ------------------------------------------------- #
        self.options_widget = QWidget()
        options_layout = QVBoxLayout(self.options_widget)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(6)

        self.options_list = QListWidget()
        self.options_list.setMaximumHeight(150)
        options_layout.addWidget(self.options_list)

        buttons = QHBoxLayout()
        for text, slot in (
            ("Add", self._add_option),
            ("Rename", self._rename_option),
            ("Colour", self._set_option_colour),
            ("Default", self._set_option_default),
            ("Remove", self._remove_option),
            ("Up", lambda: self._move_option(-1)),
            ("Down", lambda: self._move_option(1)),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        options_layout.addLayout(buttons)
        options_layout.addWidget(
            hint_label(
                "A colour here is what the log paints the row. Leave it unset and one "
                "is derived from the name, so it is still legible."
            )
        )
        form.addRow(field_label("Choices"), self.options_widget)

        # -- number bounds -------------------------------------------------- #
        self.bounds_widget = QWidget()
        bounds = QHBoxLayout(self.bounds_widget)
        bounds.setContentsMargins(0, 0, 0, 0)
        self.minimum_spin = QSpinBox()
        self.minimum_spin.setRange(-1_000_000, 1_000_000)
        self.maximum_spin = QSpinBox()
        self.maximum_spin.setRange(-1_000_000, 1_000_000)
        self.maximum_spin.setValue(1000)
        bounds.addWidget(QLabel("from"))
        bounds.addWidget(self.minimum_spin)
        bounds.addWidget(QLabel("to"))
        bounds.addWidget(self.maximum_spin)
        bounds.addStretch(1)
        form.addRow(field_label("Range"), self.bounds_widget)

        # -- length / rows -------------------------------------------------- #
        self.length_spin = QSpinBox()
        self.length_spin.setRange(1, 100_000)
        self.length_spin.setSingleStep(50)
        form.addRow(field_label("Maximum length"), self.length_spin)

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 20)
        form.addRow(field_label("Lines shown"), self.rows_spin)

        self.suggest_check = QCheckBox("Offer values already used in this case")
        form.addRow(field_label("Suggestions"), self.suggest_check)

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color:#e5484d; font-weight:600;")
        self.problem_label.hide()
        outer.addWidget(self.problem_label)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._on_save)
        box.rejected.connect(self.reject)
        outer.addWidget(box)

    # ------------------------------------------------------------------ #

    def _populate(self, field: TemplateField | None) -> None:
        if field is None:
            self.type_combo.setCurrentIndex(0)
            self.length_spin.setValue(DEFAULT_MAX_LENGTH[FieldType.TEXT])
            self.rows_spin.setValue(3)
            self._on_type_changed()
            self._refresh_role_hint()
            self.label_edit.setFocus()
            return

        self.label_edit.setText(field.label)
        self.key_edit.setText(field.field_key)
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(field.field_type.value)))
        self.role_combo.setCurrentIndex(
            max(0, self.role_combo.findData(field.role.value if field.role else ""))
        )
        self.required_check.setChecked(field.is_required)
        self.in_table_check.setChecked(field.in_table)
        self.help_edit.setText(field.help_text)
        self.length_spin.setValue(field.max_length or DEFAULT_MAX_LENGTH[FieldType.TEXT])
        self.rows_spin.setValue(int(field.config.get("rows", 3)))
        self.suggest_check.setChecked(field.suggests_from_history)
        minimum = field.config.get("minimum")
        maximum = field.config.get("maximum")
        if isinstance(minimum, int | float):
            self.minimum_spin.setValue(int(minimum))
        if isinstance(maximum, int | float):
            self.maximum_spin.setValue(int(maximum))

        self._refresh_options()
        self._on_type_changed()
        self._refresh_role_hint()
        self.label_edit.setFocus()

    def _on_label_changed(self, text: str) -> None:
        """Keep the machine name following the label, until it is set by hand."""
        if self._original is not None:
            return
        if not self.key_edit.isModified():
            self.key_edit.setText(slugify_key(text))

    @property
    def _selected_type(self) -> FieldType:
        return FieldType(self.type_combo.currentData())

    def _on_type_changed(self) -> None:
        kind = self._selected_type
        # Only the settings this type actually has. A "lines shown" box on a yes/no
        # field is a question with no meaning, and a form full of them is how a
        # builder becomes unusable.
        for widget, visible in (
            (self.options_widget, kind is FieldType.CHOICE),
            (self.bounds_widget, kind is FieldType.NUMBER),
            (self.rows_spin, kind is FieldType.MULTILINE),
            (self.suggest_check, kind is FieldType.TEXT),
            (self.length_spin, kind.is_prose),
        ):
            widget.setVisible(visible)
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

        self._refresh_type_warning()

    def _refresh_type_warning(self) -> None:
        """Say plainly what changing the type will do to answers already recorded.

        Nothing is deleted -- the stored answers stay exactly as they are -- but one
        that cannot be read as the new type will be flagged the next time the entry is
        opened, and the reviewer has to be told that before they agree to it.
        """
        if self._original is None or self._usage == 0:
            self.type_warning.hide()
            return
        if self._selected_type is self._original.field_type:
            self.type_warning.hide()
            return

        entries = f"{self._usage} {'entry' if self._usage == 1 else 'entries'}"
        if self._selected_type.storage == self._original.field_type.storage:
            self.type_warning.setText(
                f"{entries} already answer this field. They are kept as they are."
            )
        else:
            self.type_warning.setText(
                f"{entries} already answer this field. Nothing is deleted, but an answer "
                f"that cannot be read as a {self._selected_type.value} will be flagged "
                "the next time that entry is opened, for someone to correct."
            )
        self.type_warning.show()

    def _refresh_role_hint(self) -> None:
        data = self.role_combo.currentData()
        role = FieldRole(data) if data else None
        if role is None:
            self.role_hint.setText("")
            return

        holder = self._template.field_for_role(role)
        if holder is not None and (
            self._original is None or holder.field_id != self._original.field_id
        ):
            self.role_hint.setText(
                f"“{holder.label}” has this role at the moment. Saving moves it to this field."
            )
        else:
            self.role_hint.setText("")

    # -- options -------------------------------------------------------- #

    def _refresh_options(self) -> None:
        self.options_list.clear()
        for option in self._options:
            text = option.label
            if option.is_default:
                text += "   (default)"
            item = QListWidgetItem(text)
            if option.colour:
                item.setForeground(QColor(option.colour))
            self.options_list.addItem(item)

    def _current_option(self) -> FieldOption | None:
        row = self.options_list.currentRow()
        return self._options[row] if 0 <= row < len(self._options) else None

    def _add_option(self) -> None:
        text, accepted = QInputDialog.getText(self, "Add choice", "Choice:")
        value = text.strip()
        if not accepted or not value:
            return
        if any(option.value == value for option in self._options):
            QMessageBox.information(self, "Already listed", f"“{value}” is already a choice.")
            return
        self._options.append(
            FieldOption(
                option_id=new_id(),
                value=value,
                label=value,
                position=len(self._options),
                # The first choice added becomes the default, which is almost always
                # what is wanted and is one click to change.
                is_default=not self._options,
            )
        )
        self._refresh_options()

    def _rename_option(self) -> None:
        option = self._current_option()
        if option is None:
            QMessageBox.information(self, "Nothing selected", "Select a choice first.")
            return
        text, accepted = QInputDialog.getText(self, "Rename choice", "Choice:", text=option.value)
        value = text.strip()
        if not accepted or not value:
            return
        # The stored value changes, so answers already recorded keep the old wording.
        # That is deliberate: a record says what it said when it was made.
        option.value = value
        option.label = value
        self._refresh_options()

    def _set_option_colour(self) -> None:
        option = self._current_option()
        if option is None:
            QMessageBox.information(self, "Nothing selected", "Select a choice first.")
            return
        initial = QColor(option.colour) if option.colour else QColor("#8b949e")
        chosen = QColorDialog.getColor(initial, self, f"Colour for “{option.label}”")
        if chosen.isValid():
            option.colour = chosen.name()
            self._refresh_options()

    def _set_option_default(self) -> None:
        option = self._current_option()
        if option is None:
            QMessageBox.information(self, "Nothing selected", "Select a choice first.")
            return
        for candidate in self._options:
            candidate.is_default = candidate is option
        self._refresh_options()

    def _remove_option(self) -> None:
        row = self.options_list.currentRow()
        if not 0 <= row < len(self._options):
            QMessageBox.information(self, "Nothing selected", "Select a choice first.")
            return
        self._options.pop(row)
        for position, option in enumerate(self._options):
            option.position = position
        self._refresh_options()

    def _move_option(self, delta: int) -> None:
        row = self.options_list.currentRow()
        target = row + delta
        if not (0 <= row < len(self._options) and 0 <= target < len(self._options)):
            return
        self._options[row], self._options[target] = self._options[target], self._options[row]
        for position, option in enumerate(self._options):
            option.position = position
        self._refresh_options()
        self.options_list.setCurrentRow(target)

    # -- save ----------------------------------------------------------- #

    def _problems(self, label: str, key: str, kind: FieldType) -> list[str]:
        problems: list[str] = []
        if not label:
            problems.append("A field needs a label.")
        if not key:
            problems.append("A field needs a name.")
        elif not re.fullmatch(r"[a-z0-9_]+", key):
            problems.append("The name may contain only lowercase letters, digits and underscores.")

        clash = self._template.field_by_key(key)
        if clash is not None and (
            self._original is None or clash.field_id != self._original.field_id
        ):
            problems.append(f"Another field is already named “{key}”.")

        if kind is FieldType.CHOICE and not self._options:
            problems.append("A 'choose one' field needs at least one choice.")
        if kind is FieldType.NUMBER and self.minimum_spin.value() > self.maximum_spin.value():
            problems.append("The range cannot start above where it ends.")
        return problems

    def _on_save(self) -> None:
        label = self.label_edit.text().strip()
        key = self.key_edit.text().strip().lower()
        kind = self._selected_type

        problems = self._problems(label, key, kind)
        if problems:
            self.problem_label.setText("\n".join(f"• {problem}" for problem in problems))
            self.problem_label.show()
            return
        self.problem_label.hide()

        config: dict[str, object] = {}
        if kind.is_prose:
            config["max_length"] = self.length_spin.value()
        if kind is FieldType.MULTILINE:
            config["rows"] = self.rows_spin.value()
        if kind is FieldType.TEXT and self.suggest_check.isChecked():
            config["suggest_from_history"] = True
        if kind is FieldType.NUMBER:
            config["minimum"] = self.minimum_spin.value()
            config["maximum"] = self.maximum_spin.value()

        role_data = self.role_combo.currentData()
        self._field = TemplateField(
            # The id is kept across an edit. It is what every recorded answer points
            # at, so a new one here would orphan all of them.
            field_id=self._original.field_id if self._original else new_id(),
            field_key=key,
            label=label,
            field_type=kind,
            role=FieldRole(role_data) if role_data else None,
            position=self._original.position if self._original else len(self._template.fields),
            is_required=self.required_check.isChecked(),
            help_text=self.help_edit.text().strip(),
            config=config,
            options=self._options if kind is FieldType.CHOICE else [],
            in_table=self.in_table_check.isChecked(),
        )
        self.accept()

    @property
    def field(self) -> TemplateField | None:
        """The field as defined, or None if the dialog was cancelled."""
        return self._field
