"""An entry shown in more cases than its own (phase 3, 2026-10-07).

An entry keeps one home case and can be linked into others. The links travel with
the entry as ``also_in``. "Not known" (an entry from a server without links) must
stay distinguishable from "in no other case", or an old entry pushed back would
erase links made elsewhere.
"""

from __future__ import annotations

from evidence_review.models import Case, SyncState

from .conftest import OBSERVATION, make_entry


def test_links_are_kept_and_the_entry_shows_in_each_case(store) -> None:
    for cid in ("alpha", "bravo"):
        store.upsert_case(Case(case_id=cid, name=cid))
    entry = make_entry(case_id="alpha", values={OBSERVATION: "Knock"})
    entry.sync_state = SyncState.SYNCED
    store.save(entry)
    assert store.set_links([entry.entry_id], "bravo", linked=True) == 1
    stored = store.get(entry.entry_id)
    assert stored.case_id == "alpha" and stored.also_in == ["bravo"]
    assert stored.sync_state is SyncState.PENDING, "a link is an edit, and syncs"
    assert [e.entry_id for e in store.list_entries(case_id="bravo")] == [entry.entry_id]
    assert [e.entry_id for e in store.list_entries(case_id="alpha")] == [entry.entry_id]
    assert store.set_links([entry.entry_id], "bravo", linked=False) == 1
    assert store.list_entries(case_id="bravo") == []
    assert store.get(entry.entry_id).also_in == []


def test_never_linked_into_its_own_case(store) -> None:
    entry = make_entry(case_id="alpha")
    store.save(entry)
    assert store.set_links([entry.entry_id], "alpha", linked=True) == 0


def test_unknown_links_are_sent_as_null_not_as_none(store) -> None:
    entry = make_entry(case_id="alpha")
    store.save(entry)
    assert store.get(entry.entry_id).also_in is None
    assert store.get(entry.entry_id).wire_payload()["also_in"] is None


def test_a_pulled_entry_brings_its_links(store) -> None:
    remote = make_entry(case_id="alpha", values={OBSERVATION: "From the site"})
    remote.also_in = ["bravo"]
    store.merge_remote([remote.to_wire()])
    assert store.get(remote.entry_id).also_in == ["bravo"]
    assert len(store.list_entries(case_id="bravo")) == 1
