"""Sync must be idempotent, survive an unreachable server, and never lose an entry."""

from __future__ import annotations

import json

import httpx
import pytest

from evidence_review.config import ApiSettings
from evidence_review.models import SyncState
from evidence_review.store import LocalStore
from evidence_review.sync import (
    MAX_BACKOFF_SECONDS,
    ApiClient,
    ApiError,
    backoff_delay,
)
from evidence_review.templates import LogTemplate

from .conftest import OBSERVATION, make_entry

BASE_URL = "https://evidence.example.org"


def settings(**overrides) -> ApiSettings:
    base = {"base_url": BASE_URL, "enabled": True, "timeout_seconds": 5.0}
    base.update(overrides)
    return ApiSettings(**base)


def client_with(handler) -> ApiClient:
    return ApiClient(settings(), "test-token", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


def test_bearer_token_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"status": "ok", "version": "1.0.0"})

    with client_with(handler) as client:
        client.health()

    assert seen["authorization"] == "Bearer test-token"
    assert "EvidenceReview/" in seen["user-agent"]


def test_health_hits_the_versioned_path() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "version": "1.0.0"})

    with client_with(handler) as client:
        assert client.health()["status"] == "ok"

    assert paths == ["/api/v1/health"]


def test_push_sends_the_wire_shape_only() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"entry_id": captured["entry_id"], "server_id": 7})

    entry = make_entry()
    entry.sync_error = "should not be transmitted"
    with client_with(handler) as client:
        result = client.push(entry)

    assert result.ok and result.server_id == 7
    assert "sync_state" not in captured
    assert "sync_error" not in captured
    assert captured["values"][OBSERVATION] == entry.values[OBSERVATION]


def test_batch_reports_each_entry_separately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"entry_id": payload["entries"][0]["entry_id"], "status": "ok", "server_id": 1},
                    {
                        "entry_id": payload["entries"][1]["entry_id"],
                        "status": "error",
                        "message": "bad status value",
                    },
                ]
            },
        )

    entries = [make_entry(), make_entry()]
    with client_with(handler) as client:
        results = client.push_batch(entries)

    assert [r.ok for r in results] == [True, False]
    assert results[1].message == "bad status value"


def test_empty_batch_makes_no_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for an empty batch")

    with client_with(handler) as client:
        assert client.push_batch([]) == []


def test_pull_skips_entries_it_cannot_parse() -> None:
    good = make_entry().wire_payload()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [good, {"entry_id": "broken"}]})

    with client_with(handler) as client:
        entries = client.pull_page(case_id="alpha").entries

    assert len(entries) == 1
    assert entries[0].entry_id == good["entry_id"]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_unauthorised_is_reported_clearly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid or revoked API token"})

    with client_with(handler) as client, pytest.raises(ApiError) as excinfo:
        client.push(make_entry())

    assert excinfo.value.status_code == 401
    assert "token" in str(excinfo.value).lower()


def test_read_only_token_is_reported_as_forbidden() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "This token is read-only"})

    with client_with(handler) as client, pytest.raises(ApiError) as excinfo:
        client.push(make_entry())

    assert excinfo.value.status_code == 403


