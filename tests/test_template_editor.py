"""The template editor: building a form, and not damaging records while doing it.

Two things are guarded here above all others, because they are the ones that would
quietly destroy a reviewer's work:

* renaming or removing a field must not touch an answer already recorded, and
* duplicating a template must not leave the copy sharing field ids with the original,
  which would make the two templates' answers indistinguishable everywhere.

Every dialog this module raises is patched out. A modal in a headless test does not
ask anything -- it blocks forever.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from evidence_review.builtin_templates import (
    CRIMINAL_TEMPLATE,
    DEFAULT_TEMPLATE_ID,
    PARANORMAL_TEMPLATE,
)
from evidence_review.models import Case
from evidence_review.store import LocalStore
from evidence_review.templates import FieldOption, FieldRole, FieldType, LogTemplate, TemplateField
from evidence_review.ui import template_editor as editor_module
from evidence_review.ui.field_editor import slugify_key
from evidence_review.ui.template_editor import TemplateEditorDialog, _with_new_ids

from .conftest import make_entry


@pytest.fixture
def quiet(monkeypatch):
    """Answer every prompt, so nothing blocks. Returns the dial to set an answer with."""
    answers = {"text": "Named by the test", "question": QMessageBox.StandardButton.Yes}
    monkeypatch.setattr(
        editor_module.QInputDialog, "getText", staticmethod(lambda *a, **k: (answers["text"], True))
    )
    monkeypatch.setattr(
        editor_module.QMessageBox, "question", staticmethod(lambda *a, **k: answers["question"])
    )
    monkeypatch.setattr(
        editor_module.QMessageBox, "information", staticmethod(lambda *a, **k: None)
    )
    monkeypatch.setattr(editor_module.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    return answers


@pytest.fixture
def editor(qt_app, store: LocalStore, quiet):
    store.upsert_case(Case(case_id="case-a", name="Case A"))
    dialog = TemplateEditorDialog(store, case_id="case-a")
    yield dialog
    dialog.deleteLater()


def select(dialog: TemplateEditorDialog, name_fragment: str) -> None:
    for row in range(dialog.template_list.count()):
        if name_fragment in dialog.template_list.item(row).text():
            dialog.template_list.setCurrentRow(row)
            return
    raise AssertionError(f"no template matching {name_fragment!r}")


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #


def test_it_opens_on_the_templates_that_exist(editor) -> None:
    labels = [editor.template_list.item(i).text() for i in range(editor.template_list.count())]
    assert len(labels) == 2
    assert all("built-in" in label for label in labels)


def test_the_list_says_how_many_entries_use_a_template(qt_app, store: LocalStore, quiet) -> None:
    """A reviewer deciding whether to change a form needs to know what it is holding."""
    store.save(make_entry())
    store.save(make_entry())
    dialog = TemplateEditorDialog(store, case_id="case-a")
    try:
        select(dialog, "Paranormal")
        label = dialog.template_list.currentItem().text()
        assert "2 entries" in label, label
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------- #
# Duplicating
# --------------------------------------------------------------------------- #


def test_duplicating_renumbers_every_id(editor, store: LocalStore, quiet) -> None:
    """Sharing a field_id with the original would make the two templates' answers
    indistinguishable in every query and every export."""
    select(editor, "Paranormal")
    quiet["text"] = "My Own Form"
    editor._duplicate_template()

    copy = editor.draft
    assert copy.name == "My Own Form"
    assert copy.template_id != PARANORMAL_TEMPLATE.template_id
    assert not copy.is_builtin
    assert len(copy.fields) == len(PARANORMAL_TEMPLATE.fields)

    original_ids = {field.field_id for field in PARANORMAL_TEMPLATE.fields}
    assert not ({field.field_id for field in copy.fields} & original_ids)
    assert not (
        {option.option_id for field in copy.fields for option in field.options}
        & {option.option_id for field in PARANORMAL_TEMPLATE.fields for option in field.options}
    )


def test_a_duplicated_rule_points_at_the_copys_own_fields(editor, quiet) -> None:
    """Remapped, not copied verbatim. A rule still naming the original's fields would
    never fire, and would be impossible to see was broken."""
    select(editor, "Paranormal")
    quiet["text"] = "Copy With Rules"
    editor._duplicate_template()

    copy = editor.draft
    ids = {field.field_id for field in copy.fields}
    assert len(copy.rules) == 1
    assert copy.rules[0].when_field_id in ids
    assert copy.rules[0].requires_field_id in ids
    assert copy.rules[0].rule_id != PARANORMAL_TEMPLATE.rules[0].rule_id


def test_a_duplicate_starts_at_version_one(editor, quiet) -> None:
    select(editor, "Criminal")
    quiet["text"] = "Fresh"
    editor._duplicate_template()
    assert editor.draft.version == 1
    assert editor.draft.is_customised is False


# --------------------------------------------------------------------------- #
# Editing fields
# --------------------------------------------------------------------------- #


def test_renaming_a_field_keeps_its_answers(editor, store: LocalStore) -> None:
    """field_id is a uuid and the label is not, exactly so this holds. A reviewer will
    rename a field, and it must not orphan the entries that answered it."""
    entry = make_entry(values={PARANORMAL_TEMPLATE.field_by_key("area").field_id: "Attic"})
    store.save(entry)

    select(editor, "Paranormal")
    area = editor.draft.field_by_key("area")
    field_id = area.field_id
    area.label = "Room"
    area.field_key = "room"
    editor._mark_dirty()
    editor._save_draft()

    assert store.get(entry.entry_id).values[field_id] == "Attic"
    assert store.get_template(DEFAULT_TEMPLATE_ID).field_by_key("room").label == "Room"


def test_removing_a_field_keeps_its_answers(editor, store: LocalStore, quiet) -> None:
    """Unreachable but not erased. Editing a form is not a reason to delete a record
    made with it, and the reviewer may put the field back."""
    field_id = PARANORMAL_TEMPLATE.field_by_key("debunk_reasoning").field_id
    entry = make_entry(values={field_id: "A pipe"})
    store.save(entry)

    select(editor, "Paranormal")
    row = next(
        index
        for index, field in enumerate(editor.draft.ordered_fields)
        if field.field_id == field_id
    )
    editor.field_table.selectRow(row)
    editor._remove_field()
    editor._save_draft()

    assert store.get_template(DEFAULT_TEMPLATE_ID).field(field_id) is None
    kept = store.conn.execute(
        "SELECT value_text FROM entry_values WHERE entry_id = ? AND field_id = ?",
        (entry.entry_id, field_id),
    ).fetchone()
    assert kept is not None and kept["value_text"] == "A pipe"


def test_removing_a_field_removes_the_rules_that_name_it(editor, quiet) -> None:
    """A rule pointing at a field that no longer exists would never fire and could not
    be understood in the list."""
    select(editor, "Paranormal")
    field_id = PARANORMAL_TEMPLATE.field_by_key("debunk_reasoning").field_id
    row = next(
        index
        for index, field in enumerate(editor.draft.ordered_fields)
        if field.field_id == field_id
    )
    editor.field_table.selectRow(row)
    editor._remove_field()

    assert editor.draft.rules == []


def test_a_role_belongs_to_one_field_at_a_time(editor) -> None:
    """Two summary fields would make "the wide column" ambiguous, and the engine would
    silently pick whichever came first."""
    select(editor, "Paranormal")
    draft = editor.draft
    area = draft.field_by_key("area")
    area.role = FieldRole.SUMMARY
    editor._take_role(area)

    holders = [field.label for field in draft.fields if field.role is FieldRole.SUMMARY]
    assert holders == ["Area"]


def test_moving_a_field_changes_the_form_order(editor) -> None:
    select(editor, "Paranormal")
    before = [field.field_key for field in editor.draft.ordered_fields]
    editor.field_table.selectRow(0)
    editor._move_field(1)
    after = [field.field_key for field in editor.draft.ordered_fields]

    assert after[0] == before[1]
    assert after[1] == before[0]
    assert [field.position for field in editor.draft.ordered_fields] == [0, 1, 2, 3]


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #


def test_saving_bumps_the_version_and_marks_it_edited(editor, store: LocalStore) -> None:
    """The version is what the server uses to decide whose edit wins, and what stops a
    new release's shipped copy overwriting this one."""
    select(editor, "Paranormal")
    before = editor.draft.version
    editor.name_edit.setText("Paranormal, my way")
    editor._save_draft()

    stored = store.get_template(DEFAULT_TEMPLATE_ID)
    assert stored.version == before + 1
    assert stored.is_customised is True
    assert stored.name == "Paranormal, my way"


