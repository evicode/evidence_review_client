"""Log templates: the form a case is reviewed with, defined as data.

What these guard is that the engine has no idea the built-ins are special. Every
assertion below would hold for a template a reviewer invents, which is the whole
claim being made -- if Paranormal Investigation needed a code path of its own, a
user's template would never work.
"""

from __future__ import annotations

import datetime as _dt
import threading

import pytest

from evidence_review.builtin_templates import (
    BUILTIN_TEMPLATES,
    CRIMINAL_TEMPLATE,
    DEFAULT_TEMPLATE_ID,
    PARANORMAL_TEMPLATE,
)
from evidence_review.models import DEFAULT_STATUSES, EntryRow
from evidence_review.store import LocalStore
from evidence_review.templates import (
    FieldOption,
    FieldRole,
    FieldType,
    LogTemplate,
    TemplateField,
    TemplateRule,
)
from evidence_review.ui.theme import STATUS_COLOURS

from .conftest import OBSERVATION, make_entry


def field_of(template: LogTemplate, key: str) -> str:
    return template.field_by_key(key).field_id


# --------------------------------------------------------------------------- #
# The paranormal template must reproduce the form that shipped
# --------------------------------------------------------------------------- #


def test_the_paranormal_template_is_the_form_that_shipped() -> None:
    """Phase one's whole promise: nothing the reviewer sees changes.

    That the built-in comes out identical to the four hardcoded fields is the proof
    that the engine is right, so this is the test that matters most."""
    assert [field.label for field in PARANORMAL_TEMPLATE.ordered_fields] == [
        "Area",
        "What Was Heard or Seen",
        "Debunk Reasoning",
        "Status",
    ]
    assert tuple(PARANORMAL_TEMPLATE.statuses) == DEFAULT_STATUSES
    for status in PARANORMAL_TEMPLATE.statuses:
        assert PARANORMAL_TEMPLATE.colour_for_status(status) == STATUS_COLOURS[status], (
            f"{status} must keep the colour the log has always painted it"
        )


def test_the_debunk_rule_is_data_not_an_if_statement() -> None:
    """It used to be a hardcoded check in the entry model, which meant no other
    template could ever have a conditional field."""
    values = {
        field_of(PARANORMAL_TEMPLATE, "area"): "Cellar",
        field_of(PARANORMAL_TEMPLATE, "observation"): "A figure",
        field_of(PARANORMAL_TEMPLATE, "status"): "Debunked",
    }
    problems = PARANORMAL_TEMPLATE.validation_problems(values)
    assert problems == ["'Debunk Reasoning' is required when Status is Debunked."]

    values[field_of(PARANORMAL_TEMPLATE, "debunk_reasoning")] = "A coat on a hook"
    assert PARANORMAL_TEMPLATE.validation_problems(values) == []


def test_the_same_rule_mechanism_serves_a_different_template() -> None:
    """The criminal template's follow-up rule is the same table row, not a second
    special case in the code."""
    values = {
        field_of(CRIMINAL_TEMPLATE, "location"): "Cam 4",
        field_of(CRIMINAL_TEMPLATE, "observation"): "Male enters",
        field_of(CRIMINAL_TEMPLATE, "status"): "Requires Follow-up",
    }
    assert CRIMINAL_TEMPLATE.validation_problems(values) == [
        "'Follow-up Action' is required when Status is Requires Follow-up."
    ]


# --------------------------------------------------------------------------- #
# Field types mean something
# --------------------------------------------------------------------------- #


def test_the_criminal_template_is_not_the_paranormal_one_relabelled() -> None:
    """The cheap version of this feature renames the four existing fields. It cannot
    give a detective Persons *and* Vehicles *and* an exhibit reference, and it cannot
    make a time a time."""
    keys = {field.field_key for field in CRIMINAL_TEMPLATE.fields}
    assert {"persons", "vehicles", "exhibit_reference", "actual_time"} <= keys

    # No field_id is shared with the paranormal template, so a query for one
    # template's answers can never return the other's.
    paranormal_ids = {field.field_id for field in PARANORMAL_TEMPLATE.fields}
    criminal_ids = {field.field_id for field in CRIMINAL_TEMPLATE.fields}
    assert not (paranormal_ids & criminal_ids)