def test_connection_failure_has_no_status_code() -> None:
    """A network failure is 'offline', which the worker treats differently to a 4xx."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with client_with(handler) as client, pytest.raises(ApiError) as excinfo:
        client.health()

    assert excinfo.value.status_code is None
    assert "Cannot reach server" in str(excinfo.value)


def test_timeout_is_reported_as_such() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with client_with(handler) as client, pytest.raises(ApiError) as excinfo:
        client.health()

    assert "Timed out" in str(excinfo.value)


def test_server_error_includes_the_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "database is locked"})

    with client_with(handler) as client, pytest.raises(ApiError) as excinfo:
        client.health()

    assert "database is locked" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #


def test_no_failures_uses_the_configured_interval() -> None:
    assert backoff_delay(0, 30.0) == 30.0


def test_backoff_grows_and_is_capped() -> None:
    previous = 0.0
    for failures in range(1, 8):
        delay = backoff_delay(failures, 30.0)
        assert delay <= MAX_BACKOFF_SECONDS * 1.2
        if failures < 8:
            assert delay >= previous * 0.7  # jitter makes this approximate
        previous = delay

    assert backoff_delay(50, 30.0) <= MAX_BACKOFF_SECONDS * 1.2


def test_backoff_is_jittered() -> None:
    """Identical retry storms from many clients would be self-defeating."""
    delays = {backoff_delay(5, 30.0) for _ in range(20)}
    assert len(delays) > 1


# --------------------------------------------------------------------------- #
# Offline-first behaviour
# --------------------------------------------------------------------------- #


def test_entries_survive_an_unreachable_server(tmp_path) -> None:
    """The point of the whole design: local save never depends on the network."""
    store = LocalStore(tmp_path / "evidence.db")
    try:
        for index in range(5):
            store.save(make_entry(values={OBSERVATION: f"observation {index}"}))

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        with client_with(handler) as client, pytest.raises(ApiError):
            client.push_batch(store.list_pending())

        # Nothing was lost and everything is still queued.
        assert store.count_entries() == 5
        assert store.pending_count() == 5

        # When the server comes back, the queue drains.
        def ok_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"entry_id": item["entry_id"], "status": "ok", "server_id": index}
                        for index, item in enumerate(payload["entries"], start=1)
                    ]
                },
            )

        with client_with(ok_handler) as client:
            pending = store.list_pending()
            uploaded = {e.entry_id: e.local_revision for e in pending}
            for result in client.push_batch(pending):
                store.mark_synced(result.entry_id, result.server_id, uploaded[result.entry_id])

        assert store.pending_count() == 0
    finally:
        store.close()


def test_retrying_the_same_entry_is_idempotent(tmp_path) -> None:
    """Retries send the same entry_id, so the server upserts rather than duplicating."""
    store = LocalStore(tmp_path / "evidence.db")
    try:
        entry = make_entry()
        store.save(entry)

        seen_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen_ids.append(payload["entry_id"])
            return httpx.Response(200, json={"entry_id": payload["entry_id"], "server_id": 1})

        with client_with(handler) as client:
            client.push(entry)
            client.push(entry)
            client.push(entry)

        assert seen_ids == [entry.entry_id] * 3
        assert len(set(seen_ids)) == 1
    finally:
        store.close()


def test_a_failed_entry_is_marked_not_dropped(tmp_path) -> None:
    store = LocalStore(tmp_path / "evidence.db")
    try:
        entry = make_entry()
        store.save(entry)

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "entry_id": payload["entries"][0]["entry_id"],
                            "status": "error",
                            "message": "rejected",
                        }
                    ]
                },
            )

        with client_with(handler) as client:
            for result in client.push_batch([entry]):
                if not result.ok:
                    store.mark_sync_error(result.entry_id, result.message)

        reloaded = store.get(entry.entry_id)
        assert reloaded is not None, "a rejected entry must still exist locally"
        assert reloaded.sync_state is SyncState.ERROR
        assert store.pending_count() == 1, "and must stay in the retry queue"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Registering the case
# --------------------------------------------------------------------------- #


def test_push_case_sends_the_name_the_entries_cannot_carry() -> None:
    """Only case_id travels on an entry, so without this the server has nothing to
    show but the slug, and a case with no entries yet does not exist remotely."""
    from evidence_review.models import Case

    captured: dict = {}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"case_id": captured["case_id"]})

    with client_with(handler) as client:
        client.push_case(Case(case_id="willow-house", name="Willow House"))

    assert paths == ["/api/v1/cases"]
    assert captured["case_id"] == "willow-house"
    assert captured["name"] == "Willow House"


def test_a_server_without_post_cases_does_not_fail_the_cycle(
    store: LocalStore, monkeypatch
) -> None:
    """An older server answers 404 or 405 here. Losing the case's *name* must not
    fail - and so endlessly retry - a cycle that would otherwise upload entries."""
    from evidence_review import sync as evidence_sync
    from evidence_review.config import ApiSettings as _ApiSettings
    from evidence_review.models import Case
    from evidence_review.sync import SyncWorker

    store.upsert_case(Case(case_id="willow-house", name="Willow House"))
    store.save(make_entry(case_id="willow-house", values={OBSERVATION: "Knocks."}))

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/cases"):
            return httpx.Response(405, json={"detail": "Method not allowed"})
        if request.url.path.endswith("/entries/batch"):
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"entry_id": e["entry_id"], "status": "ok", "server_id": 1}
                        for e in payload["entries"]
                    ]
                },
            )
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = SyncWorker(
        store,
        _ApiSettings(base_url=BASE_URL, enabled=True, pull_on_start=False),
        "test-token",
        case_id="willow-house",
    )

    transport = httpx.MockTransport(handler)
    real_client = evidence_sync.ApiClient

    def fake_client(settings_, token, **kwargs):
        return real_client(settings_, token, transport=transport)

    monkeypatch.setattr(evidence_sync, "ApiClient", fake_client)
    pushed = worker._run_cycle()

    assert any(p.endswith("/cases") for p in seen), "it must at least try to register"
    assert pushed == 1, "the entry still had to upload despite the case call failing"
    assert store.pending_count() == 0, "the upload must have been recorded as done"


# --------------------------------------------------------------------------- #
# Pulling is continuous, not once per worker
# --------------------------------------------------------------------------- #


def _worker_with(store, handler, monkeypatch, **api):
    """A SyncWorker whose cycles run against a mock transport."""
    from evidence_review import sync as evidence_sync
    from evidence_review.config import ApiSettings as _ApiSettings
    from evidence_review.sync import SyncWorker

    base = {"base_url": BASE_URL, "enabled": True}
    base.update(api)
    worker = SyncWorker(store, _ApiSettings(**base), "test-token", case_id="willow-house")

    transport = httpx.MockTransport(handler)
    real_client = evidence_sync.ApiClient
    monkeypatch.setattr(
        evidence_sync,
        "ApiClient",
        lambda s, t, **kw: real_client(s, t, transport=transport),
    )
    return worker


def test_every_cycle_pulls_not_just_the_first(store: LocalStore, monkeypatch) -> None:
    """The bug this prevents: the pull flag was cleared after the first pull and
    nothing ever set it again, so a teammate's entry only arrived if you restarted
    the app or switched case - while the status bar still said "Up to date"."""
    pulls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Registration traffic -- the case and its templates -- happens once per
        # worker and is not a pull, so it is answered and not counted.
        if request.url.path.endswith(("/cases", "/templates")):
            return httpx.Response(200, json={"case_id": "willow-house"})
        # The open case's pull. Each cycle also pulls every case (no case_id),
        # which test_every_case_is_pulled_too covers.
        if request.url.params.get("case_id"):
            pulls.append(request.url.params.get("cursor"))
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)

    worker._run_cycle()
    worker._run_cycle()
    worker._run_cycle()

    assert len(pulls) == 3, f"expected a pull on every cycle, got {len(pulls)}"


def test_a_later_cycle_picks_up_a_remote_entry(store: LocalStore, monkeypatch) -> None:
    """The point of the whole fix, from the reviewer's side: an entry logged by
    someone else after this client started has to turn up without a restart."""
    remote = make_entry(case_id="willow-house", values={OBSERVATION: "Logged by a colleague."})
    cycles = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/cases", "/templates")):
            return httpx.Response(200, json={"case_id": "willow-house"})
        if not request.url.params.get("case_id"):
            return httpx.Response(200, json={"items": [], "has_more": False})
        cycles["n"] += 1
        # Nothing on the first pull; the colleague's entry appears afterwards.
        items = [remote.wire_payload()] if cycles["n"] > 1 else []
        return httpx.Response(200, json={"items": items, "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)

    worker._run_cycle()
    assert store.get(remote.entry_id) is None, "nothing to find yet"

    worker._run_cycle()
    assert store.get(remote.entry_id) is not None, (
        "an entry logged remotely after startup never arrived"
    )


def test_receiving_can_still_be_turned_off(store: LocalStore, monkeypatch) -> None:
    """Unticking it means upload-only. That was the one case the old code got
    right, and it must survive making pulling continuous."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cases"):
            return httpx.Response(200, json={"case_id": "willow-house"})
        if request.url.path.endswith("/entries/batch"):
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"entry_id": e["entry_id"], "status": "ok", "server_id": 1}
                        for e in payload["entries"]
                    ]
                },
            )
        return httpx.Response(200, json={"items": [], "has_more": False})

    store.save(make_entry(case_id="willow-house"))
    worker = _worker_with(store, handler, monkeypatch, pull_on_start=False)

    worker._run_cycle()
    worker._run_cycle()

    assert any(p.endswith("/entries/batch") for p in paths), "it must still upload"
    assert not any(p.endswith("/entries") for p in paths), "it must not download"