def test_a_template_with_no_fields_is_refused(editor, store: LocalStore, quiet) -> None:
    """It would record nothing, and the reviewer would not find out until they tried
    to log an entry."""
    quiet["text"] = "Empty"
    editor._new_template()
    select(editor, "Empty")
    editor._save_draft()

    assert editor.problem_label.isVisibleTo(editor)
    assert "no fields" in editor.problem_label.text()


def test_two_fields_cannot_share_a_name(editor) -> None:
    select(editor, "Paranormal")
    draft = editor.draft
    draft.fields.append(TemplateField(field_id="dupe", field_key="area", label="Another Area"))
    editor._save_draft()

    assert editor.problem_label.isVisibleTo(editor)
    assert "area" in editor.problem_label.text()


def test_nothing_is_written_until_save(editor, store: LocalStore) -> None:
    """The draft is a copy, so Cancel really does discard."""
    select(editor, "Paranormal")
    editor.draft.name = "Not saved"
    editor.draft.fields = []

    assert store.get_template(DEFAULT_TEMPLATE_ID).name == "Paranormal Investigation"
    assert len(store.get_template(DEFAULT_TEMPLATE_ID).fields) == 4


# --------------------------------------------------------------------------- #
# Deleting and restoring
# --------------------------------------------------------------------------- #


