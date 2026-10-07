"""Regression tests for bugs found in adversarial review.

Each test names the wrong behaviour it prevents, because the correct behaviour is
not obvious from the code alone.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from evidence_review.builtin_templates import PARANORMAL_TEMPLATE
from evidence_review.export import csv_safe, export_csv, export_xlsx, is_formula_like
from evidence_review.models import SyncState
from evidence_review.store import LocalStore
from evidence_review.util import slugify_case_id

from .conftest import AREA, OBSERVATION, STATUS, make_entry

#: Every export call needs the templates its entries were written on;
#: these tests all use the paranormal one.
TEMPLATES = [PARANORMAL_TEMPLATE]

PLUS_0530 = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Ordering across timezone offsets
# --------------------------------------------------------------------------- #


def test_migration_carries_old_rows_to_the_new_shape(tmp_path) -> None:
    """Event Timestamp changed meaning from a wall-clock time to a position in the
    media. Existing rows must survive that, not be silently emptied."""
    import sqlite3

    from evidence_review.store import MIGRATIONS

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, statements in MIGRATIONS:
        if version > 3:
            break
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute(
        """INSERT INTO log_entries
           (entry_id, case_id, file_name, media_path, media_kind, area,
            file_time_seconds, file_time_end_seconds, duration_seconds,
            observation, status, investigator_name, date_reviewed,
            created_at_utc, updated_at_utc)
           VALUES ('legacy','c','clip.mp4','C:/e/clip.mp4','video','Attic',
                   83.4, 87.1, 3600.0, 'legacy row', 'Unexplained', 'Alice',
                   '2026-09-15','2026-09-15T00:00:00+00:00','2026-09-15T00:00:00+00:00')"""
    )
    conn.commit()
    conn.close()

    store = LocalStore(path)  # migration 4 runs on open
    try:
        row = store.list_entries()[0]
        assert row.values[OBSERVATION] == "legacy row"
        assert row.event_offset_seconds == pytest.approx(83.4)
        assert row.event_duration_seconds == pytest.approx(3.7), "end point became a duration"
        assert row.media_duration_seconds == pytest.approx(3600.0)

        columns = [c[1] for c in store.conn.execute("PRAGMA table_info(log_entries)")]
        for gone in ("event_timestamp", "file_time_seconds", "duration_seconds"):
            assert gone not in columns, f"{gone} should have been dropped"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# The sync race
# --------------------------------------------------------------------------- #


def test_an_edit_made_during_upload_is_not_marked_synced(store: LocalStore) -> None:
    """The worker snapshots pending rows, uploads, then reports success. If the
    reviewer edits one in that window, clearing the flag by id alone would mean
    the newer version is never sent."""
    entry = make_entry(values={OBSERVATION: "v1"})
    store.save(entry)

    uploaded = store.list_pending()[0]  # the worker's snapshot

    edited = store.get(entry.entry_id)
    edited.values[OBSERVATION] = "v2 edited mid-upload"
    edited.updated_at_utc = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=1)
    store.save(edited)  # the reviewer saves

    marked = store.mark_synced(uploaded.entry_id, 7, uploaded.local_revision)

    assert marked is False, "the stale version must not clear the pending flag"
    row = store.get(entry.entry_id)
    assert row.values[OBSERVATION] == "v2 edited mid-upload"
    assert row.sync_state is SyncState.PENDING, "the edit must still be queued"


def test_an_untouched_entry_is_marked_synced(store: LocalStore) -> None:
    """The guard must not block the ordinary case."""
    entry = make_entry()
    store.save(entry)
    snapshot = store.list_pending()[0]

    assert store.mark_synced(snapshot.entry_id, 7, snapshot.local_revision)

    row = store.get(entry.entry_id)
    assert row.sync_state is SyncState.SYNCED
    assert row.server_id == 7


def test_the_revision_advances_on_every_save(store: LocalStore) -> None:
    """A wall-clock guard failed here: on Windows datetime.now() resolves to about
    15 ms, so an edit saved in the same tick kept a byte-identical timestamp."""
    entry = make_entry(values={OBSERVATION: "v1"})
    store.save(entry)
    first = store.list_pending()[0].local_revision

    edited = store.get(entry.entry_id)
    edited.values[OBSERVATION] = "v2"
    edited.updated_at_utc = first_timestamp = store.get(entry.entry_id).updated_at_utc
    store.save(edited)  # same timestamp, deliberately

    second = store.get(entry.entry_id)
    assert second.updated_at_utc == first_timestamp, "timestamps deliberately identical"
    assert second.local_revision > first, "the revision must still advance"
    assert store.mark_synced(entry.entry_id, 1, first) is False
    assert store.get(entry.entry_id).sync_state is SyncState.PENDING


# --------------------------------------------------------------------------- #
# Export formula injection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", chr(9), chr(13)])
def test_formula_triggers_are_recognised(prefix: str) -> None:
    assert is_formula_like(f"{prefix}1+1")
    assert csv_safe(f"{prefix}1+1").startswith("'")


def test_ordinary_text_is_untouched() -> None:
    assert csv_safe("Two knocks from the north wall.") == "Two knocks from the north wall."
    assert not is_formula_like("Two knocks")


def test_csv_neutralises_formulas(tmp_path) -> None:
    entry = make_entry(values={OBSERVATION: "=cmd|'/c calc'!A1"})
    text = export_csv([entry], tmp_path / "log.csv", TEMPLATES).read_text(encoding="utf-8-sig")
    assert "'=cmd|" in text, "a formula-like observation must be prefixed"


def test_xlsx_never_stores_a_formula(tmp_path) -> None:
    """openpyxl infers a formula from a leading '='; exports must force text."""
    openpyxl = pytest.importorskip("openpyxl")

    hostile = ["=1+1", "=cmd|'/c calc'!A1", "+1+1", "-1+1", "@SUM(1:2)"]
    entries = [make_entry(values={OBSERVATION: text}) for text in hostile]
    target = export_xlsx(entries, tmp_path / "log.xlsx", TEMPLATES)

    sheet = openpyxl.load_workbook(target)["Evidence Log"]
    headers = [cell.value for cell in sheet[1]]
    column = headers.index("What Was Heard or Seen") + 1

    for row in range(2, sheet.max_row + 1):
        cell = sheet.cell(row=row, column=column)
        assert cell.data_type != "f", f"{cell.value!r} was stored as a live formula"

    # And the text is preserved exactly, with no added apostrophe.
    stored = [sheet.cell(row=r, column=column).value for r in range(2, sheet.max_row + 1)]
    assert stored == hostile


# --------------------------------------------------------------------------- #
# Case identifiers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Willow House", "willow-house"),
        ("Willow House (2025)", "willow-house-2025"),
        ("Willow House 10/2025", "willow-house-10-2025"),
        ("../../etc/passwd", "etc-passwd"),
        ("Café Noir", "cafe-noir"),
        ("...", "default"),
        ("", "default"),
        # A separator next to a hyphen that was already there. This used to give
        # "willow-house---october", which became a folder name and a filename.
        ("Willow House - October review", "willow-house-october-review"),
        ("  --Willow--  ", "willow"),
        # Dots are legal in an id, so a version-like name keeps its shape.
        ("v1.2 build", "v1.2-build"),
    ],
)
def test_case_ids_are_slugified(name: str, expected: str) -> None:
    assert slugify_case_id(name) == expected


@pytest.mark.parametrize("name", ["../../etc", "a/b", "c:\\\\windows", "..", "with space"])
def test_slugified_ids_are_always_path_safe(name: str) -> None:
    import re

    slug = slugify_case_id(name)
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,120}", slug), slug
    assert ".." not in slug or slug == "default"


def test_long_case_names_are_truncated() -> None:
    assert len(slugify_case_id("x" * 400)) <= 120


# --------------------------------------------------------------------------- #
# Over-long values
# --------------------------------------------------------------------------- #


def test_over_long_values_are_reported_before_they_reach_the_server() -> None:
    """A field the API will refuse must be caught in the modal, otherwise the
    entry sits in the queue forever with no explanation.

    The limit now comes from the field's own definition rather than a table of
    column names, so a field a reviewer adds is length-checked too."""
    entry = make_entry(values={AREA: "A" * 500})
    problems = entry.validation_problems(PARANORMAL_TEMPLATE)
    assert any("Area" in problem and "200" in problem for problem in problems)


def test_normal_values_pass_the_length_check() -> None:
    entry = make_entry(values={AREA: "Basement", STATUS: "Unexplained"})
    assert entry.validation_problems(PARANORMAL_TEMPLATE) == []


# --------------------------------------------------------------------------- #
# Area guessing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:/case/Front Room/clip.mp4", "Front Room"),
        ("C:/case/Main Hall/clip.mp4", "Main Hall"),
        ("C:/case/Night Nursery/clip.mp4", "Night Nursery"),
        ("C:/case/Day Room/clip.mp4", "Day Room"),
    ],
)
def test_room_words_are_not_stripped_as_recorder_noise(path: str, expected: str) -> None:
    """ "front", "main", "day" and "night" were treated as camera noise, so
    "Front Room" was prefilled as "Room" - a wrong value written into evidence."""
    from evidence_review.autofill import guess_area

    assert guess_area(path) == expected


def test_genuine_recorder_tokens_are_still_stripped() -> None:
    from evidence_review.autofill import guess_area

    assert guess_area("C:/case/Attic Stairs/Attic_Stairs_cam1.mp4") == "Attic Stairs"


# --------------------------------------------------------------------------- #
# Case root paths
# --------------------------------------------------------------------------- #


def test_upserting_a_case_without_root_path_keeps_it(store: LocalStore) -> None:
    """MainWindow re-upserts the case on every launch with no root_path, which a
    blind assignment wiped each time."""
    from evidence_review.models import Case

    store.upsert_case(Case(case_id="willow", name="Willow House", root_path="D:/evidence"))
    store.upsert_case(Case(case_id="willow", name="Willow House"))

    stored = {case.case_id: case for case in store.list_cases()}
    assert stored["willow"].root_path == "D:/evidence"


def test_a_delete_during_upload_is_not_marked_synced(store: LocalStore) -> None:
    """A deletion is a content change, so it must bump the revision too.

    Without it the worker could mark the pre-deletion version as synced, the
    deletion would never reach the server, and the entry would stay live for
    every other investigator.
    """
    entry = make_entry()
    store.save(entry)
    uploaded = store.list_pending()[0]

    store.soft_delete(entry.entry_id)

    assert store.mark_synced(uploaded.entry_id, 1, uploaded.local_revision) is False
    row = store.get(entry.entry_id)
    assert row.is_deleted is True
    assert row.sync_state is SyncState.PENDING, "the deletion must still be queued"


def test_sync_bookkeeping_does_not_bump_the_revision(store: LocalStore) -> None:
    """The counterpart rule: marking sync state is not a content change, or every
    sync attempt would invalidate itself."""
    entry = make_entry()
    store.save(entry)
    before = store.get(entry.entry_id).local_revision

    store.mark_sync_error(entry.entry_id, "boom")
    assert store.get(entry.entry_id).local_revision == before

    store.requeue_all()
    assert store.get(entry.entry_id).local_revision == before


# --------------------------------------------------------------------------- #
# Editing an entry must not move it to another case
# --------------------------------------------------------------------------- #


def test_editing_an_entry_keeps_its_own_case(store: LocalStore, tmp_path) -> None:
    """Editing an old entry must not re-stamp it with whatever Case ID is in
    Settings right now.

    The modal builds its field dict from current settings and merges it over the
    existing row. `case_id` came from that dict, so opening an entry belonging to
    one case while Settings named another silently moved the entry into the
    second case - and marked it pending, so the move was pushed to the server and
    the entry vanished from the first case's log. An observation belongs to the
    case it was recorded in; changing which case you are working on cannot
    retroactively reassign old evidence.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    from evidence_review.autofill import MediaContext
    from evidence_review.config import Settings
    from evidence_review.ui.log_dialog import LogEntryDialog

    media = tmp_path / "Basement_Cam2.mp4"
    media.write_bytes(b"not really a video")

    existing = make_entry(
        case_id="willow-house",
        media_path=str(media),
        file_name=media.name,
        values={OBSERVATION: "Two distinct knocks from the north wall."},
    )
    store.save(existing)

    # The reviewer has since switched to a different case.
    settings = Settings()
    settings.review.case_id = "harbour-street"
    settings.review.case_name = "Harbour Street"

    dialog = LogEntryDialog(
        context=MediaContext(path=media, kind="video", duration_seconds=3600.0),
        settings=settings,
        store=store,
        event_offset_seconds=existing.event_offset_seconds,
        snapshot_path=existing.snapshot_path,
        existing=existing,
    )
    try:
        rebuilt = dialog._build_entry()
    finally:
        dialog.deleteLater()

    assert rebuilt.case_id == "willow-house", (
        "editing re-stamped the entry with the Case ID from Settings, "
        "moving it out of the case it was recorded in"
    )