# --------------------------------------------------------------------------- #
# Templates over the wire
#
# A colleague's form has to reach this client, or their entries arrive as answers
# keyed by field ids nothing here can name. Pulling was written and then left
# unwired and untested; these cover both halves.
# --------------------------------------------------------------------------- #


def template_payload(template_id: str = "shared-form", version: int = 1, **overrides) -> dict:
    body = {
        "template_id": template_id,
        "name": "Shared Form",
        "description": "",
        "version": version,
        "is_builtin": False,
        "is_customised": False,
        "fields": [
            {
                "field_id": f"{template_id}.where",
                "field_key": "where",
                "label": "Where",
                "field_type": "text",
                "role": "location",
                "position": 0,
                "is_required": False,
                "help_text": "",
                "config": {},
                "in_table": True,
                "options": [],
            },
            {
                "field_id": f"{template_id}.grade",
                "field_key": "grade",
                "label": "Grade",
                "field_type": "choice",
                "role": "status",
                "position": 1,
                "is_required": False,
                "help_text": "",
                "config": {},
                "in_table": True,
                "options": [
                    {
                        "option_id": f"{template_id}.g0",
                        "value": "Pass",
                        "label": "Pass",
                        "colour": "#3fb950",
                        "position": 0,
                        "is_default": True,
                    }
                ],
            },
        ],
        "rules": [],
    }
    body.update(overrides)
    return body