def test_a_builtin_cannot_be_deleted(editor, store: LocalStore, quiet) -> None:
    select(editor, "Paranormal")
    editor._delete_template()
    assert store.get_template(DEFAULT_TEMPLATE_ID) is not None


def test_a_template_in_use_cannot_be_deleted(editor, store: LocalStore, quiet) -> None:
    """Deleting it would leave the entries written on it unreadable."""
    quiet["text"] = "Doomed"
    editor._new_template()
    select(editor, "Doomed")
    doomed_id = editor.draft.template_id

    store.save(make_entry(template_id=doomed_id))
    editor._delete_template()

    assert store.get_template(doomed_id) is not None


def test_an_unused_template_can_be_deleted(editor, store: LocalStore, quiet) -> None:
    quiet["text"] = "Spare"
    editor._new_template()
    select(editor, "Spare")
    spare_id = editor.draft.template_id

    editor._delete_template()
    assert store.get_template(spare_id) is None


def test_restoring_a_builtin_puts_the_shipped_form_back(editor, store: LocalStore, quiet) -> None:
    select(editor, "Paranormal")
    editor.draft.fields = [editor.draft.fields[0]]
    editor.draft.rules = []
    editor._save_draft()
    assert len(store.get_template(DEFAULT_TEMPLATE_ID).fields) == 1

    select(editor, "Paranormal")
    editor._restore_builtin()

    restored = store.get_template(DEFAULT_TEMPLATE_ID)
    assert len(restored.fields) == 4
    assert restored.is_customised is False
    # The version has to advance past the edit, or a server holding the edited one
    # would treat the restore as the stale copy and keep the edit.
    assert restored.version > 2


def test_restoring_does_not_touch_answers(editor, store: LocalStore, quiet) -> None:
    field_id = PARANORMAL_TEMPLATE.field_by_key("area").field_id
    entry = make_entry(values={field_id: "Attic"})
    store.save(entry)

    select(editor, "Paranormal")
    editor.draft.fields = [field for field in editor.draft.fields if field.field_id != field_id]
    editor.draft.rules = []
    editor._save_draft()
    select(editor, "Paranormal")
    editor._restore_builtin()

    assert store.get(entry.entry_id).values[field_id] == "Attic"


# --------------------------------------------------------------------------- #
# Using it for a case
# --------------------------------------------------------------------------- #


def test_setting_a_template_for_a_case_leaves_existing_entries_alone(
    editor, store: LocalStore, quiet
) -> None:
    """An entry keeps the form it was written on. A record that silently
    re-interprets itself is not a record."""
    before = make_entry(case_id="case-a")
    store.save(before)

    select(editor, "Criminal")
    editor._use_for_case()

    assert store.template_for_case("case-a").template_id == CRIMINAL_TEMPLATE.template_id
    assert store.get(before.entry_id).template_id == DEFAULT_TEMPLATE_ID


def test_an_unsaved_draft_is_not_set_for_a_case(editor, store: LocalStore, quiet) -> None:
    select(editor, "Criminal")
    editor._mark_dirty()
    editor._use_for_case()

    assert store.get_case("case-a").template_id is None