# --------------------------------------------------------------------------- #
# The pull cursor belongs to the case, not to the worker
# --------------------------------------------------------------------------- #


def test_pull_cursor_survives_a_case_switch(store: LocalStore) -> None:
    """Switching cases must not re-pull the case you come back to.

    The cursor used to be `SyncWorker` instance state, and the worker is rebuilt
    on every settings change and every case switch. A reviewer moving between
    cases in one sitting therefore re-pulled each case from the beginning each
    time, and so did every app restart. Storing it on the case makes resuming the
    default.
    """
    from evidence_review.models import Case

    store.upsert_case(Case(case_id="willow-house", name="Willow House"))
    store.upsert_case(Case(case_id="harbour-street", name="Harbour Street"))

    assert store.get_pull_cursor("willow-house") is None, "a fresh case starts at the top"

    store.set_pull_cursor("willow-house", "cursor-w-1")
    store.set_pull_cursor("harbour-street", "cursor-h-1")

    # Cursors are per case, and neither treads on the other.
    assert store.get_pull_cursor("willow-house") == "cursor-w-1"
    assert store.get_pull_cursor("harbour-street") == "cursor-h-1"

    # Reopening the database is the same thing as restarting the application.
    store.close()
    assert store.get_pull_cursor("willow-house") == "cursor-w-1"