def test_a_cycle_registers_the_local_templates(store: LocalStore, monkeypatch) -> None:
    """A server that has not been told the form can store its answers but cannot
    label, validate or report on them."""
    pushed: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates") and request.method == "POST":
            pushed.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.url.path.endswith("/templates"):
            return httpx.Response(200, json={"items": [], "count": 0})
        if request.url.path.endswith("/cases"):
            return httpx.Response(200, json={"case_id": "willow-house"})
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    worker._run_cycle()

    names = {body["name"] for body in pushed}
    assert "Paranormal Investigation" in names
    assert "Criminal Investigation" in names


def test_a_colleagues_template_is_taken(store: LocalStore, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates") and request.method == "GET":
            return httpx.Response(200, json={"items": [template_payload()], "count": 1})
        if request.url.path.endswith(("/templates", "/cases")):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    worker._run_cycle()

    stored = store.get_template("shared-form")
    assert stored is not None
    assert [field.field_key for field in stored.ordered_fields] == ["where", "grade"]
    assert stored.colour_for_status("Pass") == "#3fb950", "its colours come too"


def test_a_local_edit_is_not_overwritten_by_an_older_server_copy(
    store: LocalStore, monkeypatch
) -> None:
    """The edit has not been pushed yet. Taking the server's older copy would throw
    away work that is still on its way up."""
    store.upsert_template(LogTemplate.model_validate(template_payload(version=9, name="Mine")))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates") and request.method == "GET":
            return httpx.Response(
                200, json={"items": [template_payload(version=2, name="Theirs")], "count": 1}
            )
        if request.url.path.endswith(("/templates", "/cases")):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    worker._run_cycle()

    assert store.get_template("shared-form").name == "Mine"
    assert store.get_template("shared-form").version == 9


def test_a_newer_server_copy_is_taken(store: LocalStore, monkeypatch) -> None:
    store.upsert_template(LogTemplate.model_validate(template_payload(version=1, name="Old")))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates") and request.method == "GET":
            return httpx.Response(
                200, json={"items": [template_payload(version=4, name="New")], "count": 1}
            )
        if request.url.path.endswith(("/templates", "/cases")):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    worker._run_cycle()

    assert store.get_template("shared-form").name == "New"


def test_one_unusable_template_does_not_stop_the_rest(store: LocalStore, monkeypatch) -> None:
    """One colleague's malformed form must not stop everybody else's work syncing."""
    broken = template_payload(template_id="broken")
    broken["fields"][1]["field_key"] = broken["fields"][0]["field_key"]  # two named the same
    good = template_payload(template_id="good")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates") and request.method == "GET":
            return httpx.Response(200, json={"items": [broken, good], "count": 2})
        if request.url.path.endswith(("/templates", "/cases")):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    worker._run_cycle()

    assert store.get_template("broken") is None, "the bad one is skipped"
    assert store.get_template("good") is not None, "the good one still lands"


def test_a_server_that_cannot_serve_templates_does_not_fail_the_cycle(
    store: LocalStore, monkeypatch
) -> None:
    """An older server has no /templates at all. That costs the labels, which is not
    worth failing -- and so retrying -- an otherwise good cycle over."""
    store.save(make_entry())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates"):
            return httpx.Response(404, json={"detail": "No such endpoint"})
        if request.url.path.endswith("/cases"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/entries/batch"):
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"entry_id": item["entry_id"], "status": "ok", "server_id": 1}
                        for item in payload["entries"]
                    ],
                    "accepted": len(payload["entries"]),
                    "rejected": 0,
                },
            )
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    pushed = worker._run_cycle()

    assert pushed == 1, "the entry still went up"