# --------------------------------------------------------------------------- #
# Sharing
# --------------------------------------------------------------------------- #


def test_a_template_survives_a_round_trip_through_a_file(tmp_path) -> None:
    """What somebody shares has to arrive as the same form, down to the colours."""
    original = LogTemplate(
        template_id="shared",
        name="Shared",
        fields=[
            TemplateField(
                field_id="shared.grade",
                field_key="grade",
                label="Grade",
                field_type=FieldType.CHOICE,
                role=FieldRole.STATUS,
                options=[
                    FieldOption(option_id="g0", value="Low", colour="#112233", position=0),
                    FieldOption(option_id="g1", value="High", colour="#445566", position=1),
                ],
            ),
            TemplateField(
                field_id="shared.depth",
                field_key="depth",
                label="Depth",
                field_type=FieldType.NUMBER,
                config={"minimum": 0, "maximum": 99},
            ),
        ],
    )
    path = tmp_path / "shared.json"
    path.write_text(original.model_dump_json(indent=2), encoding="utf-8")

    restored = LogTemplate.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored.model_dump() == original.model_dump()
    assert restored.colour_for_status("Low") == "#112233"
    assert restored.field_by_key("depth").config["maximum"] == 99


def test_importing_as_a_copy_cannot_collide_with_what_is_already_here() -> None:
    copy = _with_new_ids(CRIMINAL_TEMPLATE, "Criminal (imported)")

    assert copy.template_id != CRIMINAL_TEMPLATE.template_id
    assert not (
        {field.field_id for field in copy.fields}
        & {field.field_id for field in CRIMINAL_TEMPLATE.fields}
    )
    # And its rules still work, pointing at the copy's own fields.
    ids = {field.field_id for field in copy.fields}
    assert all(rule.requires_field_id in ids for rule in copy.rules)
    assert all(rule.when_field_id in ids for rule in copy.rules)


# --------------------------------------------------------------------------- #
# Field naming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Burn Depth (mm)", "burn_depth_mm"),
        ("Location / Camera", "location_camera"),
        ("  Vehicles  ", "vehicles"),
        ("!!!", "field"),
    ],
)
def test_a_field_name_is_derived_from_its_label(label, expected) -> None:
    assert slugify_key(label) == expected


# --------------------------------------------------------------------------- #
# The field dialog
# --------------------------------------------------------------------------- #


@pytest.fixture
def field_dialog(qt_app):
    """Open the field dialog on a template, and tidy up after."""
    from evidence_review.ui.field_editor import FieldEditorDialog

    made: list = []

    def open_on(template, field=None, usage=0):
        dialog = FieldEditorDialog(template=template, field=field, usage=usage)
        made.append(dialog)
        return dialog

    yield open_on
    for dialog in made:
        dialog.deleteLater()


def test_the_name_follows_the_label_until_it_is_typed_in(field_dialog) -> None:
    dialog = field_dialog(LogTemplate(template_id="t", name="T"))
    dialog.label_edit.setText("Burn Depth (mm)")
    assert dialog.key_edit.text() == "burn_depth_mm"

    # Once the reviewer edits the name themselves, it stops being overwritten.
    dialog.key_edit.setText("depth")
    dialog.key_edit.setModified(True)
    dialog.label_edit.setText("Something Else")
    assert dialog.key_edit.text() == "depth"


def test_editing_a_field_never_changes_its_id(field_dialog) -> None:
    """The id is what every recorded answer points at."""
    dialog = field_dialog(PARANORMAL_TEMPLATE, PARANORMAL_TEMPLATE.field_by_key("area"))
    dialog.label_edit.setText("Room")
    dialog.key_edit.setText("room")
    dialog._on_save()

    assert dialog.field is not None
    assert dialog.field.field_id == PARANORMAL_TEMPLATE.field_by_key("area").field_id


def test_a_new_field_gets_a_fresh_id(field_dialog) -> None:
    dialog = field_dialog(PARANORMAL_TEMPLATE)
    dialog.label_edit.setText("Smell Noticed")
    dialog._on_save()

    assert dialog.field is not None
    existing = {field.field_id for field in PARANORMAL_TEMPLATE.fields}
    assert dialog.field.field_id not in existing


