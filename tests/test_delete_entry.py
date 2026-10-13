"""Deleting a log entry, and getting it back.

Soft deletion always worked at the database level. What these guard is the part that
did not: that a reviewer can find the delete, can tell afterwards that the record was
kept rather than erased, and can put it back.

The promise the confirmation makes -- "never erased, kept, recorded on the server" --
is only true if all three of those hold. Before this, the row simply vanished and
nothing in the application could reach it again.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from evidence_review.builtin_templates import PARANORMAL_TEMPLATE
from evidence_review.config import Settings
from evidence_review.models import SyncState
from evidence_review.store import LocalStore
from evidence_review.ui.entry_detail import EntryDetailDialog
from evidence_review.ui.log_dock import LogDock

from .conftest import OBSERVATION, make_entry

# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


def test_a_deleted_entry_is_kept_not_erased(store: LocalStore) -> None:
    entry = make_entry(values={OBSERVATION: "Two knocks."})
    store.save(entry)
    store.soft_delete(entry.entry_id)

    kept = store.get(entry.entry_id)
    assert kept is not None, "the row must still be there"
    assert kept.is_deleted
    assert kept.values[OBSERVATION] == "Two knocks.", "and so must every answer"


def test_restore_puts_it_back(store: LocalStore) -> None:
    """The counterpart soft_delete always implied. Without it the confirmation was
    true of the database and false of the application."""
    entry = make_entry()
    store.save(entry)
    store.soft_delete(entry.entry_id)
    store.restore(entry.entry_id)

    assert not store.get(entry.entry_id).is_deleted
    assert [found.entry_id for found in store.list_entries()] == [entry.entry_id]


def test_restoring_re_queues_for_the_server(store: LocalStore) -> None:
    """A restore is a content change. The team's other clients are still hiding the
    entry until they are told, so it has to go back up."""
    entry = make_entry()
    store.save(entry)
    store.mark_synced(entry.entry_id, 7, store.get(entry.entry_id).local_revision)
    store.soft_delete(entry.entry_id)
    store.mark_synced(entry.entry_id, 7, store.get(entry.entry_id).local_revision)

    store.restore(entry.entry_id)
    assert store.get(entry.entry_id).sync_state is SyncState.PENDING


def test_restoring_bumps_the_revision(store: LocalStore) -> None:
    """Same reason the deletion does: an upload already in flight must not be able to
    mark the deleted version as synced and strand the restore here."""
    entry = make_entry()
    store.save(entry)
    store.soft_delete(entry.entry_id)
    before = store.get(entry.entry_id).local_revision

    store.restore(entry.entry_id)
    after = store.get(entry.entry_id)
    assert after.local_revision > before
    assert store.mark_synced(entry.entry_id, 7, before) is False


def test_deleted_entries_are_hidden_unless_asked_for(store: LocalStore) -> None:
    live = make_entry()
    gone = make_entry()
    store.save(live)
    store.save(gone)
    store.soft_delete(gone.entry_id)

    assert [found.entry_id for found in store.list_entries()] == [live.entry_id]
    assert {found.entry_id for found in store.list_entries(include_deleted=True)} == {
        live.entry_id,
        gone.entry_id,
    }


def test_the_deleted_count_is_per_case(store: LocalStore) -> None:
    """It drives the "n deleted, hidden" note, which would be wrong on every other
    case if it counted the whole database."""
    here = make_entry(case_id="willow-house")
    elsewhere = make_entry(case_id="harbour-street")
    store.save(here)
    store.save(elsewhere)
    store.soft_delete(here.entry_id)
    store.soft_delete(elsewhere.entry_id)

    assert store.deleted_count("willow-house") == 1
    assert store.deleted_count() == 2


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #


@pytest.fixture
def dock(qt_app):
    widget = LogDock()
    widget.set_template(PARANORMAL_TEMPLATE, {PARANORMAL_TEMPLATE.template_id: PARANORMAL_TEMPLATE})
    yield widget
    widget.deleteLater()


def test_a_deleted_row_looks_deleted(dock) -> None:
    """Showing them is only useful if one cannot be misread as live."""
    gone = make_entry()
    gone.is_deleted = True
    dock.set_entries([gone], deleted=1)

    index = dock.model.index(0, dock.model.columnCount() - 1)
    font = dock.model.data(index, Qt.ItemDataRole.FontRole)
    assert font is not None and font.strikeOut()
    assert "Deleted" in dock.model.data(index, Qt.ItemDataRole.ToolTipRole)


def test_the_count_says_when_something_is_hidden(dock) -> None:
    """Otherwise a deletion looks exactly like an erasure: the row is gone and nothing
    says where."""
    dock.set_entries([make_entry()], deleted=2)
    assert "2 deleted, hidden" in dock.count_label.text()


def test_the_count_says_how_many_are_deleted_when_they_are_shown(dock) -> None:
    dock.show_deleted_check.setChecked(True)
    gone = make_entry()
    gone.is_deleted = True
    dock.set_entries([make_entry(), gone], deleted=1)

    text = dock.count_label.text()
    assert "2 entries" in text
    assert "1 deleted" in text
    assert "hidden" not in text


def test_an_empty_case_whose_entries_were_all_deleted_says_so(dock) -> None:
    """ "No entries yet - press LOG" would be a lie, and would send the reviewer off to
    re-record something they already have."""
    dock.set_entries([], deleted=3)
    text = dock.count_label.text()
    assert "3 deleted" in text
    assert "Show deleted" in text


def test_toggling_the_checkbox_announces_it(dock) -> None:
    seen: list[bool] = []
    dock.show_deleted_changed.connect(seen.append)

    dock.show_deleted_check.setChecked(True)
    dock.show_deleted_check.setChecked(False)
    assert seen == [True, False]
    assert dock.show_deleted is False


def test_the_menu_offers_restore_for_a_deleted_entry_and_delete_otherwise(dock) -> None:
    """Both at once would be nonsense: an entry is either in the log or it is not."""
    live = make_entry()
    gone = make_entry()
    gone.is_deleted = True

    for_live = [action.text() for action in dock.context_menu_for(live).actions()]
    for_gone = [action.text() for action in dock.context_menu_for(gone).actions()]

    assert "Delete entry" in for_live and "Restore entry" not in for_live
    assert "Restore entry" in for_gone and "Delete entry" not in for_gone


def test_choosing_restore_in_the_menu_asks_the_window(dock) -> None:
    """Triggered through the action, not by emitting the signal by hand -- otherwise
    this would be testing Qt rather than that the action is wired to anything."""
    gone = make_entry()
    gone.is_deleted = True

    asked: list = []
    dock.restore_requested.connect(asked.append)

    restore = next(
        action
        for action in dock.context_menu_for(gone).actions()
        if action.text() == "Restore entry"
    )
    restore.trigger()
    assert [found.entry_id for found in asked] == [gone.entry_id]


def test_choosing_delete_in_the_menu_asks_the_window(dock) -> None:
    live = make_entry()
    asked: list = []
    dock.delete_requested.connect(asked.append)

    delete = next(
        action
        for action in dock.context_menu_for(live).actions()
        if action.text() == "Delete entry"
    )
    delete.trigger()
    assert [found.entry_id for found in asked] == [live.entry_id]


# --------------------------------------------------------------------------- #
# The detail dialog
# --------------------------------------------------------------------------- #


@pytest.fixture
def detail(qt_app, store: LocalStore):
    settings = Settings()
    settings.api.enabled = False
    made: list[EntryDetailDialog] = []

    def open_on(entry):
        dialog = EntryDetailDialog(entry, store=store, settings=settings, commit=store.save)
        made.append(dialog)
        return dialog

    yield open_on
    for dialog in made:
        dialog.deleteLater()


def test_delete_is_offered_where_the_entry_is_being_read(detail, store: LocalStore) -> None:
    """Which is the point: it was in the Review menu and a right-click, and people did
    not find it."""
    entry = make_entry()
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    assert dialog.delete_button.isVisibleTo(dialog)
    assert not dialog.restore_button.isVisibleTo(dialog)


def test_a_deleted_entry_offers_restore_instead(detail, store: LocalStore) -> None:
    entry = make_entry()
    store.save(entry)
    store.soft_delete(entry.entry_id)

    dialog = detail(store.get(entry.entry_id))
    assert dialog.restore_button.isVisibleTo(dialog)
    assert not dialog.delete_button.isVisibleTo(dialog)
    assert not dialog.edit_button.isEnabled(), "restore it before editing it"


def test_deleting_from_the_dialog_asks_rather_than_does(detail, store: LocalStore) -> None:
    """The window owns the confirmation, the refresh and the sync nudge. A dialog that
    wrote straight to the store would skip all three."""
    entry = make_entry()
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    asked: list = []
    dialog.delete_requested.connect(asked.append)
    dialog._delete()

    assert [found.entry_id for found in asked] == [entry.entry_id]
    assert not store.get(entry.entry_id).is_deleted, "nothing was written behind the window"


def test_a_cancelled_delete_leaves_the_dialog_open(detail, store: LocalStore) -> None:
    """Closing regardless would make a cancelled confirmation look like it worked."""
    entry = make_entry()
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    dialog.show()
    try:
        # Nothing is connected, so nothing deletes -- which is what a cancelled
        # confirmation amounts to from the dialog's side.
        dialog._delete()
        assert dialog.isVisible()
        assert not dialog.was_edited
    finally:
        dialog.hide()


def test_the_dialog_closes_once_the_entry_really_is_deleted(detail, store: LocalStore) -> None:
    entry = make_entry()
    store.save(entry)

    dialog = detail(store.get(entry.entry_id))
    dialog.delete_requested.connect(lambda found: store.soft_delete(found.entry_id))
    dialog.show()
    dialog._delete()

    assert not dialog.isVisible()
    assert dialog.was_edited, "the caller has to know the log changed"


def test_restoring_from_the_dialog_keeps_it_open(detail, store: LocalStore) -> None:
    """There is a record to carry on reading, and Delete to offer again."""
    entry = make_entry()
    store.save(entry)
    store.soft_delete(entry.entry_id)

    dialog = detail(store.get(entry.entry_id))
    dialog.restore_requested.connect(lambda found: store.restore(found.entry_id))
    dialog.show()
    try:
        dialog._restore()
        assert dialog.isVisible()
        assert dialog.delete_button.isVisibleTo(dialog)
        assert not dialog.restore_button.isVisibleTo(dialog)
        assert dialog.edit_button.isEnabled()
    finally:
        dialog.hide()


def test_a_deleted_entry_can_still_be_read_in_full(detail, store: LocalStore) -> None:
    """Nothing is erased, so there is still a record to look at -- which is the whole
    claim the deletion makes."""
    entry = make_entry(values={OBSERVATION: "Two distinct knocks."})
    store.save(entry)
    store.soft_delete(entry.entry_id)

    dialog = detail(store.get(entry.entry_id))
    assert dialog.heading.text() == "Two distinct knocks."
    assert "deleted" in dialog.subheading.text()