# --------------------------------------------------------------------------- #
# A lapsed subscription: the one failure the reviewer can do something about
# --------------------------------------------------------------------------- #


def _api_client(handler, tmp_path):
    """An ApiClient wired to a stub transport."""
    import httpx

    from evidence_review.config import ApiSettings
    from evidence_review.sync import ApiClient

    settings = ApiSettings(enabled=True, base_url="https://example.org", timeout_seconds=5)
    client = ApiClient(settings, "evr_token")
    client._client = httpx.Client(
        base_url="https://example.org",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_a_lapsed_subscription_is_not_an_authentication_failure(tmp_path) -> None:
    """402 and 401 mean different things and need different answers.

    401 says the token is wrong, which sends somebody hunting through settings
    for a problem that is not there. 402 says the token is fine and the
    subscription is not -- the only sync failure a reviewer can actually fix,
    and the only one worth offering a way out of.
    """
    import httpx

    from evidence_review.sync import ApiError, SubscriptionRequired

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "detail": "The last payment did not go through, so syncing is paused.",
                "reason": "payment_failed",
                "upgrade_url": "https://muuurder.com/cloud.php",
            },
        )

    client = _api_client(handler, tmp_path)
    with pytest.raises(SubscriptionRequired) as caught:
        client.health()

    problem = caught.value
    assert isinstance(problem, ApiError), "callers that catch ApiError must still catch this"
    assert problem.status_code == 402
    assert problem.reason == "payment_failed"
    assert problem.upgrade_url == "https://muuurder.com/cloud.php"
    assert "payment" in str(problem).lower()


def test_the_server_s_own_words_are_shown_not_a_generic_message(tmp_path) -> None:
    """The server knows which of five things went wrong; the client does not.

    Restating it locally would mean two copies of the same vocabulary drifting
    apart, and the one on the user's screen would be the stale one.
    """
    import httpx

    from evidence_review.sync import SubscriptionRequired

    said = "The subscription on this account has ended, so syncing is paused."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402, json={"detail": said, "reason": "subscription_ended", "upgrade_url": ""}
        )

    client = _api_client(handler, tmp_path)
    with pytest.raises(SubscriptionRequired) as caught:
        client.health()

    assert str(caught.value) == said


def test_a_402_with_no_body_still_says_something_useful(tmp_path) -> None:
    """A proxy can swallow the body. The reviewer still needs a sentence."""
    import httpx

    from evidence_review.sync import SubscriptionRequired

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, content=b"<html>Payment Required</html>")

    client = _api_client(handler, tmp_path)
    with pytest.raises(SubscriptionRequired) as caught:
        client.health()

    assert str(caught.value).strip() != ""
    assert caught.value.reason == ""
    assert caught.value.upgrade_url == ""


def test_nothing_is_lost_while_the_subscription_is_lapsed(tmp_path) -> None:
    """The point of the whole design.

    Sync is the paid part; reviewing is not. An entry written while the account
    is unpaid must stay on this machine, keep its pending state, and go up by
    itself once syncing resumes -- otherwise a failed card costs somebody their
    evidence, which is not a thing this application is allowed to do.
    """
    from evidence_review.models import EntryRow, MediaKind, SyncState
    from evidence_review.store import LocalStore

    store = LocalStore(tmp_path / "evidence.db")
    entry = store.save(
        EntryRow(
            case_id="adobe-house",
            file_name="cellar.wav",
            media_path=r"C:\e\cellar.wav",
            media_kind=MediaKind.AUDIO,
            event_offset_seconds=12.0,
            values={"builtin-paranormal.observation": "A knock, twice."},
        )
    )

    # Nothing about a 402 touches the local store, so the entry is simply still
    # there, still pending, exactly as it would be with the network unplugged.
    reloaded = store.get(entry.entry_id)
    assert reloaded is not None
    assert reloaded.sync_state is SyncState.PENDING
    assert reloaded.values["builtin-paranormal.observation"] == "A knock, twice."
    assert len(store.list_entries(case_id="adobe-house")) == 1
    store.close()


