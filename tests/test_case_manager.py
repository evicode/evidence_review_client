"""The Cases and log window, driven the way a reviewer drives it.

Built 2026-10-07 because the desktop app could not: see a whole case's log at
once, sort it, filter it beyond one search box, change more than one entry at a
time, move an entry to another case, archive or delete a case, or reach the form
editor from anywhere but the File menu.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from evidence_review.models import Case, SyncState
from evidence_review.ui.case_manager import ALL_CASES, CasesAndLogWindow, Host

from .conftest import OBSERVATION, STATUS, make_entry


@pytest.fixture
def setup(qt_app, store):
    store.upsert_case(Case(case_id="willow", name="Willow House"))
    store.upsert_case(Case(case_id="mill", name="Old Mill"))
    store.upsert_case(Case(case_id="done", name="Closed Case"))
    entries = [
        make_entry(case_id="willow", values={OBSERVATION: "Knock", STATUS: "Needs Review"},
                   event_offset_seconds=30.0, investigator_name="Dana"),
        make_entry(case_id="willow", values={OBSERVATION: "Cold spot", STATUS: "Confirmed"},
                   event_offset_seconds=10.0, investigator_name="Sam"),
        make_entry(case_id="willow", values={OBSERVATION: "Door", STATUS: "Debunked"},
                   event_offset_seconds=20.0, investigator_name="Dana"),
        make_entry(case_id="mill", values={OBSERVATION: "Voice"}, event_offset_seconds=5.0),
    ]
    for entry in entries:
        entry.sync_state = SyncState.SYNCED
        store.save(entry)
    state = {"current": "willow", "changed": 0, "switched": None, "forms": 0}
    host = Host(
        current_case=lambda: state["current"],
        switch_case=lambda cid, name: state.update(current=cid, switched=cid),
        view_entry=lambda e: None,
        edit_entry=lambda e: None,
        goto_entry=lambda e: None,
        edit_templates=lambda: state.update(forms=state["forms"] + 1),
        changed=lambda: state.update(changed=state["changed"] + 1),
    )
    window = CasesAndLogWindow(store, host)
    window.show()
    qt_app.processEvents()
    yield window, store, entries, state
    window.close()
    window.deleteLater()
    qt_app.processEvents()


def case_rows(window) -> list[str]:
    return [window.case_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(window.case_list.count())]


def choose(window, case_id) -> None:
    window.case_list.setCurrentRow(case_rows(window).index(case_id))


def select_rows(window, rows: list[int]) -> None:
    sel = window.table.selectionModel()
    sel.clearSelection()
    for row in rows:
        sel.select(window.proxy.index(row, 0),
                   QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)


def test_every_case_is_listed_with_counts_and_all_cases(setup) -> None:
    window, _store, _entries, _state = setup
    rows = case_rows(window)
    assert rows[0] == ALL_CASES and {"willow", "mill", "done"} <= set(rows)
    labels = [window.case_list.item(i).text() for i in range(window.case_list.count())]
    assert any("Willow House  (3)" in t and t.startswith("●") for t in labels), labels


def test_the_whole_case_is_shown_and_sorts_by_any_column(setup) -> None:
    window, _store, _entries, _state = setup
    choose(window, "willow")
    assert window.proxy.rowCount() == 3
    offset_col = next(i for i, c in enumerate(window.model.columns) if c.kind == "offset")
    window.table.sortByColumn(offset_col, Qt.SortOrder.AscendingOrder)
    times = [window.model.entry_at(window.proxy.mapToSource(window.proxy.index(r, 0)).row()).event_offset_seconds
             for r in range(3)]
    assert times == [10.0, 20.0, 30.0]
    window.table.sortByColumn(offset_col, Qt.SortOrder.DescendingOrder)
    first = window.model.entry_at(window.proxy.mapToSource(window.proxy.index(0, 0)).row())
    assert first.event_offset_seconds == 30.0


def test_filters_by_status_and_investigator(setup) -> None:
    window, _store, _entries, _state = setup
    choose(window, "willow")
    window.status_filter.setCurrentIndex(window.status_filter.findData("Confirmed"))
    assert window.proxy.rowCount() == 1
    window.status_filter.setCurrentIndex(0)
    window.investigator_filter.setCurrentIndex(window.investigator_filter.findData("Dana"))
    assert window.proxy.rowCount() == 2


def test_many_entries_move_to_another_case_and_queue_for_sync(setup) -> None:
    window, store, _entries, state = setup
    choose(window, "willow")
    select_rows(window, [0, 1])
    assert "2 selected" in window.count_label.text()
    moved = [e.entry_id for e in window.selected_entries()]
    window._move_to("mill", "Old Mill")
    for entry_id in moved:
        stored = store.get(entry_id)
        assert stored.case_id == "mill" and stored.sync_state is SyncState.PENDING
    assert state["changed"] == 1
    assert window.proxy.rowCount() == 1, "the moved entries left this case's view"


def test_status_is_set_on_many_at_once(setup) -> None:
    window, store, _entries, _state = setup
    choose(window, "willow")
    window.table.selectAll()
    window._set_status("Confirmed")
    assert {store.get(e.entry_id).values.get(STATUS) for e in store.list_entries(case_id="willow")} == {"Confirmed"}


def test_delete_and_restore_many(setup, monkeypatch) -> None:
    window, store, _entries, _state = setup
    choose(window, "willow")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    select_rows(window, [0, 1])
    window._delete_selected()
    assert len(store.list_entries(case_id="willow")) == 1
    window.show_deleted.setChecked(True)
    window.table.selectAll()
    window._restore_selected()
    assert len(store.list_entries(case_id="willow")) == 3


def test_archive_hides_a_case_and_show_archived_brings_it_back(setup) -> None:
    window, store, _entries, _state = setup
    choose(window, "done")
    window._toggle_archive()
    assert store.get_case("done").is_archived
    assert "done" not in case_rows(window)
    window.show_archived.setChecked(True)
    assert "done" in case_rows(window)


def test_the_case_being_worked_on_cannot_be_archived(setup, monkeypatch) -> None:
    window, store, _entries, _state = setup
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    choose(window, "willow")
    window._toggle_archive()
    assert not store.get_case("willow").is_archived


def test_a_case_is_deleted_only_when_empty_and_named(setup, monkeypatch) -> None:
    window, store, _entries, _state = setup
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, title, text: told.append(title))
    choose(window, "mill")
    window._delete_case()
    assert store.get_case("mill") is not None and told[-1] == "The case is not empty"
    choose(window, "done")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("wrong", True))
    window._delete_case()
    assert store.get_case("done") is not None
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Closed Case", True))
    window._delete_case()
    assert store.get_case("done") is None


def test_a_case_chooses_its_form_and_forms_are_one_click_away(setup) -> None:
    window, store, _entries, state = setup
    choose(window, "mill")
    index = window.form_combo.findData("builtin-criminal")
    assert index >= 0
    window.form_combo.setCurrentIndex(index)
    window._set_form()
    assert store.template_for_case("mill").template_id == "builtin-criminal"
    window._edit_forms()
    assert state["forms"] == 1


def test_work_on_another_case_and_create_one(setup, monkeypatch) -> None:
    window, store, _entries, state = setup
    choose(window, "mill")
    window._make_current()
    assert state["switched"] == "mill"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Harbour Street", True))
    window._new_case()
    assert store.get_case("harbour-street") is not None


def test_the_selected_entry_is_shown_in_full(setup) -> None:
    window, _store, _entries, _state = setup
    choose(window, "willow")
    select_rows(window, [0])
    text = window.detail.toPlainText()
    entry = window.selected_entries()[0]
    assert entry.values[OBSERVATION] in text and "Willow House" in text


def test_the_main_window_opens_it_from_the_case_menu(window) -> None:
    menus = {a.text().replace("&", ""): a.menu() for a in window.menuBar().actions()}
    assert "Case" in menus
    labels = [a.text() for a in menus["Case"].actions()]
    assert "Cases and log…" in labels and "Forms (log templates)…" in labels
    window.open_cases_window()
    assert window._cases_window.isVisible()
    window._cases_window.close()
