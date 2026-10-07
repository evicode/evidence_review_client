"""The local store is the one thing that must never lose an entry."""

from __future__ import annotations

import datetime as _dt

import pytest

from evidence_review.builtin_templates import PARANORMAL_TEMPLATE
from evidence_review.models import DEBUNKED_STATUS, EntryRow, MediaKind, SyncState
from evidence_review.store import LocalStore

from .conftest import AREA, DEBUNK, OBSERVATION, STATUS, make_entry


def test_save_and_read_back_round_trips_every_field(store: LocalStore, sample_entry) -> None:
    store.save(sample_entry)
    loaded = store.get(sample_entry.entry_id)

    assert loaded is not None
    assert loaded.file_name == sample_entry.file_name
    assert loaded.values == sample_entry.values
    assert loaded.investigator_name == sample_entry.investigator_name
    assert loaded.event_offset_seconds == pytest.approx(83.4)
    assert loaded.event_duration_seconds == pytest.approx(3.7)
    assert loaded.media_duration_seconds == pytest.approx(3600.0)
    assert loaded.media_sha256 == sample_entry.media_sha256
    assert loaded.date_reviewed == sample_entry.date_reviewed


def test_save_is_an_upsert(store: LocalStore, sample_entry) -> None:
    store.save(sample_entry)
    sample_entry.values[OBSERVATION] = "Revised description."
    store.save(sample_entry)

    assert store.count_entries() == 1
    assert store.get(sample_entry.entry_id).values[OBSERVATION] == "Revised description."


def test_migrations_are_idempotent(tmp_path) -> None:
    path = tmp_path / "evidence.db"
    first = LocalStore(path)
    first.save(make_entry())
    first.close()

    second = LocalStore(path)  # re-running migrations must not wipe anything
    assert second.count_entries() == 1
    second.close()


def test_soft_delete_keeps_the_row_and_requeues_it(store: LocalStore, sample_entry) -> None:
    sample_entry.sync_state = SyncState.SYNCED
    store.save(sample_entry)
    store.soft_delete(sample_entry.entry_id)

    assert store.count_entries() == 0, "deleted entries are hidden from normal listings"
    assert store.list_entries(include_deleted=True), "but the row is still there"

    row = store.get(sample_entry.entry_id)
    assert row.is_deleted is True
    assert row.sync_state is SyncState.PENDING, "the deletion must propagate to the server"


def test_pending_tracking(store: LocalStore) -> None:
    for index in range(3):
        store.save(make_entry(values={OBSERVATION: f"entry {index}"}))
    assert store.pending_count() == 3
    assert len(store.list_pending()) == 3

    first = store.list_pending()[0]
    store.mark_synced(first.entry_id, 42, first.local_revision)

    assert store.pending_count() == 2
    assert store.get(first.entry_id).server_id == 42
    assert store.get(first.entry_id).sync_state is SyncState.SYNCED


def test_sync_errors_stay_in_the_retry_queue(store: LocalStore) -> None:
    entry = make_entry()
    store.save(entry)
    store.mark_sync_error(entry.entry_id, "server said no")

    reloaded = store.get(entry.entry_id)
    assert reloaded.sync_state is SyncState.ERROR
    assert reloaded.sync_error == "server said no"
    assert entry.entry_id in {row.entry_id for row in store.list_pending()}


def test_requeue_all(store: LocalStore) -> None:
    for _ in range(3):
        entry = make_entry()
        store.save(entry)
        store.mark_synced(entry.entry_id, 1, store.get(entry.entry_id).local_revision)
    assert store.pending_count() == 0

    assert store.requeue_all() == 3
    assert store.pending_count() == 3


def test_filtering_by_case_and_search(store: LocalStore) -> None:
    store.save(make_entry(case_id="alpha", values={OBSERVATION: "knocking on the north wall"}))
    store.save(make_entry(case_id="alpha", values={OBSERVATION: "a cold draught"}))
    store.save(make_entry(case_id="beta", values={OBSERVATION: "knocking again"}))

    assert len(store.list_entries(case_id="alpha")) == 2
    assert len(store.list_entries(search="knocking")) == 2
    assert len(store.list_entries(case_id="alpha", search="knocking")) == 1


def test_area_vocabulary_and_folder_memory(store: LocalStore) -> None:
    store.save(make_entry(values={AREA: "Basement"}))
    store.save(make_entry(values={AREA: "Attic"}))
    store.save(make_entry(values={AREA: "Basement"}))

    assert store.distinct_field_values(AREA) == ["Attic", "Basement"]

    store.remember_area_for_folder(r"C:\evidence\Cam2", "Basement")
    assert store.recall_area_for_folder(r"C:\EVIDENCE\CAM2") == "Basement"
    assert store.recall_area_for_folder(r"C:\somewhere\else") is None


def test_hash_cache_is_keyed_on_size_and_mtime(store: LocalStore) -> None:
    store.put_cached_hash("C:/a.mp4", 100, 1234, "deadbeef")
    assert store.get_cached_hash("C:/a.mp4", 100, 1234) == "deadbeef"
    # A file that changed size or mtime must not reuse the stale hash.
    assert store.get_cached_hash("C:/a.mp4", 101, 1234) is None
    assert store.get_cached_hash("C:/a.mp4", 100, 9999) is None