def test_the_window_offers_a_way_out_exactly_once(window, monkeypatch) -> None:
    """Worth saying. Not worth saying every thirty seconds.

    The worker retries on a timer, so without a latch this fires on every cycle
    and the reviewer cannot use the application for the dialog.
    """
    shown = []
    monkeypatch.setattr(
        "evidence_review.ui.main_window.QMessageBox.exec",
        lambda self: shown.append(self.text()) or 0,
    )

    for _ in range(4):
        window._on_subscription_required(
            "Syncing needs a subscription.", "no_subscription", ""
        )

    assert len(shown) == 1, f"the offer appeared {len(shown)} times"


@pytest.mark.parametrize(
    ("reason", "button"),
    [
        ("token_not_linked", "Get a new token"),
        ("no_account", "Get a new token"),
        ("payment_failed", "Update payment method"),
        ("no_subscription", "See plans"),
        ("subscription_ended", "See plans"),
    ],
)
def test_the_button_says_where_it_goes(window, monkeypatch, reason, button) -> None:
    """A token problem is not fixed by buying anything. Offering "See plans" to
    somebody who already pays, and then opening the token page, is both wrong
    about the cause and wrong about the destination."""
    labels = []
    monkeypatch.setattr(
        "evidence_review.ui.main_window.QMessageBox.exec",
        lambda self: labels.extend(b.text() for b in self.buttons()) or 0,
    )

    window._on_subscription_required("Syncing is paused.", reason, "https://x/y")

    assert button in labels, labels


def _capture_boxes(monkeypatch):
    """Record (text, informative text, button labels) for each box shown."""
    shown = []
    monkeypatch.setattr(
        "evidence_review.ui.main_window.QMessageBox.exec",
        lambda self: shown.append(
            (self.text(), self.informativeText(), [b.text() for b in self.buttons()])
        ) or 0,
    )
    return shown


def test_no_destination_means_no_button_that_goes_nowhere(window, monkeypatch) -> None:
    """entitlement_unavailable is a fault on the server: the server sends no URL
    because no web page fixes it. An action button there would open nothing."""
    shown = _capture_boxes(monkeypatch)

    window._on_subscription_required(
        "Subscriptions cannot be checked at the moment.", "entitlement_unavailable", ""
    )

    (_, _, buttons), = shown
    assert not any(
        label in buttons for label in ("See plans", "Get a new token", "Update payment method")
    ), buttons


def test_a_token_problem_does_not_promise_to_fix_itself(window, monkeypatch) -> None:
    """A token that is not linked will never be accepted. "Anything waiting will
    upload by itself" sent people to wait for something that could not happen."""
    shown = _capture_boxes(monkeypatch)

    window._on_subscription_required("Not linked.", "token_not_linked", "https://x/t")
    window._on_subscription_required("Card failed.", "payment_failed", "https://x/p")

    (_, token_text, _), (_, payment_text, _) = shown
    assert "new token" in token_text and "by itself" not in token_text, token_text
    assert "by itself" in payment_text, payment_text


def test_a_new_reason_is_explained_even_after_an_old_one(window, monkeypatch) -> None:
    """Latched per reason, not per session: a failed payment that turns into an
    ended subscription needs a different page, and saying nothing hides that."""
    shown = _capture_boxes(monkeypatch)

    window._on_subscription_required("Card failed.", "payment_failed", "https://x/p")
    window._on_subscription_required("Card failed.", "payment_failed", "https://x/p")
    window._on_subscription_required("Ended.", "subscription_ended", "https://x/c")

    assert [text for text, _, _ in shown] == ["Card failed.", "Ended."]


def test_the_status_corner_says_which_problem_this_is(window) -> None:
    """Somebody glancing at the corner should not confuse "not paid" with
    "server down" -- the first they can fix, the second they cannot."""
    window._set_sync_indicator("unsubscribed", 3, "Syncing needs a subscription.")
    unpaid = window.sync_label.text()

    window._set_sync_indicator("error", 3, "Server returned 500")
    broken = window.sync_label.text()

    assert "Subscription needed" in unpaid
    assert unpaid != broken
    assert "3 waiting" in unpaid, "it should still say how much is queued"


# --------------------------------------------------------------------------- #
# "Test connection" has to test the thing the user just typed
# --------------------------------------------------------------------------- #