@pytest.mark.parametrize(
    ("field_type", "raw", "expected"),
    [
        (FieldType.NUMBER, "12.5", 12.5),
        (FieldType.NUMBER, 3, 3.0),
        (FieldType.BOOLEAN, "yes", True),
        (FieldType.BOOLEAN, "", False),
        (FieldType.DATE, "2026-03-03", _dt.date(2026, 3, 3)),
        (FieldType.TEXT, "  padded  ", "padded"),
        (FieldType.TEXT, "   ", None),
    ],
)
def test_a_value_is_coerced_to_its_field_type(field_type, raw, expected) -> None:
    field = TemplateField(field_id="f", field_key="f", label="F", field_type=field_type)
    assert field.coerce(raw) == expected


def test_a_value_that_cannot_be_read_is_kept_and_complained_about() -> None:
    """Discarding what somebody typed because it did not parse is the one thing a
    field must never do -- they cannot correct what they cannot see."""
    field = TemplateField(field_id="f", field_key="n", label="Count", field_type=FieldType.NUMBER)
    kept = field.coerce("about twelve")
    assert kept == "about twelve"
    assert field.problems(kept) == ["'Count' must be a number."]


def test_typed_fields_are_stored_in_typed_columns() -> None:
    """A number sorts as a number and a date compares as a date. One stringly-typed
    column would put 100 before 99."""
    assert FieldType.NUMBER.storage == "value_number"
    assert FieldType.BOOLEAN.storage == "value_number"
    assert FieldType.DATE.storage == "value_date"
    assert FieldType.TIME.storage == "value_text"
    assert FieldType.CHOICE.storage == "value_text"


def test_a_time_is_validated_as_a_time() -> None:
    field = CRIMINAL_TEMPLATE.field(field_of(CRIMINAL_TEMPLATE, "actual_time"))
    assert field.problems("23:14") == []
    assert field.problems("23:14:05") == []
    assert field.problems("9pm") == ["'Actual Time' must be a time, as 23:14 or 23:14:05."]
    assert field.problems("25:00") == ["'Actual Time' must be a time, as 23:14 or 23:14:05."]


def test_a_choice_rejects_a_value_it_does_not_list() -> None:
    field = PARANORMAL_TEMPLATE.status_field
    assert field.problems("Unexplained") == []
    assert "must be one of" in field.problems("Haunted")[0]


def test_a_judgement_field_has_no_default() -> None:
    """Pre-filling Evidential Significance with "Background" would put a judgement
    into the record that nobody made."""
    assert (
        CRIMINAL_TEMPLATE.default_values().get(field_of(CRIMINAL_TEMPLATE, "significance")) is None
    )
    assert (
        CRIMINAL_TEMPLATE.default_values()[field_of(CRIMINAL_TEMPLATE, "status")] == "Needs Review"
    )


# --------------------------------------------------------------------------- #
# Roles, so behaviour is driven by data and not by field names
# --------------------------------------------------------------------------- #


def test_roles_are_how_the_engine_finds_a_field() -> None:
    """Nothing asks "is this field called Area". Both templates answer the same
    questions about themselves despite sharing no field names."""
    for template in BUILTIN_TEMPLATES:
        assert template.summary_field is not None
        assert template.status_field is not None
        assert template.location_field is not None

    assert PARANORMAL_TEMPLATE.location_field.label == "Area"
    assert CRIMINAL_TEMPLATE.location_field.label == "Location / Camera"


def test_a_template_with_no_roles_still_works() -> None:
    """A reviewer can build a form that declares nothing, and must not get a crash."""
    plain = LogTemplate(
        template_id="plain",
        name="Plain",
        fields=[TemplateField(field_id="plain.note", field_key="note", label="Note")],
    )
    assert plain.summary_field is None
    assert plain.statuses == []
    assert plain.colour_for_status("anything") is None
    # The summary falls back to the first prose field rather than returning nothing.
    assert plain.summarise({"plain.note": "something happened"}) == "something happened"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def test_both_builtins_are_installed_on_a_new_database(store: LocalStore) -> None:
    assert {template.name for template in store.list_templates()} == {
        "Paranormal Investigation",
        "Criminal Investigation",
    }


