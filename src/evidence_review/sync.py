"""Talking to the shared logging server.

``ApiClient`` is a plain httpx wrapper with no Qt dependency, so it can be tested
against a mock transport. ``SyncWorker`` drives it from a background thread and
reports progress back to the UI through Qt signals.

The contract that matters: entries are already safely in local SQLite before this
module ever runs. Everything here is best-effort, retried, and idempotent.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError
from PySide6.QtCore import QThread, Signal

from .config import ApiSettings
from .models import Case, EntryRow, LogEntry
from .store import LocalStore, TemplateError
from .templates import LogTemplate
from .version import APP_VERSION

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


class ApiError(RuntimeError):
    """Any failure talking to the server."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SubscriptionRequired(ApiError):
    """The token is fine; the account behind it is not paying.

    Kept apart from every other failure because it is the only one the reviewer
    can actually do something about, and because the answer is never "retry".
    A lapsed subscription looks exactly like a server fault if you only have a
    status code to go on -- hence 402 from the server and this class here, so
    the window can offer the way out instead of showing "sync failed" for ever.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "",
        upgrade_url: str = "",
    ) -> None:
        super().__init__(message, status_code=402)
        #: A short machine-readable cause: no_subscription, payment_failed,
        #: subscription_ended, token_not_linked, no_account.
        self.reason = reason
        #: Where to send somebody who wants to fix it. May be empty.
        self.upgrade_url = upgrade_url


@dataclass(frozen=True, slots=True)
class PullPage:
    """A page of remote entries and the opaque cursor that follows it."""

    entries: list[LogEntry]
    next_cursor: str | None = None
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class PushResult:
    entry_id: str
    ok: bool
    server_id: int | None = None
    message: str = ""


class ApiClient:
    """Synchronous REST client for the evidence log API."""

    def __init__(
        self,
        settings: ApiSettings,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.token = token
        headers = {
            "User-Agent": f"EvidenceReview/{APP_VERSION}",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=f"{settings.base_url}{API_PREFIX}",
            headers=headers,
            timeout=settings.timeout_seconds,
            verify=settings.verify_tls,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
        except httpx.TimeoutException as exc:
            raise ApiError(f"Timed out after {self.settings.timeout_seconds:g}s") from exc
        except httpx.TransportError as exc:
            raise ApiError(f"Cannot reach server: {exc}") from exc

        if response.status_code == 401:
            raise ApiError("Authentication failed - check the API token", status_code=401)
        if response.status_code == 403:
            raise ApiError("This token is not permitted to write entries", status_code=403)
        if response.status_code == 402:
            # The server distinguishes "your token is wrong" (401) from "your
            # subscription lapsed" (402) precisely so this case can be answered
            # with an offer rather than an error nobody can act on.
            payload: dict = {}
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = {}
            raise SubscriptionRequired(
                str(payload.get("detail") or "A subscription is needed to sync."),
                reason=str(payload.get("reason") or ""),
                upgrade_url=str(payload.get("upgrade_url") or ""),
            )
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = payload.get("detail") or payload.get("message") or ""
            except Exception:  # noqa: BLE001
                detail = response.text[:200]
            raise ApiError(
                f"Server returned {response.status_code}: {detail}".strip(),
                status_code=response.status_code,
            )
        return response

    # -- endpoints ---------------------------------------------------------- #

    def health(self) -> dict:
        """Liveness probe. Used by the first-run wizard and Settings."""
        return self._request("GET", "/health").json()

    def push(self, entry: EntryRow | LogEntry) -> PushResult:
        payload = (
            entry.wire_payload() if isinstance(entry, EntryRow) else entry.model_dump(mode="json")
        )
        data = self._request("POST", "/entries", json=payload).json()
        return PushResult(
            entry_id=data.get("entry_id", payload["entry_id"]),
            ok=True,
            server_id=data.get("server_id"),
        )

    def push_case(self, case: Case) -> None:
        """Register the case, so the server knows its name and not just its id.

        Only the id travels on an entry, so without this the server has nothing to
        show but the slug, and a case with no entries yet does not exist remotely
        at all. Idempotent: it is a rename if the case is already there.
        """
        self._request(
            "POST",
            "/cases",
            json={
                "case_id": case.case_id,
                "name": case.name,
                "root_path": case.root_path,
                "template_id": case.template_id,
            },
        )

    def push_template(self, template: LogTemplate) -> None:
        """Register a template, so the server can interpret the answers that cite it.

        Whole, in one request: fields, options and rules together, because that is how
        it is edited and the only way a half-applied change cannot be observed.
        Idempotent on ``template_id``, exactly like an entry.
        """
        self._request("POST", "/templates", json=template.model_dump(mode="json"))

    def pull_templates(self) -> list[LogTemplate]:
        """Templates the server knows, so a colleague's form can be rendered here."""
        response = self._request("GET", "/templates")
        body = response.json()
        # One by one: a single template this client cannot read is skipped, not
        # allowed to fail the list -- and with it the sync cycle, which fetches
        # templates before it uploads anything.
        templates: list[LogTemplate] = []
        for item in body.get("items", []):
            try:
                templates.append(LogTemplate.model_validate(item))
            except ValidationError as exc:
                log.warning(
                    "Skipped template %s from the server: %s",
                    (item or {}).get("template_id", "?") if isinstance(item, dict) else "?",
                    exc.errors()[0].get("msg", exc) if exc.errors() else exc,
                )
        return templates

    def push_batch(self, entries: Sequence[EntryRow]) -> list[PushResult]:
        """Upsert many entries in one call. Idempotent on ``entry_id``."""
        if not entries:
            return []
        payload = {"entries": [entry.wire_payload() for entry in entries]}
        data = self._request("POST", "/entries/batch", json=payload).json()
        results: list[PushResult] = []
        for item in data.get("results", []):
            results.append(
                PushResult(
                    entry_id=item["entry_id"],
                    ok=item.get("status") == "ok",
                    server_id=item.get("server_id"),
                    message=item.get("message", ""),
                )
            )
        return results

    def pull_page(
        self,
        *,
        case_id: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> PullPage:
        """One page of remote entries, plus the cursor to resume from."""
        params: dict[str, object] = {"limit": limit, "include_deleted": True}
        if case_id:
            params["case_id"] = case_id
        if cursor:
            params["cursor"] = cursor
        elif since:
            params["since"] = since

        data = self._request("GET", "/entries", params=params).json()
        entries: list[LogEntry] = []
        for item in data.get("items", []):
            try:
                entries.append(LogEntry.model_validate(item))
            except Exception:  # noqa: BLE001 - skip anything we cannot parse
                log.warning("Skipping unparseable remote entry %s", item.get("entry_id"))
        return PullPage(
            entries=entries,
            next_cursor=data.get("next_cursor"),
            has_more=bool(data.get("has_more")),
        )

    def pull_all(
        self,
        *,
        case_id: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        limit: int = 500,
        max_pages: int = 200,
    ) -> PullPage:
        """Follow the cursor to the end, so a large backlog arrives in one cycle.

        Returns every entry gathered and the cursor positioned after the last one,
        which the caller stores as its watermark for the next incremental pull.
        """
        collected: list[LogEntry] = []
        resume = cursor
        pages = 0

        while pages < max_pages:
            page = self.pull_page(
                case_id=case_id, since=None if resume else since, cursor=resume, limit=limit
            )
            collected.extend(page.entries)
            if page.next_cursor:
                resume = page.next_cursor
            pages += 1
            if not page.has_more:
                break
        else:
            log.warning("Stopped pulling after %d pages; more remain", max_pages)

        return PullPage(entries=collected, next_cursor=resume, has_more=False)

    def upload_snapshot(self, entry_id: str, image_path: str | Path) -> bool:
        path = Path(image_path)
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            files = {"file": (path.name, handle, "image/png")}
            self._request("POST", f"/entries/{entry_id}/snapshot", files=files)
        return True


# --------------------------------------------------------------------------- #
# Background worker
# --------------------------------------------------------------------------- #

MIN_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 300.0


def backoff_delay(consecutive_failures: int, base_interval: float) -> float:
    """Exponential backoff with jitter, clamped to five minutes."""
    if consecutive_failures <= 0:
        return base_interval
    raw = MIN_BACKOFF_SECONDS * (2 ** (consecutive_failures - 1))
    capped = min(raw, MAX_BACKOFF_SECONDS)
    return capped * (0.8 + random.random() * 0.4)  # jitter, not crypto


class SyncWorker(QThread):
    """Drains pending local entries to the server and merges remote changes back."""

    #: (state, pending_count, message) where state is
    #: idle|syncing|offline|error|disabled|unsubscribed
    status_changed = Signal(str, int, str)
    #: Emitted after remote entries are merged into the local store.
    remote_merged = Signal(int)
    #: (message, reason, upgrade_url) when the account behind the token is not
    #: paying. Separate from status_changed because this one is an offer, not a
    #: status: the window answers it with a way to fix it rather than a colour.
    subscription_required = Signal(str, str, str)

    def __init__(
        self,
        store: LocalStore,
        settings: ApiSettings,
        token: str,
        *,
        case_id: str = "default",
        upload_snapshots: bool = True,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._settings = settings
        self._token = token
        self._case_id = case_id
        self._upload_snapshots = upload_snapshots
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._failures = 0
        # Whether this client receives other investigators' entries at all. When it
        # does, it pulls on *every* cycle: the cursor makes that incremental, so a
        # steady-state pull is one request that comes back empty.
        self._pull_enabled = settings.pull_on_start
        # Registered once per worker, and the worker is rebuilt on every case
        # switch, so switching to a case the server has never heard of announces it.
        self._case_registered = False

    # -- control ------------------------------------------------------------ #

    def request_sync(self) -> None:
        """Ask the worker to run a cycle now instead of waiting for the timer.

        A cycle pulls as well as pushes, so this is all "Sync now" needs.
        """
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- main loop ---------------------------------------------------------- #

    def run(self) -> None:  # QThread entry point
        if not self._settings.is_configured:
            self.status_changed.emit("disabled", self._safe_pending(), "Remote sync is off")
            return

        while not self._stop.is_set():
            delay = self._settings.sync_interval_seconds
            try:
                moved = self._run_cycle()
                self._failures = 0
                pending = self._safe_pending()
                if pending:
                    self.status_changed.emit("idle", pending, f"{pending} waiting to upload")
                else:
                    message = (
                        f"Synced {moved} entr{'y' if moved == 1 else 'ies'}"
                        if moved
                        else "Up to date"
                    )
                    self.status_changed.emit("idle", 0, message)
            except SubscriptionRequired as exc:
                # Not a failure to retry out of. Nothing about waiting longer
                # makes a lapsed subscription come back, and hammering the
                # server every thirty seconds to be told so again is rude to
                # both ends. Back off to the longest interval and say plainly
                # what has happened; a successful sync resets it.
                self._failures = 0
                delay = max(self._settings.sync_interval_seconds, 300)
                self.status_changed.emit(
                    "unsubscribed", self._safe_pending(), str(exc)
                )
                self.subscription_required.emit(
                    str(exc), exc.reason, exc.upgrade_url
                )
                log.info("Sync paused: %s (%s)", exc, exc.reason)
            except ApiError as exc:
                self._failures += 1
                delay = backoff_delay(self._failures, self._settings.sync_interval_seconds)
                state = "offline" if exc.status_code is None else "error"
                self.status_changed.emit(state, self._safe_pending(), str(exc))
                log.warning("Sync cycle failed (attempt %d): %s", self._failures, exc)
            except Exception as exc:
                self._failures += 1
                delay = backoff_delay(self._failures, self._settings.sync_interval_seconds)
                self.status_changed.emit("error", self._safe_pending(), str(exc))
                log.exception("Unexpected sync failure")

            self._wake.wait(timeout=delay)
            self._wake.clear()

        self._store.close()

    # -- one cycle ---------------------------------------------------------- #

    def _run_cycle(self) -> int:
        pending = self._store.list_pending(self._settings.batch_size)
        pushed = 0

        if not pending and not self._pull_enabled and self._case_registered:
            # Nothing to do: don't open a connection just to close it again.
            return 0

        with ApiClient(self._settings, self._token) as client:
            if not self._case_registered:
                case = self._store.get_case(self._case_id)
                if case is not None:
                    try:
                        client.push_case(case)
                    except ApiError as exc:
                        # An older server has no POST /cases. That costs the case
                        # its name on the server, which is not worth failing - and
                        # so retrying - an otherwise good cycle over.
                        log.info("Could not register the case (%s); continuing", exc)
                # The templates go up with the case. An entry's answers are keyed by
                # field_id, so a server that has not been told the template can store
                # them but cannot label, validate or report on them.
                for template in self._store.list_templates():
                    try:
                        client.push_template(template)
                    except ApiError as exc:
                        log.info("Could not register template %s (%s)", template.name, exc)

                # And a colleague's forms come back the other way. Without this an
                # entry written on somebody else's template arrived as answers keyed
                # by field ids this client had never seen: stored faithfully, but
                # shown under raw ids because nothing here knew what they were asked.
                self._pull_templates(client)
                self._case_registered = True

            if pending:
                self.status_changed.emit("syncing", len(pending), f"Uploading {len(pending)}...")
                # Keep the snapshot we uploaded, so success can be attributed to
                # exactly that version and not to a newer local edit.
                uploaded = {entry.entry_id: entry.local_revision for entry in pending}
                for result in client.push_batch(pending):
                    if result.ok:
                        if self._store.mark_synced(
                            result.entry_id,
                            result.server_id,
                            uploaded.get(result.entry_id),
                        ):
                            pushed += 1
                    else:
                        self._store.mark_sync_error(result.entry_id, result.message or "rejected")

                if self._upload_snapshots:
                    self._push_snapshots(client, pending)

            if self._pull_enabled:
                # Every cycle, not once. This used to run one time per worker, so a
                # teammate's entry only ever arrived if you restarted the app or
                # switched case, while the status bar said "Up to date" regardless.
                #
                # The cursor lives with the case, not with this worker, so switching
                # away and back resumes instead of re-pulling from the beginning,
                # and a steady-state pull is a single request that returns nothing.
                cursor = self._store.get_pull_cursor(self._case_id)
                page = client.pull_all(case_id=self._case_id, cursor=cursor)
                merged = self._store.merge_remote(page.entries)
                if page.next_cursor:
                    # An opaque (updated_at, id) cursor, so resuming can neither
                    # skip an entry nor replay the whole table.
                    self._store.set_pull_cursor(self._case_id, page.next_cursor)
                if merged:
                    self.remote_merged.emit(merged)

        return pushed

    def _pull_templates(self, client: ApiClient) -> int:
        """Store the forms the server knows about. Returns how many were taken.

        A template that will not store -- two fields sharing a name, say -- is logged
        and skipped rather than allowed to end the cycle. One malformed form from one
        colleague must not stop everybody else's entries syncing.
        """
        try:
            remote = client.pull_templates()
        except ApiError as exc:
            log.info("Could not fetch templates (%s); continuing", exc)
            return 0

        taken = 0
        for template in remote:
            existing = self._store.get_template(template.template_id)
            # Last edit wins on version, the same rule the server applies. A local
            # edit that has not gone up yet is not overwritten by the older copy
            # still sitting on the server.
            if existing is not None and existing.version >= template.version:
                continue
            try:
                self._store.upsert_template(template)
            except (TemplateError, sqlite3.Error) as exc:
                # Narrow on purpose: a template that is malformed or that the schema
                # rejects is somebody else's mistake and gets skipped, but a bug in
                # this code should still surface rather than be swallowed per-item.
                log.warning("Skipped template %s from the server: %s", template.template_id, exc)
                continue
            taken += 1
        if taken:
            log.info("Took %d template(s) from the server", taken)
        return taken

    def _push_snapshots(self, client: ApiClient, entries: Sequence[EntryRow]) -> None:
        for entry in entries:
            if not entry.snapshot_path:
                continue
            try:
                client.upload_snapshot(entry.entry_id, entry.snapshot_path)
            except ApiError:
                # A missing snapshot must never block the entry itself.
                log.debug("Snapshot upload failed for %s", entry.entry_id, exc_info=True)

    def _safe_pending(self) -> int:
        try:
            return self._store.pending_count()
        except Exception:  # noqa: BLE001
            return 0