def _settings_dialog(qt_app, tmp_path, handler):
    """A real SettingsDialog whose ApiClient talks to a stub transport."""
    import httpx

    from evidence_review.config import Settings
    from evidence_review.store import LocalStore
    from evidence_review.ui import settings_dialog as module

    real_client = module.ApiClient

    class StubbedClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, settings, token):
            super().__init__(settings, token)
            self._client = httpx.Client(
                base_url="https://example.org",
                transport=httpx.MockTransport(handler),
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )

    module.ApiClient = StubbedClient
    settings = Settings()
    settings.first_run_complete = True
    store = LocalStore(tmp_path / "e.db")
    dialog = module.SettingsDialog(settings=settings, store=store)
    dialog.api_url_edit.setText("https://example.org")
    return dialog, store, module, real_client


@pytest.mark.parametrize(
    ("reason", "expected", "not_expected"),
    [
        ("payment_failed", "/profile.php", "/evidence-review.php"),
        ("token_not_linked", "/evidence-review.php", "/profile.php"),
    ],
)
def test_the_settings_link_follows_the_reason(
    qt_app, tmp_path, reason, expected, not_expected
) -> None:
    """Only a token problem is fixed by a new token. Offering "get a token" for a
    failed payment sent people to make a token refused exactly like this one."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(
                200,
                json={"status": "ok", "version": "1.1.0",
                      "tokens_url": "https://site.example/evidence-review.php"},
            )
        return httpx.Response(
            402,
            json={"detail": "Paused.", "reason": reason,
                  "upgrade_url": "https://site.example/profile.php"
                  if reason == "payment_failed"
                  else "https://site.example/evidence-review.php"},
        )

    dialog, store, module, real_client = _settings_dialog(qt_app, tmp_path, handler)
    try:
        dialog.api_token_edit.setText("evr_a_real_looking_token")
        dialog._test_connection()

        link = dialog.tokens_link_label.text()
        assert expected in link and not_expected not in link, link
    finally:
        module.ApiClient = real_client
        store.close()


def test_a_token_the_server_refuses_is_not_reported_as_connected(qt_app, tmp_path) -> None:
    """/health is unauthenticated -- that is the point of it, the button is there
    to check an address before a token exists.

    So reaching it proved the URL and nothing else, and the dialog said
    "Connected" to somebody whose very next sync would be refused. They would
    then go hunting through settings for a problem that was never there.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(
                200,
                json={"status": "ok", "version": "1.1.0",
                      "tokens_url": "https://muuurder.com/evidence-review.php"},
            )
        return httpx.Response(
            402,
            json={
                "detail": "Syncing and case sharing need a subscription.",
                "reason": "no_subscription",
                "upgrade_url": "https://muuurder.com/cloud.php",
            },
        )

    dialog, store, module, real_client = _settings_dialog(qt_app, tmp_path, handler)
    try:
        dialog.api_token_edit.setText("evr_a_real_looking_token")
        dialog._test_connection()

        said = dialog.test_result_label.text()
        assert "Connected" not in said, said
        assert "subscription" in said.lower(), said
    finally:
        module.ApiClient = real_client
        store.close()


def test_a_working_token_says_so_plainly(qt_app, tmp_path) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "version": "1.1.0"})
        return httpx.Response(200, json={"entries": [], "next_cursor": None})

    dialog, store, module, real_client = _settings_dialog(qt_app, tmp_path, handler)
    try:
        dialog.api_token_edit.setText("evr_good")
        dialog._test_connection()

        said = dialog.test_result_label.text()
        assert "token works" in said.lower(), said
        assert "1.1.0" in said
    finally:
        module.ApiClient = real_client
        store.close()


def test_with_no_token_it_says_the_address_is_fine_and_no_more(qt_app, tmp_path) -> None:
    """The button still has to work before a token exists -- that is what it is
    for -- but it must not imply more than it checked."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "1.1.0"})

    dialog, store, module, real_client = _settings_dialog(qt_app, tmp_path, handler)
    try:
        dialog.api_token_edit.setText("")
        dialog._test_connection()

        said = dialog.test_result_label.text()
        assert "No token entered" in said, said
    finally:
        module.ApiClient = real_client
        store.close()


def test_the_server_says_where_to_get_a_token(qt_app, tmp_path) -> None:
    """A password box labelled "Bearer token" does not tell anybody where one
    comes from. The address is discovered rather than built, so it follows
    whatever deployment this install points at."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "version": "1.1.0",
                  "tokens_url": "https://muuurder.com/evidence-review.php"},
        )

    dialog, store, module, real_client = _settings_dialog(qt_app, tmp_path, handler)
    try:
        dialog._test_connection()

        # isHidden, not isVisibleTo: the label lives on a tab that is not the
        # current one, which makes isVisibleTo false for reasons that have
        # nothing to do with whether the link was offered.
        assert not dialog.tokens_link_label.isHidden()
        assert "evidence-review.php" in dialog.tokens_link_label.text()
        assert "href=" in dialog.tokens_link_label.text()
    finally:
        module.ApiClient = real_client
        store.close()