def test_a_template_round_trips_through_the_store(store: LocalStore) -> None:
    stored = store.get_template(PARANORMAL_TEMPLATE.template_id)
    assert stored is not None
    assert stored.model_dump() == PARANORMAL_TEMPLATE.model_dump()


def test_two_templates_coexist_with_different_shapes(store: LocalStore) -> None:
    """The point of the feature: one database, two investigations, two forms."""
    ghost = make_entry(
        case_id="willow-house",
        template_id=PARANORMAL_TEMPLATE.template_id,
        values={
            field_of(PARANORMAL_TEMPLATE, "area"): "Attic",
            field_of(PARANORMAL_TEMPLATE, "observation"): "A knock",
            field_of(PARANORMAL_TEMPLATE, "status"): "Unexplained",
        },
    )
    crime = EntryRow(
        file_name="cam4.mp4",
        investigator_name="Bob",
        case_id="harbour-street",
        template_id=CRIMINAL_TEMPLATE.template_id,
        values={
            field_of(CRIMINAL_TEMPLATE, "location"): "Cam 4",
            field_of(CRIMINAL_TEMPLATE, "observation"): "Male enters",
            field_of(CRIMINAL_TEMPLATE, "status"): "Subject Identified",
            field_of(CRIMINAL_TEMPLATE, "vehicles"): "AB12 CDE, blue Astra",
            field_of(CRIMINAL_TEMPLATE, "actual_time"): "23:14",
        },
    )
    store.save(ghost)
    store.save(crime)

    assert len(store.get(crime.entry_id).values) == 5
    assert len(store.get(ghost.entry_id).values) == 3
    # Neither entry has picked up a field belonging to the other's form.
    assert not {
        key for key in store.get(ghost.entry_id).values if key.startswith("builtin-criminal")
    }


def test_clearing_an_answer_removes_it(store: LocalStore) -> None:
    """A per-key upsert would leave the old answer behind, so the reviewer could not
    take something back."""
    entry = make_entry(values={field_of(PARANORMAL_TEMPLATE, "debunk_reasoning"): "A pipe"})
    store.save(entry)
    assert field_of(PARANORMAL_TEMPLATE, "debunk_reasoning") in store.get(entry.entry_id).values

    saved = store.get(entry.entry_id)
    del saved.values[field_of(PARANORMAL_TEMPLATE, "debunk_reasoning")]
    store.save(saved)

    reloaded = store.get(entry.entry_id)
    assert field_of(PARANORMAL_TEMPLATE, "debunk_reasoning") not in reloaded.values
    assert reloaded.values[field_of(PARANORMAL_TEMPLATE, "observation")], "the rest must survive"


def test_an_answer_for_an_unknown_field_is_kept(store: LocalStore) -> None:
    """It is a colleague's answer on a template version this client has not pulled.
    Dropping it would destroy their work on the next save."""
    entry = make_entry(values={"some-future-template.field": "written by a newer client"})
    store.save(entry)
    assert store.get(entry.entry_id).values["some-future-template.field"] == (
        "written by a newer client"
    )


def test_the_legacy_columns_are_written_from_the_answers(store: LocalStore) -> None:
    """They are kept only so the PHP web log and a rollback still work. Driven by
    role, so an entry on *any* template projects sensibly."""
    entry = EntryRow(
        file_name="cam4.mp4",
        investigator_name="Bob",
        template_id=CRIMINAL_TEMPLATE.template_id,
        values={
            field_of(CRIMINAL_TEMPLATE, "location"): "Cam 4",
            field_of(CRIMINAL_TEMPLATE, "observation"): "Male enters",
            field_of(CRIMINAL_TEMPLATE, "status"): "Subject Identified",
        },
    )
    store.save(entry)
    row = store.conn.execute(
        "SELECT area, observation, status FROM log_entries WHERE entry_id = ?",
        (entry.entry_id,),
    ).fetchone()
    assert row["area"] == "Cam 4"
    assert row["observation"] == "Male enters"
    assert row["status"] == "Subject Identified"