def test_merge_remote_keeps_the_newer_local_edit(store: LocalStore, sample_entry) -> None:
    newer = sample_entry.model_copy(
        update={"updated_at_utc": _dt.datetime(2026, 9, 16, tzinfo=_dt.UTC)}
    )
    newer.values[OBSERVATION] = "local edit"
    store.save(newer)

    stale = sample_entry.to_wire().model_copy(
        update={
            "observation": "older server version",
            "updated_at_utc": _dt.datetime(2026, 9, 15, tzinfo=_dt.UTC),
        }
    )
    assert store.merge_remote([stale]) == 0
    assert store.get(sample_entry.entry_id).values[OBSERVATION] == "local edit"


def test_merge_remote_accepts_a_newer_server_version(store: LocalStore, sample_entry) -> None:
    store.save(sample_entry)
    remote = sample_entry.to_wire().model_copy(
        update={
            "values": {**sample_entry.values, OBSERVATION: "corrected by a colleague"},
            "updated_at_utc": _dt.datetime(2026, 9, 20, tzinfo=_dt.UTC),
        }
    )
    assert store.merge_remote([remote]) == 1

    merged = store.get(sample_entry.entry_id)
    assert merged.values[OBSERVATION] == "corrected by a colleague"
    assert merged.sync_state is SyncState.SYNCED


def test_entries_are_ordered_newest_logged_first(store: LocalStore) -> None:
    """The session log reads as a running list of what was logged, newest first."""
    old = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    new = _dt.datetime(2026, 6, 1, tzinfo=_dt.UTC)
    store.save(make_entry(created_at_utc=old, updated_at_utc=old, values={OBSERVATION: "older"}))
    store.save(make_entry(created_at_utc=new, updated_at_utc=new, values={OBSERVATION: "newer"}))

    assert [e.values[OBSERVATION] for e in store.list_entries()] == ["newer", "older"]


# --------------------------------------------------------------------------- #
# Model validation
# --------------------------------------------------------------------------- #


def test_debunked_requires_reasoning() -> None:
    entry = make_entry(values={STATUS: DEBUNKED_STATUS})
    problems = entry.validation_problems(PARANORMAL_TEMPLATE)
    assert any("Debunk Reasoning" in problem for problem in problems)

    entry.values[DEBUNK] = "Contractor's generator two doors down."
    assert entry.validation_problems(PARANORMAL_TEMPLATE) == []


def test_required_fields_are_reported() -> None:
    entry = EntryRow(file_name="x.mp4", investigator_name="", values={})
    problems = " ".join(entry.validation_problems(PARANORMAL_TEMPLATE))
    assert "What Was Heard or Seen" in problems
    assert "Area" in problems
    assert "Investigator Name" in problems


def test_an_event_cannot_run_past_the_end_of_the_media() -> None:
    entry = make_entry(
        event_offset_seconds=3500.0, event_duration_seconds=200.0, media_duration_seconds=3600.0
    )
    assert any(
        "past the end" in problem for problem in entry.validation_problems(PARANORMAL_TEMPLATE)
    )


def test_event_end_is_derived_from_the_duration() -> None:
    entry = make_entry(event_offset_seconds=10.0, event_duration_seconds=5.0)
    assert entry.has_duration
    assert entry.event_end_seconds == pytest.approx(15.0)
    assert make_entry(event_offset_seconds=10.0).event_end_seconds is None


def test_wire_payload_drops_local_only_columns(sample_entry) -> None:
    payload = sample_entry.wire_payload()
    for field in ("sync_state", "sync_error", "synced_at_utc", "server_id"):
        assert field not in payload
    assert payload["entry_id"] == sample_entry.entry_id
    assert payload["values"][OBSERVATION] == sample_entry.values[OBSERVATION]


def test_entries_stay_attached_to_a_file_whose_path_changes_case(tmp_path) -> None:
    """Windows and macOS both treat these as one file; an exact match did not.

    The marker bar and the waveform's marks are both built by asking for the entries
    belonging to the open file's path. A file dialog and a folder listing do not always
    agree about the drive letter's case, so the same recording could arrive spelled two
    ways in one session -- and the second time, every event already logged against it
    vanished from the timeline. The work was still in the database, which is worse than
    losing it: the reviewer has no reason to think anything is wrong.
    """
    store = LocalStore(tmp_path / "e.db")
    stored_as = r"C:\Evidence\Cellar.wav"
    entry = store.save(
        EntryRow(
            case_id="c",
            file_name="Cellar.wav",
            media_path=stored_as,
            media_kind=MediaKind.AUDIO,
            event_offset_seconds=12.0,
        )
    )

    for spelling in (stored_as, r"c:\evidence\cellar.wav", r"C:\EVIDENCE\CELLAR.WAV"):
        found = store.list_entries(case_id="c", media_path=spelling)
        assert [e.entry_id for e in found] == [entry.entry_id], (
            f"the entry came unattached when the path was spelled {spelling!r}"
        )

    # Still a filter, not a free-for-all.
    assert store.list_entries(case_id="c", media_path=r"C:\Evidence\Landing.wav") == []
    store.close()