def test_a_name_already_taken_is_refused(field_dialog) -> None:
    dialog = field_dialog(PARANORMAL_TEMPLATE)
    dialog.label_edit.setText("Another Area")
    dialog.key_edit.setText("area")
    dialog._on_save()

    assert dialog.field is None
    assert "already named" in dialog.problem_label.text()


def test_a_choice_field_needs_at_least_one_choice(field_dialog) -> None:
    """Otherwise it is a dropdown that cannot be answered, and a required one could
    never be satisfied."""
    dialog = field_dialog(LogTemplate(template_id="t", name="T"))
    dialog.label_edit.setText("Grade")
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.CHOICE.value))
    dialog._on_save()

    assert dialog.field is None
    assert "at least one choice" in dialog.problem_label.text()


def test_a_number_range_cannot_be_backwards(field_dialog) -> None:
    dialog = field_dialog(LogTemplate(template_id="t", name="T"))
    dialog.label_edit.setText("Count")
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.NUMBER.value))
    dialog.minimum_spin.setValue(10)
    dialog.maximum_spin.setValue(5)
    dialog._on_save()

    assert dialog.field is None
    assert "cannot start above" in dialog.problem_label.text()


def test_only_the_settings_a_type_has_are_shown(field_dialog) -> None:
    """A "lines shown" box on a yes/no field is a question with no meaning."""
    dialog = field_dialog(LogTemplate(template_id="t", name="T"))

    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.MULTILINE.value))
    assert dialog.rows_spin.isVisibleTo(dialog)
    assert not dialog.options_widget.isVisibleTo(dialog)

    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.CHOICE.value))
    assert dialog.options_widget.isVisibleTo(dialog)
    assert not dialog.rows_spin.isVisibleTo(dialog)

    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.BOOLEAN.value))
    assert not dialog.rows_spin.isVisibleTo(dialog)
    assert not dialog.length_spin.isVisibleTo(dialog)
    assert not dialog.bounds_widget.isVisibleTo(dialog)


def test_changing_the_type_of_a_field_in_use_says_what_will_happen(field_dialog) -> None:
    """Nothing is deleted, but an answer that cannot be read as the new type gets
    flagged -- and the reviewer has to be told that before agreeing to it."""
    area = PARANORMAL_TEMPLATE.field_by_key("area")
    dialog = field_dialog(PARANORMAL_TEMPLATE, area, usage=7)
    assert not dialog.type_warning.isVisibleTo(dialog), "no change yet, nothing to warn about"

    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.NUMBER.value))
    assert dialog.type_warning.isVisibleTo(dialog)
    text = dialog.type_warning.text()
    assert "7 entries" in text
    assert "Nothing is deleted" in text


def test_a_type_change_that_keeps_the_same_storage_says_so(field_dialog) -> None:
    """Text to long-text cannot invalidate anything, and should not be made to sound
    as though it might."""
    area = PARANORMAL_TEMPLATE.field_by_key("area")
    dialog = field_dialog(PARANORMAL_TEMPLATE, area, usage=3)
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.MULTILINE.value))

    assert dialog.type_warning.isVisibleTo(dialog)
    assert "kept as they are" in dialog.type_warning.text()


def test_taking_a_role_from_another_field_is_announced(field_dialog) -> None:
    """One field per role, so saving this moves it -- which the reviewer should know
    before they do it, not afterwards."""
    dialog = field_dialog(PARANORMAL_TEMPLATE)
    dialog.label_edit.setText("Headline")
    dialog.role_combo.setCurrentIndex(dialog.role_combo.findData(FieldRole.SUMMARY.value))

    assert "What Was Heard or Seen" in dialog.role_hint.text()


def test_the_first_choice_added_becomes_the_default(field_dialog, monkeypatch) -> None:
    from evidence_review.ui import field_editor as field_module

    dialog = field_dialog(LogTemplate(template_id="t", name="T"))
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(FieldType.CHOICE.value))

    for value in ("Low", "High"):
        monkeypatch.setattr(
            field_module.QInputDialog, "getText", staticmethod(lambda *a, v=value, **k: (v, True))
        )
        dialog._add_option()

    dialog.label_edit.setText("Grade")
    dialog._on_save()
    assert dialog.field is not None
    assert dialog.field.default_value == "Low"
    assert [option.value for option in dialog.field.options] == ["Low", "High"]