def test_an_over_long_answer_is_truncated_in_the_projection_only(store: LocalStore) -> None:
    """The old area column holds 200 characters. Refusing to save a valid entry
    because a column on its way out cannot hold its shadow would be absurd."""
    long_location = "A" * 400
    template = LogTemplate(
        template_id="roomy",
        name="Roomy",
        fields=[
            TemplateField(
                field_id="roomy.where",
                field_key="where",
                label="Where",
                field_type=FieldType.TEXT,
                role=FieldRole.LOCATION,
                config={"max_length": 500},
            )
        ],
    )
    store.upsert_template(template)
    entry = EntryRow(
        file_name="x.mp4",
        investigator_name="Tester",
        template_id="roomy",
        values={"roomy.where": long_location},
    )
    store.save(entry)

    assert store.get(entry.entry_id).values["roomy.where"] == long_location, (
        "the real answer is never truncated"
    )
    row = store.conn.execute(
        "SELECT area FROM log_entries WHERE entry_id = ?", (entry.entry_id,)
    ).fetchone()
    assert len(row["area"]) == 200


# --------------------------------------------------------------------------- #
# Search and vocabularies, without an allowlist
# --------------------------------------------------------------------------- #


def test_search_finds_a_value_in_a_field_that_did_not_exist_before(store: LocalStore) -> None:
    """Search used to name four columns. A field a reviewer adds is searchable the
    moment it holds a value, with nothing to update."""
    store.upsert_template(
        LogTemplate(
            template_id="custom",
            name="Custom",
            fields=[
                TemplateField(field_id="custom.smell", field_key="smell", label="Smell Noticed")
            ],
        )
    )
    entry = EntryRow(
        file_name="x.mp4",
        investigator_name="Tester",
        template_id="custom",
        values={"custom.smell": "ozone and burnt dust"},
    )
    store.save(entry)

    assert [found.entry_id for found in store.list_entries(search="ozone")] == [entry.entry_id]


def test_distinct_values_work_for_any_field(store: LocalStore) -> None:
    for area in ("Basement", "Attic", "Basement"):
        store.save(make_entry(values={field_of(PARANORMAL_TEMPLATE, "area"): area}))

    assert store.distinct_field_values(field_of(PARANORMAL_TEMPLATE, "area")) == [
        "Attic",
        "Basement",
    ]
    assert store.distinct_role_values(FieldRole.LOCATION, DEFAULT_TEMPLATE_ID) == [
        "Attic",
        "Basement",
    ]


def test_a_deleted_entry_does_not_pollute_a_vocabulary(store: LocalStore) -> None:
    gone = make_entry(values={field_of(PARANORMAL_TEMPLATE, "area"): "Demolished Wing"})
    store.save(gone)
    store.soft_delete(gone.entry_id)
    assert "Demolished Wing" not in store.distinct_field_values(
        field_of(PARANORMAL_TEMPLATE, "area")
    )


# --------------------------------------------------------------------------- #
# Editing a template
# --------------------------------------------------------------------------- #


def test_a_case_keeps_its_own_template(store: LocalStore) -> None:
    from evidence_review.models import Case

    store.upsert_case(Case(case_id="harbour-street", name="Harbour Street"))
    store.set_case_template("harbour-street", CRIMINAL_TEMPLATE.template_id)

    assert store.template_for_case("harbour-street").template_id == CRIMINAL_TEMPLATE.template_id
    # A case that has not chosen still opens.
    assert store.template_for_case("never-seen").template_id == DEFAULT_TEMPLATE_ID


def test_editing_a_status_list_keeps_the_colours_of_the_ones_that_stay(
    store: LocalStore,
) -> None:
    """Re-ordering the list must not repaint a log that was already readable."""
    store.apply_status_vocabulary(
        "default", ["Unexplained", "Needs Review", "Draught - confirmed"], "Needs Review"
    )
    template = store.template_for_case("default")

    assert template.statuses == ["Unexplained", "Needs Review", "Draught - confirmed"]
    assert template.colour_for_status("Unexplained") == STATUS_COLOURS["Unexplained"]
    assert template.colour_for_status("Draught - confirmed") is None, "a new status has no colour"
    assert template.default_values()[field_of(template, "status")] == "Needs Review"


