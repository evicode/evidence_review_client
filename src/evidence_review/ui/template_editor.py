"""Defining the form an investigation is reviewed with.

The reviewer builds their own log here: the questions, how each is answered, which
answers are required, and which one is required only when another says something
particular. Both shipped templates were written with nothing this dialog cannot do,
which is the standard it is held to.

Two rules run through all of it:

* **An answer already recorded is never altered or deleted.** Removing a field leaves
  its answers in place, unreachable but intact; renaming one keeps them, because they
  are tied to the field's id and not to its name. Editing a form is not a reason to
  change a record made with it.
* **Every edit bumps the template's version**, which is how two people editing the
  same shared template resolve it: the later version wins, and the earlier one is
  still in the server's history.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..builtin_templates import BUILTIN_TEMPLATES_BY_ID
from ..store import LocalStore
from ..templates import FieldRole, FieldType, LogTemplate, TemplateRule, new_id
from .field_editor import ROLE_LABELS, TYPE_LABELS, FieldEditorDialog
from .widgets import field_label, fit_to_screen, hint_label

log = logging.getLogger(__name__)


class TemplateEditorDialog(QDialog):
    """Create, edit, duplicate, share and delete log templates."""

    def __init__(
        self,
        store: LocalStore,
        *,
        case_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._case_id = case_id
        #: The template being edited, as a working copy. Nothing is written until
        #: Save, so Cancel really does discard.
        self._draft: LogTemplate | None = None
        self._dirty = False

        self.setWindowTitle("Log templates")
        self.setModal(True)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._reload_list()
        fit_to_screen(self)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_template_pane())
        splitter.addWidget(self._build_field_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 640])
        outer.addWidget(splitter, 1)

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color:#e5484d; font-weight:600;")
        self.problem_label.hide()
        outer.addWidget(self.problem_label)

        box = QDialogButtonBox()
        self.save_button = box.addButton("Save template", QDialogButtonBox.ButtonRole.ApplyRole)
        self.save_button.clicked.connect(self._save_draft)
        self.use_button = box.addButton("Use for this case", QDialogButtonBox.ButtonRole.ActionRole)
        self.use_button.clicked.connect(self._use_for_case)
        box.addButton(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        outer.addWidget(box)

    def _build_template_pane(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addWidget(field_label("Templates"))
        self.template_list = QListWidget()
        self.template_list.currentRowChanged.connect(self._on_template_selected)
        layout.addWidget(self.template_list, 1)

        for text, slot, tip in (
            ("New…", self._new_template, "Start an empty template"),
            (
                "Duplicate…",
                self._duplicate_template,
                "Copy the selected one, which is the usual way to start",
            ),
            ("Import…", self._import_template, "Load a template someone shared with you"),
            ("Export…", self._export_template, "Save this template to a file to share"),
            ("Delete", self._delete_template, "Remove a template you created"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            layout.addWidget(button)

        self.restore_button = QPushButton("Restore shipped version")
        self.restore_button.setToolTip(
            "Put a built-in template back the way it came, discarding your changes to it"
        )
        self.restore_button.clicked.connect(self._restore_builtin)
        layout.addWidget(self.restore_button)

        layout.addWidget(
            hint_label(
                "Duplicating one of the built-ins is the easiest start: you get a "
                "working form and change what you need."
            )
        )
        return panel

    def _build_field_pane(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(lambda _t: self._mark_dirty())
        form.addRow(field_label("Name"), self.name_edit)
        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("What this form is for")
        self.description_edit.textChanged.connect(lambda _t: self._mark_dirty())
        form.addRow(field_label("Description"), self.description_edit)
        self.usage_label = hint_label("")
        form.addRow("", self.usage_label)
        layout.addLayout(form)

        layout.addWidget(field_label("Fields"))
        self.field_table = QTableWidget(0, 5)
        self.field_table.setHorizontalHeaderLabels(
            ["Label", "Answered with", "Role", "Required", "In the log"]
        )
        self.field_table.verticalHeader().setVisible(False)
        self.field_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.field_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.field_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.field_table.doubleClicked.connect(lambda _i: self._edit_field())
        header = self.field_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)
        layout.addWidget(self.field_table, 1)

        field_buttons = QHBoxLayout()
        for text, slot in (
            ("Add field…", self._add_field),
            ("Edit…", self._edit_field),
            ("Remove", self._remove_field),
            ("Move up", lambda: self._move_field(-1)),
            ("Move down", lambda: self._move_field(1)),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            field_buttons.addWidget(button)
        field_buttons.addStretch(1)
        layout.addLayout(field_buttons)

        layout.addWidget(field_label("Rules"))
        self.rule_list = QListWidget()
        self.rule_list.setMaximumHeight(90)
        layout.addWidget(self.rule_list)

        rule_buttons = QHBoxLayout()
        add_rule = QPushButton("Add rule…")
        add_rule.setToolTip("Require one field only when another has a particular answer")
        add_rule.clicked.connect(self._add_rule)
        remove_rule = QPushButton("Remove rule")
        remove_rule.clicked.connect(self._remove_rule)
        rule_buttons.addWidget(add_rule)
        rule_buttons.addWidget(remove_rule)
        rule_buttons.addStretch(1)
        layout.addLayout(rule_buttons)

        return panel

    # ------------------------------------------------------------------ #
    # Template list
    # ------------------------------------------------------------------ #

    def _reload_list(self, select_id: str | None = None) -> None:
        wanted = select_id or (self._draft.template_id if self._draft else None)
        counts = {
            template.template_id: self._store.template_usage(template.template_id)
            for template in self._store.list_templates()
        }

        self.template_list.blockSignals(True)
        self.template_list.clear()
        templates = self._store.list_templates()
        for template in templates:
            label = template.name
            marks = []
            if template.is_builtin:
                marks.append("built-in")
            if template.is_customised:
                marks.append("edited")
            used = counts.get(template.template_id, 0)
            if used:
                marks.append(f"{used} {'entry' if used == 1 else 'entries'}")
            if marks:
                label += f"   ({', '.join(marks)})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, template.template_id)
            self.template_list.addItem(item)
        self.template_list.blockSignals(False)

        row = 0
        for index, template in enumerate(templates):
            if template.template_id == wanted:
                row = index
                break
        if templates:
            self.template_list.setCurrentRow(row)
            self._load_draft(templates[row].template_id)

    def _on_template_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self.template_list.item(row)
        if item is None:
            return
        template_id = item.data(Qt.ItemDataRole.UserRole)
        if self._draft is not None and template_id != self._draft.template_id and self._dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                f"“{self._draft.name}” has changes that have not been saved.\n\nDiscard them?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                self._reload_list(self._draft.template_id)
                return
        self._load_draft(template_id)

    def _load_draft(self, template_id: str) -> None:
        stored = self._store.get_template(template_id)
        if stored is None:
            return
        # A deep copy, so editing the form does not quietly mutate the store's cache.
        self._draft = stored.model_copy(deep=True)
        self._dirty = False

        self.name_edit.blockSignals(True)
        self.description_edit.blockSignals(True)
        self.name_edit.setText(self._draft.name)
        self.description_edit.setText(self._draft.description)
        self.name_edit.blockSignals(False)
        self.description_edit.blockSignals(False)

        used = self._store.template_usage(template_id)
        parts = [f"Version {self._draft.version}"]
        if used:
            parts.append(f"used by {used} {'entry' if used == 1 else 'entries'}")
        if self._draft.is_builtin:
            parts.append("shipped with the application")
        self.usage_label.setText(" · ".join(parts))
        self.restore_button.setEnabled(
            self._draft.is_builtin and self._draft.template_id in BUILTIN_TEMPLATES_BY_ID
        )
        self._refresh_fields()
        self._refresh_rules()
        self.problem_label.hide()

    def _mark_dirty(self) -> None:
        self._dirty = True

    # ------------------------------------------------------------------ #
    # Fields
    # ------------------------------------------------------------------ #

    def _refresh_fields(self) -> None:
        draft = self._draft
        self.field_table.setRowCount(0)
        if draft is None:
            return
        for row, field in enumerate(draft.ordered_fields):
            self.field_table.insertRow(row)
            cells = [
                field.label,
                TYPE_LABELS[field.field_type].split(" — ")[0],
                ROLE_LABELS[field.role].split(" — ")[0],
                "yes" if field.is_required else "",
                "yes" if field.in_table else "",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, field.field_id)
                if column == 2 and field.role is FieldRole.STATUS:
                    # The status field's own colours are what the log uses, so a hint
                    # of them here connects the two.
                    default = next((option for option in field.options if option.is_default), None)
                    if default is not None and default.colour:
                        item.setForeground(QColor(default.colour))
                self.field_table.setItem(row, column, item)

    def _selected_field_id(self) -> str | None:
        row = self.field_table.currentRow()
        item = self.field_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add_field(self) -> None:
        if self._draft is None:
            return
        dialog = FieldEditorDialog(template=self._draft, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.field is None:
            return
        self._take_role(dialog.field)
        self._draft.fields.append(dialog.field)
        self._renumber()
        self._mark_dirty()
        self._refresh_fields()

    def _edit_field(self) -> None:
        if self._draft is None:
            return
        field_id = self._selected_field_id()
        field = self._draft.field(field_id) if field_id else None
        if field is None:
            QMessageBox.information(self, "Nothing selected", "Select a field first.")
            return

        dialog = FieldEditorDialog(
            template=self._draft,
            field=field,
            usage=self._store.field_usage(field.field_id),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.field is None:
            return

        self._take_role(dialog.field)
        self._draft.fields = [
            dialog.field if candidate.field_id == field.field_id else candidate
            for candidate in self._draft.fields
        ]
        self._mark_dirty()
        self._refresh_fields()
        self._refresh_rules()

    def _take_role(self, field) -> None:
        """One field per role. Giving a role to a field takes it off the other.

        Two summary fields would make "the wide column in the log" ambiguous, and the
        engine would pick whichever came first -- which is worse than being told.
        """
        if field.role is None or self._draft is None:
            return
        for candidate in self._draft.fields:
            if candidate.field_id != field.field_id and candidate.role is field.role:
                candidate.role = None

    def _remove_field(self) -> None:
        if self._draft is None:
            return
        field_id = self._selected_field_id()
        field = self._draft.field(field_id) if field_id else None
        if field is None:
            QMessageBox.information(self, "Nothing selected", "Select a field first.")
            return

        used = self._store.field_usage(field.field_id)
        message = f"Remove “{field.label}” from this template?"
        if used:
            message += (
                f"\n\n{used} {'entry' if used == 1 else 'entries'} already answer it. "
                "Those answers are kept, not deleted — they stop being shown, and they "
                "come back if you add the field again."
            )
        if (
            QMessageBox.question(
                self,
                "Remove field",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._draft.fields = [f for f in self._draft.fields if f.field_id != field.field_id]
        # A rule that points at a field which no longer exists would never fire, and
        # would be impossible to understand in the list.
        self._draft.rules = [
            rule
            for rule in self._draft.rules
            if field.field_id not in (rule.when_field_id, rule.requires_field_id)
        ]
        self._renumber()
        self._mark_dirty()
        self._refresh_fields()
        self._refresh_rules()

    def _move_field(self, delta: int) -> None:
        if self._draft is None:
            return
        ordered = self._draft.ordered_fields
        field_id = self._selected_field_id()
        index = next((i for i, f in enumerate(ordered) if f.field_id == field_id), None)
        if index is None:
            return
        target = index + delta
        if not 0 <= target < len(ordered):
            return
        ordered[index], ordered[target] = ordered[target], ordered[index]
        for position, field in enumerate(ordered):
            field.position = position
        self._draft.fields = ordered
        self._mark_dirty()
        self._refresh_fields()
        self.field_table.selectRow(target)

    def _renumber(self) -> None:
        if self._draft is None:
            return
        for position, field in enumerate(self._draft.ordered_fields):
            field.position = position

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #

    def _refresh_rules(self) -> None:
        self.rule_list.clear()
        if self._draft is None:
            return
        for rule in self._draft.rules:
            trigger = self._draft.field(rule.when_field_id)
            required = self._draft.field(rule.requires_field_id)
            if trigger is None or required is None:
                continue
            item = QListWidgetItem(
                f"“{required.label}” is required when {trigger.label} is {rule.when_value}"
            )
            item.setData(Qt.ItemDataRole.UserRole, rule.rule_id)
            self.rule_list.addItem(item)

    def _add_rule(self) -> None:
        draft = self._draft
        if draft is None:
            return
        choices = [field for field in draft.ordered_fields if field.field_type is FieldType.CHOICE]
        if not choices:
            QMessageBox.information(
                self,
                "No choice field",
                "A rule fires on a particular answer, so it needs a 'choose one' field "
                "to watch. Add one first.",
            )
            return
        others = [field for field in draft.ordered_fields if not field.is_required]
        if not others:
            QMessageBox.information(
                self,
                "Nothing left to require",
                "Every field on this template is already required, so a rule would have "
                "nothing to add.",
            )
            return

        dialog = _RuleDialog(choices, others, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.rule is None:
            return
        draft.rules.append(dialog.rule)
        self._mark_dirty()
        self._refresh_rules()

    def _remove_rule(self) -> None:
        if self._draft is None:
            return
        item = self.rule_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing selected", "Select a rule first.")
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        self._draft.rules = [rule for rule in self._draft.rules if rule.rule_id != rule_id]
        self._mark_dirty()
        self._refresh_rules()

    # ------------------------------------------------------------------ #
    # Template-level actions
    # ------------------------------------------------------------------ #

    def _new_template(self) -> None:
        name, accepted = QInputDialog.getText(self, "New template", "Name:")
        name = name.strip()
        if not accepted or not name:
            return
        template = LogTemplate(template_id=new_id(), name=name)
        self._store.upsert_template(template)
        self._reload_list(template.template_id)
        QMessageBox.information(
            self,
            "Template created",
            "It has no fields yet. Add them on the right, or start from a duplicate of "
            "one of the built-ins instead.",
        )

    def _duplicate_template(self) -> None:
        """Copy a template under fresh ids.

        New ids throughout, deliberately. Sharing a ``field_id`` with the original
        would make the two templates' answers indistinguishable in every query and
        every export, which is exactly the confusion this feature exists to remove.
        """
        if self._draft is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Duplicate template", "Name for the copy:", text=f"{self._draft.name} (copy)"
        )
        name = name.strip()
        if not accepted or not name:
            return

        copy = _with_new_ids(self._draft, name)
        self._store.upsert_template(copy)
        self._reload_list(copy.template_id)

    def _delete_template(self) -> None:
        if self._draft is None:
            return
        if self._draft.is_builtin:
            QMessageBox.information(
                self,
                "Built-in template",
                "The templates that ship cannot be deleted. Duplicate one and change "
                "the copy, or use “Restore shipped version” to undo your edits.",
            )
            return

        used = self._store.template_usage(self._draft.template_id)
        if used:
            QMessageBox.warning(
                self,
                "Template in use",
                f"{used} {'entry' if used == 1 else 'entries'} were written on "
                f"“{self._draft.name}”. Deleting it would leave them unreadable, so it "
                "has to stay.",
            )
            return

        if (
            QMessageBox.question(
                self,
                "Delete template",
                f"Delete “{self._draft.name}”?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._store.delete_template(self._draft.template_id)
        self._draft = None
        self._reload_list()

    def _restore_builtin(self) -> None:
        if self._draft is None:
            return
        shipped = BUILTIN_TEMPLATES_BY_ID.get(self._draft.template_id)
        if shipped is None:
            return
        if (
            QMessageBox.question(
                self,
                "Restore shipped version",
                f"Put “{shipped.name}” back the way it came?\n\nYour changes to the form "
                "are discarded. Entries already written keep every answer they hold.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        restored = shipped.model_copy(deep=True)
        # The version has to advance past whatever is stored, or a server holding the
        # edited one would treat the restore as the stale copy and keep the edit.
        restored.version = max(shipped.version, self._draft.version + 1)
        restored.is_customised = False
        self._store.upsert_template(restored)
        self._reload_list(restored.template_id)

    def _export_template(self) -> None:
        if self._draft is None:
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export template",
            f"{self._draft.name}.evrevtemplate.json",
            "Template (*.json)",
        )
        if not target:
            return
        try:
            Path(target).write_text(
                self._draft.model_dump_json(indent=2, exclude={"is_customised"}),
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Template exported",
            f"Saved to:\n{target}\n\nWhoever imports it gets the same questions, types and rules.",
        )

    def _import_template(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self, "Import template", "", "Template (*.json);;All files (*)"
        )
        if not source:
            return
        try:
            template = LogTemplate.model_validate_json(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Could not import",
                f"That file is not a template this version understands.\n\n{exc}",
            )
            return

        existing = self._store.get_template(template.template_id)
        if existing is not None:
            answer = QMessageBox.question(
                self,
                "Already have that template",
                f"“{existing.name}” is already here.\n\nReplace it, or import as a separate copy?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.SaveAll
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.SaveAll,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.SaveAll:
                template = _with_new_ids(template, f"{template.name} (imported)")
            else:
                # Replacing keeps the ids, so answers already recorded still resolve.
                template.version = max(template.version, existing.version + 1)

        # An imported template is never claimed as one of ours, whatever the file says.
        template.is_builtin = False
        template.is_customised = False
        self._store.upsert_template(template)
        self._reload_list(template.template_id)
        QMessageBox.information(self, "Template imported", f"“{template.name}” is ready to use.")

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #

    def _draft_problems(self) -> list[str]:
        draft = self._draft
        if draft is None:
            return []
        problems: list[str] = []
        if not self.name_edit.text().strip():
            problems.append("The template needs a name.")
        if not draft.fields:
            problems.append("A template with no fields would record nothing. Add at least one.")

        keys: dict[str, int] = {}
        for field in draft.fields:
            keys[field.field_key] = keys.get(field.field_key, 0) + 1
        for key, count in keys.items():
            if count > 1:
                problems.append(f"Two fields are both named “{key}”.")

        # Not fatal, but worth saying: without a summary field the log's wide column
        # falls back to the first prose field, which may not be the useful one.
        return problems

    def _save_draft(self) -> None:
        draft = self._draft
        if draft is None:
            return
        problems = self._draft_problems()
        if problems:
            self.problem_label.setText("\n".join(f"• {problem}" for problem in problems))
            self.problem_label.show()
            return
        self.problem_label.hide()

        draft.name = self.name_edit.text().strip()
        draft.description = self.description_edit.text().strip()
        # Every edit advances the version. It is what the server uses to decide whose
        # edit wins, and what stops a new release's shipped copy overwriting this one.
        draft.version += 1
        draft.is_customised = True
        self._renumber()
        self._store.upsert_template(draft)
        self._dirty = False
        self._reload_list(draft.template_id)

        if draft.summary_field is None:
            QMessageBox.information(
                self,
                "Saved",
                "Saved. No field is marked as the summary, so the session log will show "
                "the first long-text field in its wide column.",
            )

    def _use_for_case(self) -> None:
        if self._draft is None:
            return
        if self._dirty:
            QMessageBox.information(
                self, "Unsaved changes", "Save the template first, then set it for the case."
            )
            return
        self._store.set_case_template(self._case_id, self._draft.template_id)
        QMessageBox.information(
            self,
            "Template set",
            f"New entries in this case will use “{self._draft.name}”.\n\nEntries already "
            "written keep the form they were written on, so nothing already recorded "
            "changes.",
        )

    @property
    def draft(self) -> LogTemplate | None:
        """The template currently open, for tests and for the caller to refresh on."""
        return self._draft


def _with_new_ids(template: LogTemplate, name: str) -> LogTemplate:
    """A copy of a template under fresh ids throughout.

    Field ids are regenerated too. Sharing them with the original would make the two
    templates' answers indistinguishable in every query and export.
    """
    copy = template.model_copy(deep=True)
    copy.template_id = new_id()
    copy.name = name
    copy.version = 1
    copy.is_builtin = False
    copy.is_customised = False

    remap: dict[str, str] = {}
    for field in copy.fields:
        remap[field.field_id] = new_id()
        field.field_id = remap[field.field_id]
        for option in field.options:
            option.option_id = new_id()
    for rule in copy.rules:
        rule.rule_id = new_id()
        rule.when_field_id = remap.get(rule.when_field_id, rule.when_field_id)
        rule.requires_field_id = remap.get(rule.requires_field_id, rule.requires_field_id)
    return copy


class _RuleDialog(QDialog):
    """ "Require Y when X is V", assembled from the template's own fields."""

    def __init__(self, choices, others, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add rule")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._rule: TemplateRule | None = None

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.when_combo = QComboBox()
        for field in choices:
            self.when_combo.addItem(field.label, field.field_id)
        self.when_combo.currentIndexChanged.connect(self._refresh_values)
        form.addRow(field_label("When"), self.when_combo)

        self.value_combo = QComboBox()
        form.addRow(field_label("is"), self.value_combo)

        self.requires_combo = QComboBox()
        for field in others:
            self.requires_combo.addItem(field.label, field.field_id)
        form.addRow(field_label("then require"), self.requires_combo)

        outer.addLayout(form)
        outer.addWidget(
            hint_label(
                "The required field is marked as soon as that answer is chosen, and the "
                "entry cannot be saved without it."
            )
        )

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        outer.addWidget(box)

        self._choices = {field.field_id: field for field in choices}
        self._refresh_values()

    def _refresh_values(self) -> None:
        self.value_combo.clear()
        field = self._choices.get(self.when_combo.currentData())
        if field is None:
            return
        for option in sorted(field.options, key=lambda o: o.position):
            self.value_combo.addItem(option.label, option.value)

    def _on_accept(self) -> None:
        when_field = self.when_combo.currentData()
        value = self.value_combo.currentData()
        requires = self.requires_combo.currentData()
        if not (when_field and value and requires):
            self.reject()
            return
        if when_field == requires:
            QMessageBox.information(
                self,
                "Not a useful rule",
                "A field cannot require itself. Pick a different field to require.",
            )
            return
        self._rule = TemplateRule(
            rule_id=new_id(),
            when_field_id=when_field,
            when_value=str(value),
            requires_field_id=requires,
        )
        self.accept()

    @property
    def rule(self) -> TemplateRule | None:
        return self._rule