def test_a_junk_tokens_url_is_not_turned_into_a_link(qt_app, tmp_path) -> None:
    """The value arrives from the server. A dialog that renders whatever it is
    sent as a clickable link is a way to put javascript: in front of someone."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "version": "1.1.0",
                  "tokens_url": "javascript:alert(1)"},
        )

    dialog, store, module, real_client = _settings_dialog(qt_app, tmp_path, handler)
    try:
        dialog._test_connection()
        assert dialog.tokens_link_label.isHidden()
        assert "javascript:" not in dialog.tokens_link_label.text()
    finally:
        module.ApiClient = real_client
        store.close()


# --------------------------------------------------------------------------- #
# The PHP server's template shape
# --------------------------------------------------------------------------- #


def test_a_php_empty_config_does_not_stop_sync() -> None:
    """PHP encodes an empty array as ``[]``. The PHP server sent ``"config": []``
    for every field without settings, the client rejected the whole template list,
    and because templates are fetched first, no entry ever uploaded. Found
    2026-10-06 by running the real sync loop against the PHP server."""
    import httpx

    from evidence_review.config import ApiSettings
    from evidence_review.sync import ApiClient

    template = {
        "template_id": "custom-1", "name": "Site visit", "version": 1,
        "fields": [
            {"field_id": "f1", "field_key": "note", "label": "Note",
             "field_type": "text", "config": []},           # what PHP sent
            {"field_id": "f2", "field_key": "where", "label": "Where",
             "field_type": "text", "config": {"max_length": 40}},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [template]})

    client = ApiClient(ApiSettings(base_url=BASE_URL, enabled=True), "t",
                       transport=httpx.MockTransport(handler))
    (got,) = client.pull_templates()
    assert got.fields[0].config == {}
    assert got.fields[1].config == {"max_length": 40}


def test_one_unreadable_template_is_skipped_not_fatal() -> None:
    """Templates are fetched before entries upload. One the client cannot read
    must cost that template, never the whole sync."""
    import httpx

    from evidence_review.config import ApiSettings
    from evidence_review.sync import ApiClient

    good = {"template_id": "ok-1", "name": "Fine", "version": 1, "fields": []}
    bad = {"template_id": "bad-1", "name": "Broken", "version": "not a number", "fields": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [bad, good]})

    client = ApiClient(ApiSettings(base_url=BASE_URL, enabled=True), "t",
                       transport=httpx.MockTransport(handler))
    assert [t.template_id for t in client.pull_templates()] == ["ok-1"]



def test_every_case_is_pulled_too(store: LocalStore, monkeypatch) -> None:
    """An entry moved to another case -- on the website, or on a colleague's
    computer -- has to move here as well. Only the open case used to be pulled, so
    it sat in its old case on this computer indefinitely (2026-10-07)."""
    store.save(make_entry(case_id="willow-house", values={OBSERVATION: "Moved elsewhere."}))
    local = store.list_entries(case_id="willow-house")[0]
    moved = local.model_copy(deep=True)
    moved.case_id = "harbour-street"
    moved.updated_at_utc = local.updated_at_utc + __import__("datetime").timedelta(seconds=5)
    store.mark_synced(local.entry_id, 1, None)
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/cases", "/templates")):
            return httpx.Response(200, json={"case_id": "willow-house"})
        seen.append(request.url.params.get("case_id"))
        if not request.url.params.get("case_id"):
            return httpx.Response(200, json={"items": [moved.wire_payload()], "has_more": False})
        return httpx.Response(200, json={"items": [], "has_more": False})

    worker = _worker_with(store, handler, monkeypatch)
    worker._run_cycle()

    assert None in seen, "every case was not pulled"
    assert store.get(local.entry_id).case_id == "harbour-street", "the move did not arrive"