def test_removing_a_status_does_not_touch_entries_that_hold_it(store: LocalStore) -> None:
    """Editing a form is not a reason to alter a record made with it."""
    entry = make_entry(values={field_of(PARANORMAL_TEMPLATE, "status"): "Contamination"})
    store.save(entry)

    store.apply_status_vocabulary("default", ["Needs Review", "Unexplained"], "Needs Review")

    assert store.get(entry.entry_id).values[field_of(PARANORMAL_TEMPLATE, "status")] == (
        "Contamination"
    )


def test_a_customised_template_is_not_overwritten_by_a_new_release(store: LocalStore) -> None:
    """A vocabulary somebody curated is their work. Re-seeding a shipped template
    over it would silently delete it."""
    store.apply_status_vocabulary("default", ["Only Mine"], "Only Mine")

    # A new release ships a higher version of the same built-in.
    newer = PARANORMAL_TEMPLATE.model_copy(update={"version": 99})
    import evidence_review.store as store_module

    original = store_module.BUILTIN_TEMPLATES
    store_module.BUILTIN_TEMPLATES = (newer, CRIMINAL_TEMPLATE)
    try:
        store.seed_builtin_templates()
    finally:
        store_module.BUILTIN_TEMPLATES = original

    assert store.template_for_case("default").statuses == ["Only Mine"]


def test_deleting_a_template_refuses_a_builtin(store: LocalStore) -> None:
    assert store.delete_template(PARANORMAL_TEMPLATE.template_id) is False
    store.upsert_template(LogTemplate(template_id="mine", name="Mine"))
    assert store.delete_template("mine") is True
    assert store.get_template("mine") is None


def test_usage_counts_back_the_delete_warning(store: LocalStore) -> None:
    """Deleting a field has to say how many entries hold a value for it."""
    store.save(make_entry())
    store.save(make_entry())

    assert store.template_usage(DEFAULT_TEMPLATE_ID) == 2
    assert store.field_usage(field_of(PARANORMAL_TEMPLATE, "observation")) == 2
    assert store.field_usage(field_of(PARANORMAL_TEMPLATE, "debunk_reasoning")) == 0


def test_a_renamed_field_keeps_its_answers(store: LocalStore) -> None:
    """field_id is a uuid and the label is not, precisely so this holds. A reviewer
    will rename a field, and it must not orphan five hundred entries."""
    entry = make_entry(values={field_of(PARANORMAL_TEMPLATE, "area"): "Attic"})
    store.save(entry)

    template = store.get_template(DEFAULT_TEMPLATE_ID)
    area = template.field_by_key("area")
    area.label = "Room"
    area.field_key = "room"
    store.upsert_template(template)

    reloaded = store.get(entry.entry_id)
    assert reloaded.values[area.field_id] == "Attic"
    assert store.get_template(DEFAULT_TEMPLATE_ID).field_by_key("room").label == "Room"


def test_deleting_a_field_leaves_its_answers_intact(store: LocalStore) -> None:
    """Unreachable but not erased. Evidence is not deleted as a side effect of
    editing a form, and the reviewer may put the field back."""
    entry = make_entry(values={field_of(PARANORMAL_TEMPLATE, "debunk_reasoning"): "A pipe"})
    store.save(entry)
    field_id = field_of(PARANORMAL_TEMPLATE, "debunk_reasoning")

    template = store.get_template(DEFAULT_TEMPLATE_ID)
    template.fields = [f for f in template.fields if f.field_id != field_id]
    template.rules = []
    store.upsert_template(template)

    row = store.conn.execute(
        "SELECT value_text FROM entry_values WHERE entry_id = ? AND field_id = ?",
        (entry.entry_id, field_id),
    ).fetchone()
    assert row is not None and row["value_text"] == "A pipe"


# --------------------------------------------------------------------------- #
# The wire format
# --------------------------------------------------------------------------- #


