"""Upgrading a database that already holds real work.

The template migration moves four columns' worth of answers into a related table.
If it loses one observation it has destroyed evidence, so the test is not "does it
run" but "is every answer still there, and does the old column still say the same
thing" -- because the columns are not dropped until a later release, and a reviewer
who has to roll back still needs their data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from evidence_review import store as store_module
from evidence_review.builtin_templates import DEFAULT_TEMPLATE_ID, PARANORMAL_TEMPLATE
from evidence_review.store import LocalStore
from evidence_review.util import to_iso, utc_now

#: The shape 1.1.0 left behind: area, observation, debunk_reasoning and status as
#: columns, and no templates anywhere.
SCHEMA_BEFORE_TEMPLATES = 5

SAMPLE = [
    ("e1", "Attic", "Loud knock, three times", None, "Unexplained"),
    ("e2", "Cellar", "Figure in the doorway", "A coat on a hook", "Debunked"),
    # Deliberately awkward: no area, an empty-string debunk reason, a status the
    # reviewer invented, and an em dash to catch an encoding mistake.
    ("e3", "", "EVP at 14:02", "", "Equipment Artifact"),
    ("e4", "Landing", "Door moves", None, "Draught — confirmed"),
]


@pytest.fixture
def legacy_database(tmp_path: Path, monkeypatch) -> Path:
    """A database at schema 5, filled the way the previous release filled it."""
    db = tmp_path / "evidence.db"

    monkeypatch.setattr(
        store_module,
        "MIGRATIONS",
        [step for step in store_module.MIGRATIONS if step[0] <= SCHEMA_BEFORE_TEMPLATES],
    )
    monkeypatch.setattr(
        store_module,
        "POST_MIGRATIONS",
        {
            version: step
            for version, step in store_module.POST_MIGRATIONS.items()
            if version <= SCHEMA_BEFORE_TEMPLATES
        },
    )
    # That release had no template seeding either, so it is stubbed out alongside the
    # migrations it belongs to rather than run against tables that do not exist yet.
    monkeypatch.setattr(LocalStore, "seed_builtin_templates", lambda self: None)

    old = LocalStore(db)
    now = to_iso(utc_now())
    for entry_id, area, observation, debunk, status in SAMPLE:
        old.conn.execute(
            "INSERT INTO log_entries (entry_id, case_id, file_name, media_path, media_kind, "
            "area, observation, debunk_reasoning, status, investigator_name, date_reviewed, "
            "created_at_utc, updated_at_utc, is_deleted, sync_state, local_revision) "
            "VALUES (?, 'case-a', ?, '', 'video', ?, ?, ?, ?, 'Jane', '2026-09-01', ?, ?, 0, "
            "'synced', 3)",
            (entry_id, f"{entry_id}.mp4", area, observation, debunk, status, now, now),
        )
    old.conn.commit()
    old.close()

    # The patches exist only to *build* the old database. They are undone before the
    # test runs, so what the test then opens it with is the real shipping migration
    # list -- which is the only thing worth testing.
    monkeypatch.undo()
    return db


def schema_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()


def test_the_fixture_really_is_the_old_shape(legacy_database: Path) -> None:
    """Otherwise the tests below would be upgrading something already upgraded, and
    would pass without proving anything."""
    assert schema_version(legacy_database) == SCHEMA_BEFORE_TEMPLATES
    conn = sqlite3.connect(legacy_database)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "entry_values" not in tables
        assert "templates" not in tables
    finally:
        conn.close()


def test_every_answer_survives_the_upgrade(legacy_database: Path) -> None:
    store = LocalStore(legacy_database)
    try:
        assert schema_version(legacy_database) > SCHEMA_BEFORE_TEMPLATES
        field = {
            key: PARANORMAL_TEMPLATE.field_by_key(key).field_id
            for key in ("area", "observation", "debunk_reasoning", "status")
        }

        for entry_id, area, observation, debunk, status in SAMPLE:
            entry = store.get(entry_id)
            assert entry is not None, f"{entry_id} did not survive"
            assert entry.template_id == DEFAULT_TEMPLATE_ID

            assert entry.values[field["observation"]] == observation
            assert entry.values[field["status"]] == status
            # An empty column must not become an empty value row: absent and blank
            # are the same thing, and storing blanks would make "has a debunk
            # reason" answer yes for every entry.
            assert entry.values.get(field["area"]) == (area or None)
            assert entry.values.get(field["debunk_reasoning"]) == (debunk or None)
    finally:
        store.close()


def test_the_original_columns_are_left_alone(legacy_database: Path) -> None:
    """They are dropped a release later than the copy, so a rollback still reads."""
    LocalStore(legacy_database).close()

    conn = sqlite3.connect(legacy_database)
    conn.row_factory = sqlite3.Row
    try:
        for entry_id, area, observation, debunk, status in SAMPLE:
            row = conn.execute(
                "SELECT area, observation, debunk_reasoning, status FROM log_entries "
                "WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            assert (row["area"], row["observation"], row["status"]) == (area, observation, status)
            assert row["debunk_reasoning"] == debunk
    finally:
        conn.close()


def test_the_upgrade_does_not_requeue_the_whole_case(legacy_database: Path) -> None:
    """Moving data between columns is not a content change. Re-queueing every entry
    would push the entire case back to the server for no reason, and bumping the
    revision would let a genuine edit be marked synced by mistake."""
    store = LocalStore(legacy_database)
    try:
        for entry_id, *_ in SAMPLE:
            entry = store.get(entry_id)
            assert entry.sync_state.value == "synced"
            assert entry.local_revision == 3
        assert store.pending_count() == 0
    finally:
        store.close()


def test_a_custom_status_is_preserved_as_a_value(legacy_database: Path) -> None:
    """The reviewer invented it under the old build; it is data, not vocabulary, and
    the migration must not normalise it away."""
    store = LocalStore(legacy_database)
    try:
        status_field = PARANORMAL_TEMPLATE.field_by_key("status").field_id
        assert store.get("e4").values[status_field] == "Draught — confirmed"
    finally:
        store.close()


def test_search_still_finds_prose_written_before_the_upgrade(legacy_database: Path) -> None:
    """Search moved from four named columns to a join on the answers. An entry
    written years ago has to stay findable."""
    store = LocalStore(legacy_database)
    try:
        assert [found.entry_id for found in store.list_entries(search="knock")] == ["e1"]
        assert [found.entry_id for found in store.list_entries(search="Cellar")] == ["e2"]
    finally:
        store.close()


def test_the_migration_is_not_run_twice(legacy_database: Path) -> None:
    """Opening the database again must not duplicate a single answer."""
    first = LocalStore(legacy_database)
    counts = {entry_id: len(first.get(entry_id).values) for entry_id, *_ in SAMPLE}
    first.close()

    second = LocalStore(legacy_database)
    try:
        for entry_id, *_ in SAMPLE:
            assert len(second.get(entry_id).values) == counts[entry_id]
        total = second.conn.execute("SELECT COUNT(*) AS n FROM entry_values").fetchone()["n"]
        assert total == sum(counts.values())
    finally:
        second.close()
