"""The eye column, and the dialog it opens.

The session log shows a handful of columns so it stays scannable, which on a nine-field
template means most of the record is off screen. These guard the way back to the rest of
it: that the eye is there and reachable in one click, that the dialog shows *everything*
the entry holds, and that editing from it writes through the same path as everywhere
else.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from evidence_review.builtin_templates import CRIMINAL_TEMPLATE, PARANORMAL_TEMPLATE
from evidence_review.config import Settings
from evidence_review.models import Case, EntryRow
from evidence_review.store import LocalStore
from evidence_review.ui import entry_detail as detail_module
from evidence_review.ui.entry_detail import EntryDetailDialog, _Answer
from evidence_review.ui.icons import eye_icon
from evidence_review.ui.log_dock import LogDock, columns_for
from evidence_review.ui.theme import TEXT_MUTED

from .conftest import AREA, DEBUNK, OBSERVATION, STATUS, make_entry


def answer_labels(dialog: EntryDetailDialog) -> list[str]:
    """The caption of every answer block on screen, in order."""
    return [block.layout().itemAt(0).widget().text() for block in dialog.findChildren(_Answer)]


def answer_values(dialog: EntryDetailDialog) -> list[str]:
    return [block.layout().itemAt(1).widget().text() for block in dialog.findChildren(_Answer)]


@pytest.fixture
def detail(qt_app, store: LocalStore):
    """Open the detail dialog on an entry, and clean up after."""
    settings = Settings()
    settings.api.enabled = False
    made: list[EntryDetailDialog] = []
    saved: list[EntryRow] = []

    def open_on(entry: EntryRow, commit=None):
        dialog = EntryDetailDialog(
            entry,
            store=store,
            settings=settings,
            commit=commit or saved.append,
        )
        made.append(dialog)
        return dialog

    open_on.saved = saved
    open_on.settings = settings
    yield open_on
    for dialog in made:
        dialog.deleteLater()


# --------------------------------------------------------------------------- #
# The icon
# --------------------------------------------------------------------------- #


def test_the_eye_is_painted_not_a_font_glyph(qt_app) -> None:
    """A glyph lands on whatever emoji font the machine has, which on Windows is a
    colour bitmap at the wrong weight. Painting it means one appearance everywhere."""
    icon = eye_icon(TEXT_MUTED, 16)
    assert not icon.isNull()

    image = icon.pixmap(16, 16).toImage()
    inked = sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 0
    )
    assert inked > 20, "the icon is blank"
    assert inked < image.width() * image.height() * 0.8, "the icon is a solid block"


def test_the_same_icon_is_reused(qt_app) -> None:
    """A table model asks for its decoration once per visible row on every repaint."""
    assert eye_icon(TEXT_MUTED, 16) is eye_icon(TEXT_MUTED, 16)
    assert eye_icon(TEXT_MUTED, 16) is not eye_icon(TEXT_MUTED, 24)


# --------------------------------------------------------------------------- #
# The column
# --------------------------------------------------------------------------- #


def test_the_eye_column_leads_whatever_the_template(qt_app) -> None:
    for template in (PARANORMAL_TEMPLATE, CRIMINAL_TEMPLATE, None):
        columns = columns_for(template)
        assert columns[0].kind == "view"
        assert columns[0].title == "", "it is an icon, so a heading would be noise"
        assert columns[1].kind == "sync"


def test_the_cell_holds_the_icon_and_no_text(qt_app, store: LocalStore) -> None:
    dock = LogDock()
    try:
        dock.set_template(
            PARANORMAL_TEMPLATE, {PARANORMAL_TEMPLATE.template_id: PARANORMAL_TEMPLATE}
        )
        dock.set_entries([make_entry()])
        index = dock.model.index(0, 0)

        assert not dock.model.data(index, Qt.ItemDataRole.DecorationRole).isNull()
        assert dock.model.data(index, Qt.ItemDataRole.DisplayRole) == ""
        assert "everything recorded" in dock.model.data(index, Qt.ItemDataRole.ToolTipRole)
    finally:
        dock.deleteLater()


def test_one_click_on_the_eye_asks_for_the_entry(qt_app) -> None:
    """One click, because it is drawn as a button. Asking for two on something that
    looks like a button is how a feature goes unfound."""
    dock = LogDock()
    try:
        entry = make_entry()
        dock.set_template(
            PARANORMAL_TEMPLATE, {PARANORMAL_TEMPLATE.template_id: PARANORMAL_TEMPLATE}
        )
        dock.set_entries([entry])

        seen: list[EntryRow] = []
        dock.view_requested.connect(seen.append)

        dock._on_clicked(dock.model.index(0, 0))
        assert [found.entry_id for found in seen] == [entry.entry_id]

        # Every other column ignores a single click.
        for column in range(1, dock.model.columnCount()):
            dock._on_clicked(dock.model.index(0, column))
        assert len(seen) == 1
    finally:
        dock.deleteLater()


def test_double_clicking_the_eye_does_not_also_jump_the_player(qt_app) -> None:
    """The first of the two clicks has already opened the entry. Jumping as well would
    be two different things from one gesture."""
    dock = LogDock()
    try:
        dock.set_template(
            PARANORMAL_TEMPLATE, {PARANORMAL_TEMPLATE.template_id: PARANORMAL_TEMPLATE}
        )
        dock.set_entries([make_entry()])

        jumped: list[EntryRow] = []
        dock.entry_activated.connect(jumped.append)

        dock._on_double_clicked(dock.model.index(0, 0))
        assert jumped == []

        # A normal column still does.
        dock._on_double_clicked(dock.model.index(0, 2))
        assert len(jumped) == 1
    finally:
        dock.deleteLater()


# --------------------------------------------------------------------------- #
# The dialog
# --------------------------------------------------------------------------- #


def test_it_shows_every_answer_the_entry_holds(detail, store: LocalStore) -> None:
    """The reason the dialog exists: nine of these never fit in the table."""
    store.upsert_case(Case(case_id="c", name="C", template_id=CRIMINAL_TEMPLATE.template_id))
    form = CRIMINAL_TEMPLATE
    entry = EntryRow(
        file_name="cam4.mp4",
        case_id="c",
        investigator_name="Jane",
        template_id=form.template_id,
        values={
            form.field_by_key("location").field_id: "Cam 4",
            form.field_by_key("observation").field_id: "Male enters",
            form.field_by_key("status").field_id: "Subject Identified",
            form.field_by_key("persons").field_id: "Male, 30s",
            form.field_by_key("vehicles").field_id: "AB12 CDE",
            form.field_by_key("actual_time").field_id: "23:14",
            form.field_by_key("exhibit_reference").field_id: "JD/4",
            form.field_by_key("significance").field_id: "Key",
            form.field_by_key("follow_up").field_id: "Trace the keeper.",
        },
    )
    store.save(entry)

    labels = answer_labels(detail(store.get(entry.entry_id)))
    assert labels == [
        "Location / Camera",
        "Observation",
        "Status",
        "Persons",
        "Vehicles",
        "Actual Time",
        "Exhibit Reference",
        "Evidential Significance",
        "Follow-up Action",
    ], "in the order the form asks them"


def test_an_unanswered_field_is_left_out(detail, store: LocalStore) -> None:
    """A screen of empty labels would bury the answers that are there."""
    entry = make_entry()
    store.save(entry)

    labels = answer_labels(detail(store.get(entry.entry_id)))
    assert "Debunk Reasoning" not in labels
    assert "What Was Heard or Seen" in labels


def test_an_answer_for_a_field_this_client_has_never_seen_is_shown(
    detail, store: LocalStore
) -> None:
    """It is a colleague's answer on a template version not pulled yet. Showing it
    under its raw id beats a reader never learning it is there."""
    entry = make_entry(values={"some-newer-template.field": "written by a colleague"})
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    assert any("some-newer-template.field" in label for label in answer_labels(dialog))
    assert "written by a colleague" in answer_values(dialog)


def test_the_status_answer_is_coloured_like_the_log(detail, store: LocalStore) -> None:
    entry = make_entry(values={STATUS: "Debunked"})
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    index = answer_labels(dialog).index("Status")
    style = dialog.findChildren(_Answer)[index].layout().itemAt(1).widget().styleSheet()
    assert PARANORMAL_TEMPLATE.colour_for_status("Debunked") in style


def test_the_heading_is_the_summary_and_the_subheading_locates_it(
    detail, store: LocalStore
) -> None:
    entry = make_entry(
        values={OBSERVATION: "Two distinct knocks."},
        event_offset_seconds=83.0,
        event_duration_seconds=4.0,
    )
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    assert dialog.heading.text() == "Two distinct knocks."
    assert "clip.mp4" in dialog.subheading.text()
    assert "01:23" in dialog.subheading.text()
    assert "for 00:00:04" in dialog.subheading.text()


def test_a_long_value_wraps_rather_than_being_cut_off(detail, store: LocalStore) -> None:
    """A SHA-256 wants about 700 pixels in a 130-pixel column. Without wrapping it was
    simply chopped, which is the one thing a provenance panel cannot do -- checking a
    hash means reading all of it."""
    entry = make_entry(media_sha256="b" * 64, media_path="C:" + "\\long" * 20 + "\\clip.mp4")
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    dialog.resize(900, 620)
    dialog.show()
    try:
        clipped = [
            label.text()
            for label in dialog.findChildren(QLabel)
            if label.text()
            and not label.wordWrap()
            and label.fontMetrics().horizontalAdvance(label.text()) > label.width()
        ]
        assert clipped == [], f"these are cut off with no way to read them: {clipped}"
    finally:
        dialog.hide()


def test_close_is_not_wedged_between_the_two_actions(detail, store: LocalStore) -> None:
    """Qt orders a button box by role, which put Close in the middle: the thing that
    dismisses the dialog sat between the things that do something."""
    entry = make_entry()
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    dialog.resize(900, 620)
    dialog.show()
    try:
        ordered = [
            button.text()
            for button in sorted(
                dialog.findChildren(QPushButton),
                key=lambda widget: widget.mapTo(dialog, widget.rect().topLeft()).x(),
            )
            if button.isVisible()
        ]
        assert ordered[-1] == "Close"
    finally:
        dialog.hide()


# --------------------------------------------------------------------------- #
# Editing from it
# --------------------------------------------------------------------------- #


class _FakeEditor:
    """Stands in for the LOG modal. A real one would block forever in a headless test."""

    DialogCode = type("DialogCode", (), {"Accepted": 1})
    opened_with: EntryRow | None = None

    def __init__(self, **kwargs):
        type(self).opened_with = kwargs["existing"]
        self._existing = kwargs["existing"]

    def exec(self):
        return 1

    @property
    def entry(self):
        updated = self._existing.model_copy(deep=True)
        updated.values[OBSERVATION] = "Revised by the editor."
        return updated


def test_editing_goes_through_the_callers_save_path(detail, store: LocalStore, monkeypatch) -> None:
    """Not a direct store write. An edit made here has to take the same steps as one
    made anywhere else -- written locally, log refreshed, sync nudged."""
    monkeypatch.setattr(detail_module, "LogEntryDialog", _FakeEditor)
    entry = make_entry(values={OBSERVATION: "Before."})
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    dialog._edit()

    assert [saved.values[OBSERVATION] for saved in detail.saved] == ["Revised by the editor."]
    assert dialog.was_edited


def test_the_editor_opens_on_the_entry_being_viewed(detail, store: LocalStore, monkeypatch) -> None:
    monkeypatch.setattr(detail_module, "LogEntryDialog", _FakeEditor)
    entry = make_entry(values={AREA: "Attic"})
    store.save(entry)

    detail(store.get(entry.entry_id))._edit()
    assert _FakeEditor.opened_with.entry_id == entry.entry_id


def test_the_dialog_reloads_to_show_the_change(detail, store: LocalStore, monkeypatch) -> None:
    """Otherwise the reviewer is dropped back to the list to go and find what they
    just wrote."""
    monkeypatch.setattr(detail_module, "LogEntryDialog", _FakeEditor)
    entry = make_entry(values={OBSERVATION: "Before."})
    store.save(entry)

    dialog = detail(store.get(entry.entry_id), commit=store.save)
    assert "Before." in answer_values(dialog)

    dialog._edit()
    assert "Revised by the editor." in answer_values(dialog)
    assert "Before." not in answer_values(dialog)


def test_what_it_reloads_is_what_the_store_holds(detail, store: LocalStore, monkeypatch) -> None:
    """Re-read rather than trusting the editor's copy: the store coerces values and
    bumps the revision, so what it holds is the record and the copy is not."""
    monkeypatch.setattr(detail_module, "LogEntryDialog", _FakeEditor)
    entry = make_entry()
    store.save(entry)

    dialog = detail(store.get(entry.entry_id), commit=store.save)
    before = dialog.entry.local_revision
    dialog._edit()

    assert dialog.entry.local_revision > before


def test_other_answers_survive_an_edit(detail, store: LocalStore, monkeypatch) -> None:
    monkeypatch.setattr(detail_module, "LogEntryDialog", _FakeEditor)
    entry = make_entry(values={AREA: "Attic", DEBUNK: "A pipe."})
    store.save(entry)

    dialog = detail(store.get(entry.entry_id), commit=store.save)
    dialog._edit()

    reloaded = store.get(entry.entry_id)
    assert reloaded.values[AREA] == "Attic"
    assert reloaded.values[DEBUNK] == "A pipe."


# --------------------------------------------------------------------------- #
# A deleted entry
# --------------------------------------------------------------------------- #


def test_a_deleted_entry_can_still_be_read(detail, store: LocalStore) -> None:
    """Nothing is ever erased, so the record is still there to be looked at."""
    entry = make_entry()
    store.save(entry)
    store.soft_delete(entry.entry_id)

    dialog = detail(store.get(entry.entry_id))
    assert "deleted" in dialog.subheading.text()
    assert answer_labels(dialog), "the answers are still shown"


def test_a_deleted_entry_cannot_be_edited_from_here(detail, store: LocalStore) -> None:
    entry = make_entry()
    store.save(entry)
    store.soft_delete(entry.entry_id)

    assert not detail(store.get(entry.entry_id)).edit_button.isEnabled()


def test_going_to_the_moment_is_offered_only_when_there_is_one(detail, store: LocalStore) -> None:
    """A still image has no position to jump to."""
    positioned = make_entry(event_offset_seconds=12.0)
    store.save(positioned)
    assert detail(store.get(positioned.entry_id)).goto_button.isEnabled()

    still = make_entry(event_offset_seconds=None)
    store.save(still)
    assert not detail(store.get(still.entry_id)).goto_button.isEnabled()