def test_an_entry_and_its_answers_travel_as_one_document() -> None:
    """Relational storage and a document-shaped wire are not in tension. One request
    per entry is what keeps a retry idempotent."""
    entry = make_entry(values={field_of(PARANORMAL_TEMPLATE, "area"): "Attic"})
    payload = entry.wire_payload()

    assert payload["template_id"] == DEFAULT_TEMPLATE_ID
    assert payload["values"][field_of(PARANORMAL_TEMPLATE, "area")] == "Attic"
    # And nothing local-only leaked into it.
    assert "sync_state" not in payload
    assert "local_revision" not in payload


def test_a_date_answer_survives_the_wire_as_a_date() -> None:
    template = LogTemplate(
        template_id="dated",
        name="Dated",
        fields=[
            TemplateField(
                field_id="dated.when",
                field_key="when",
                label="When",
                field_type=FieldType.DATE,
            )
        ],
    )
    entry = EntryRow(
        file_name="x.mp4",
        template_id="dated",
        values={"dated.when": _dt.date(2026, 3, 3)},
    )
    payload = entry.wire_payload()
    assert payload["values"]["dated.when"] == "2026-03-03"
    assert template.coerce_values(payload["values"])["dated.when"] == _dt.date(2026, 3, 3)


def test_a_date_answer_is_stored_in_the_date_column(store: LocalStore) -> None:
    store.upsert_template(
        LogTemplate(
            template_id="dated",
            name="Dated",
            fields=[
                TemplateField(
                    field_id="dated.when",
                    field_key="when",
                    label="When",
                    field_type=FieldType.DATE,
                )
            ],
        )
    )
    entry = EntryRow(
        file_name="x.mp4",
        investigator_name="Tester",
        template_id="dated",
        values={"dated.when": _dt.date(2026, 3, 3)},
    )
    store.save(entry)

    row = store.conn.execute(
        "SELECT value_text, value_number, value_date FROM entry_values WHERE entry_id = ?",
        (entry.entry_id,),
    ).fetchone()
    assert row["value_date"] == "2026-03-03"
    assert row["value_text"] is None, "a date must not also sit in the text column"
    assert store.get(entry.entry_id).values["dated.when"] == _dt.date(2026, 3, 3)


def test_a_number_answer_is_stored_as_a_number(store: LocalStore) -> None:
    """So "louder than 30" is an arithmetic comparison and not a text one."""
    store.upsert_template(
        LogTemplate(
            template_id="measured",
            name="Measured",
            fields=[
                TemplateField(
                    field_id="measured.db",
                    field_key="db",
                    label="Decibels",
                    field_type=FieldType.NUMBER,
                )
            ],
        )
    )
    for value in (99, 100):
        store.save(
            EntryRow(
                file_name=f"{value}.mp4",
                investigator_name="Tester",
                template_id="measured",
                values={"measured.db": value},
            )
        )

    rows = store.conn.execute(
        "SELECT value_number FROM entry_values WHERE field_id = 'measured.db' "
        "ORDER BY value_number DESC"
    ).fetchall()
    assert [row["value_number"] for row in rows] == [100.0, 99.0]


# --------------------------------------------------------------------------- #
# Options as rows
# --------------------------------------------------------------------------- #


def test_an_option_carries_its_own_colour(store: LocalStore) -> None:
    """The reason options are rows and not a JSON list of strings. It is also what
    retires the hardcoded seven statuses in the PHP web log."""
    template = LogTemplate(
        template_id="coloured",
        name="Coloured",
        fields=[
            TemplateField(
                field_id="coloured.grade",
                field_key="grade",
                label="Grade",
                field_type=FieldType.CHOICE,
                role=FieldRole.STATUS,
                options=[
                    FieldOption(option_id="g0", value="Low", colour="#112233", position=0),
                    FieldOption(
                        option_id="g1", value="High", colour="#445566", position=1, is_default=True
                    ),
                ],
            )
        ],
    )
    store.upsert_template(template)

    stored = store.get_template("coloured")
    assert stored.colour_for_status("Low") == "#112233"
    assert stored.default_values()["coloured.grade"] == "High"


def test_an_option_label_defaults_to_its_value() -> None:
    assert FieldOption(value="Needs Review").label == "Needs Review"