def test_pull_cursor_records_a_case_never_seen_locally(store: LocalStore) -> None:
    """A case first met by pulling it has no local row yet. A bare UPDATE would
    silently do nothing and that case would re-pull from the beginning forever."""
    store.set_pull_cursor("arrived-by-pull", "cursor-1")

    assert store.get_pull_cursor("arrived-by-pull") == "cursor-1"
    assert "arrived-by-pull" in {case.case_id for case in store.list_cases()}


def test_area_suggestions_come_from_this_case_only(store: LocalStore, tmp_path) -> None:
    """Areas are the rooms of one building. Offering every area from every case
    makes the list useless once a few cases are in play, and invites filing an
    observation under a room that belongs to a different investigation."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    from evidence_review.autofill import MediaContext
    from evidence_review.config import Settings
    from evidence_review.ui.log_dialog import LogEntryDialog

    store.save(make_entry(case_id="willow-house", values={AREA: "Willow Basement"}))
    store.save(make_entry(case_id="harbour-street", values={AREA: "Harbour Attic"}))

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not really a video")

    settings = Settings()
    settings.review.case_id = "willow-house"
    settings.review.case_name = "Willow House"
    settings.review.areas = []
    settings.review.guess_area_from_path = False

    dialog = LogEntryDialog(
        context=MediaContext(path=media, kind="video", duration_seconds=10.0),
        settings=settings,
        store=store,
        event_offset_seconds=0.0,
    )
    try:
        # The location field is found by role, so this holds for a template that
        # calls it Area and one that calls it Location / Camera.
        field = dialog.template.location_field
        combo = dialog.editor_for(field.field_id).inner
        offered = [combo.itemText(index) for index in range(combo.count())]
    finally:
        dialog.deleteLater()

    assert "Willow Basement" in offered
    assert "Harbour Attic" not in offered, "another case's areas must not be offered"