def test_a_rule_pointing_at_a_missing_field_does_not_crash() -> None:
    """A template can be edited into an inconsistent state between saves."""
    template = LogTemplate(
        template_id="broken",
        name="Broken",
        fields=[TemplateField(field_id="broken.a", field_key="a", label="A")],
        rules=[
            TemplateRule(
                rule_id="r", when_field_id="gone", when_value="x", requires_field_id="broken.a"
            )
        ],
    )
    assert template.validation_problems({}) == []
    assert template.validation_problems({"gone": "x"}) == ["'A' is required."]


# --------------------------------------------------------------------------- #
# A field must not answer itself
#
# Both of these recorded a value nobody entered, which in an evidence record is
# worse than refusing to save: validation cannot object to a value that is there,
# so the entry goes in looking answered.
# --------------------------------------------------------------------------- #


def test_a_required_choice_with_no_default_starts_blank(qt_app) -> None:
    """It used to open on its first option, so an entry saved without touching the
    field recorded a choice the reviewer never made."""
    from evidence_review.ui.field_widgets import FieldEditor

    field = TemplateField(
        field_id="f",
        field_key="grade",
        label="Grade",
        field_type=FieldType.CHOICE,
        is_required=True,
        options=[
            FieldOption(option_id="a", value="Pass", position=0),
            FieldOption(option_id="b", value="Fail", position=1),
        ],
    )
    editor = FieldEditor(field)
    try:
        assert editor.value() is None, "nothing chosen yet"
        assert field.problems(editor.value()) == ["'Grade' is required."]
    finally:
        editor.deleteLater()


def test_a_choice_with_a_default_starts_on_it(qt_app) -> None:
    """The blank row is only for a field the template gives no default."""
    from evidence_review.ui.field_widgets import FieldEditor

    editor = FieldEditor(PARANORMAL_TEMPLATE.status_field)
    try:
        assert editor.value() == "Needs Review"
    finally:
        editor.deleteLater()


def test_an_untouched_date_records_nothing(qt_app) -> None:
    """A date edit cannot be blank, so it used to store its own floor -- a date
    nobody entered, sitting in the record as though somebody had."""
    from evidence_review.ui.field_widgets import FieldEditor

    field = TemplateField(field_id="f", field_key="when", label="When", field_type=FieldType.DATE)
    editor = FieldEditor(field)
    try:
        assert editor.value() is None

        editor.set_value(_dt.date(2026, 3, 3))
        assert editor.value() == _dt.date(2026, 3, 3)

        # And clearing it must leave it unanswered rather than defaulting to today.
        editor.set_value(None)
        assert editor.value() is None
    finally:
        editor.deleteLater()


# --------------------------------------------------------------------------- #
# A template that cannot be stored says why
# --------------------------------------------------------------------------- #


def test_two_fields_cannot_share_a_name(store: LocalStore) -> None:
    """The schema forbids it, but a raw IntegrityError tells the caller nothing --
    and a template arriving over sync would take the worker down with it."""
    from evidence_review.store import TemplateError

    broken = LogTemplate(
        template_id="broken",
        name="Broken",
        fields=[
            TemplateField(field_id="a", field_key="status", label="Status"),
            TemplateField(field_id="b", field_key="status", label="Status Again"),
        ],
    )
    with pytest.raises(TemplateError, match="status"):
        store.upsert_template(broken)


def test_two_fields_cannot_share_an_id(store: LocalStore) -> None:
    """An id is what every stored answer points at. Two fields claiming one would
    make those answers ambiguous."""
    from evidence_review.store import TemplateError

    broken = LogTemplate(
        template_id="broken",
        name="Broken",
        fields=[
            TemplateField(field_id="same", field_key="one", label="One"),
            TemplateField(field_id="same", field_key="two", label="Two"),
        ],
    )
    with pytest.raises(TemplateError, match="same"):
        store.upsert_template(broken)


def test_a_choice_cannot_list_the_same_value_twice(store: LocalStore) -> None:
    broken = LogTemplate(
        template_id="broken",
        name="Broken",
        fields=[
            TemplateField(
                field_id="f",
                field_key="grade",
                label="Grade",
                field_type=FieldType.CHOICE,
                options=[
                    FieldOption(option_id="a", value="Pass", position=0),
                    FieldOption(option_id="b", value="Pass", position=1),
                ],
            )
        ],
    )
    from evidence_review.store import TemplateError

    with pytest.raises(TemplateError, match="Pass"):
        store.upsert_template(broken)


def test_a_valid_template_is_not_refused(store: LocalStore) -> None:
    """The guard must not be so eager that an ordinary template trips it."""
    store.upsert_template(CRIMINAL_TEMPLATE.model_copy(deep=True))
    assert store.get_template(CRIMINAL_TEMPLATE.template_id) is not None


# --------------------------------------------------------------------------- #
# The template caches, which two threads share
#
# The store is built to be used from two: the UI thread writes and the sync worker
# reads on its own connection. Both caches were added with a check-then-use that the
# other thread can invalidate in between -- KeyError out of get_template, and an
# AttributeError out of the field index, both in a background thread where they are
# near-impossible to trace back.
#
# A stress run does not reproduce either: the gap is a few bytecodes wide. These force
# the invalidation into the gap instead of waiting to be unlucky.
# --------------------------------------------------------------------------- #


class _ClearedOnLookup(dict):
    """A cache that empties itself the instant it is asked whether it holds something.

    Stands in for the other thread invalidating in the gap between the lookup and the
    read. A real one is a few bytecodes wide and will not reproduce on demand, so it is
    forced rather than waited for.
    """

    def __contains__(self, key: object) -> bool:
        present = super().__contains__(key)
        self.clear()
        return present

    def get(self, key, default=None):
        value = super().get(key, default)
        self.clear()
        return value


def test_get_template_survives_the_cache_vanishing_mid_lookup(store: LocalStore) -> None:
    """Deciding the cache holds a template and then reading it are two steps. An edit
    landing between them used to raise KeyError, in whichever thread was reading."""
    template = store.get_template(DEFAULT_TEMPLATE_ID)
    assert template is not None

    store._template_cache = _ClearedOnLookup(store._template_cache)
    # Must return the template it found, not index a dict that is now empty.
    again = store.get_template(DEFAULT_TEMPLATE_ID)

    assert again is not None
    assert again.template_id == DEFAULT_TEMPLATE_ID


class _ClearingLock:
    """A lock that drops the field cache as it is released.

    Forces the other half of the window: the index has just been stored and is about to
    be returned. Code that returns the attribute rather than what it built hands back
    None, and every caller immediately does .get() on it.
    """

    def __init__(self, store: LocalStore) -> None:
        self._store = store
        self._inner = threading.RLock()

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, *exc):
        self._store._field_cache = None
        return self._inner.__exit__(*exc)


def test_the_field_index_is_never_handed_back_as_none(store: LocalStore) -> None:
    """Built, cached, returned. Returning the attribute instead of what was built
    means a concurrent invalidation turns the result into None -- and _values_for and
    save() both call .get() on it without looking."""
    store._invalidate_template_cache()
    store._cache_lock = _ClearingLock(store)

    index = store._field_index()

    assert isinstance(index, dict), f"got {index!r}"
    assert PARANORMAL_TEMPLATE.field_by_key("area").field_id in index


def test_two_threads_hammering_the_cache_do_not_break_it(store: LocalStore) -> None:
    """The ordinary case, as a backstop: a worker reading while the UI saves edits."""
    import threading
    import traceback

    for index in range(20):
        store.save(make_entry(values={OBSERVATION: f"entry {index}"}))

    failures: list[str] = []

    def reader() -> None:
        try:
            for _ in range(200):
                store.list_pending(limit=20)
                assert store.get_template(DEFAULT_TEMPLATE_ID) is not None
                assert store._field_index() is not None
        except Exception:  # noqa: BLE001 - whatever it is, the test wants to see it
            failures.append(traceback.format_exc())

    def writer() -> None:
        try:
            template = store.get_template(DEFAULT_TEMPLATE_ID)
            for version in range(200):
                template.version = version + 2
                store.upsert_template(template)
        except Exception:  # noqa: BLE001
            failures.append(traceback.format_exc())

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, "\n".join(failures)
