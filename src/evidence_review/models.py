"""The shared data contract between the desktop client and the server.

``LogEntry`` is what travels over the wire. ``EntryRow`` is ``LogEntry`` plus the
local-only sync bookkeeping columns, and is what the local store hands back.

An entry has two halves. The fixed one is here: which file, where in it, who
looked and when -- everything the application cannot work without. The other half
is whatever the case's template asks, carried in ``values`` and keyed by
``field_id``. See :mod:`evidence_review.templates`.
"""

from __future__ import annotations

import datetime as _dt
import enum
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .annotations import Annotation
from .builtin_templates import DEFAULT_TEMPLATE_ID
from .templates import FieldRole, LogTemplate
from .util import ensure_aware


class MediaKind(str, enum.Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"


class SyncState(str, enum.Enum):
    PENDING = "pending"
    SYNCED = "synced"
    ERROR = "error"
    CONFLICT = "conflict"


DEFAULT_STATUSES: tuple[str, ...] = (
    "Needs Review",
    "Unexplained",
    "Debunked",
    "Corroborated",
    "Inconclusive",
    "Equipment Artifact",
    "Contamination",
)

DEBUNKED_STATUS = "Debunked"

#: Server column limits, mirrored here so an over-long value is caught in the LOG
#: modal with a readable message instead of being rejected by the API later. An
#: entry the server refuses stays queued forever and blocks nothing else, but the
#: reviewer would have no idea why it never uploaded.
MAX_LENGTHS: dict[str, int] = {
    "file_name": 512,
    "case_id": 120,
    "investigator_name": 200,
    "media_sha256": 64,
    "app_version": 32,
    "machine_name": 120,
    "template_id": 64,
}

#: Limits on the legacy projection columns, which are narrower than the template
#: fields that feed them -- a template may allow a 500-character location where the
#: old ``area`` column holds 200.
#:
#: The projection is truncated to fit rather than rejected: it is compatibility
#: data, and refusing to save a valid entry because a column that is on its way out
#: cannot hold its shadow would be absurd. The real answer in ``entry_values`` is
#: never truncated.
LEGACY_LIMITS: dict[str, int] = {"area": 200, "status": 64}

#: Field names as the reviewer sees them on the form.
_FIELD_LABELS: dict[str, str] = {
    "file_name": "File Name",
    "case_id": "Case ID",
    "area": "Area",
    "status": "Status",
    "investigator_name": "Investigator Name",
}

#: The four columns that were the form before templates existed, mapped to the role
#: whose field now answers them.
#:
#: They survive as *database* columns only, written from ``values`` on every save
#: and never read, so the PHP web log and a rollback to 1.1.0 keep working. They are
#: deliberately **not** fields on ``LogEntry``: an attribute that silently discards
#: what you assign to it is a trap, and keeping them off the model means any code
#: still reaching for ``entry.observation`` fails loudly instead of writing into a
#: value that save() is about to overwrite.
#:
#: Driven by role rather than by name, so an entry on any template -- including one
#: a reviewer invents -- still projects sensibly. Dropped once the PHP web log reads
#: ``values``.
LEGACY_ROLE_COLUMNS: dict[str, FieldRole] = {
    "area": FieldRole.LOCATION,
    "observation": FieldRole.SUMMARY,
    "debunk_reasoning": FieldRole.DETAIL,
    "status": FieldRole.STATUS,
}


def new_entry_id() -> str:
    """Client-generated primary key. This is what makes sync idempotent."""
    return str(uuid.uuid4())


class LogEntry(BaseModel):
    """One logged observation. Field order matches the review form."""

    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    entry_id: str = Field(default_factory=new_entry_id)
    case_id: str = "default"

    #: Which form this entry was written with. On the entry rather than only on the
    #: case, because a case's template can change and an entry already written must
    #: keep the shape it was written in.
    template_id: str = DEFAULT_TEMPLATE_ID
    template_version: int = 1

    #: The template's half of the entry, keyed by ``field_id``.
    #:
    #: A key this client does not recognise is kept, not dropped: it is a
    #: colleague's answer on a template version not pulled yet, and discarding it
    #: would destroy their work on the next save.
    values: dict[str, Any] = Field(default_factory=dict)

    #: Marks drawn over the picture, carried with the entry so they reach colleagues.
    #:
    #: Tombstones travel too. Marks are merged by ``annotation_id`` rather than
    #: replaced wholesale, so two investigators annotating the same entry at the same
    #: time keep both sets -- which is a far more likely thing to happen than two
    #: people editing one observation. A removal has to arrive as a tombstone for
    #: that to work: if it arrived as an absence it would be indistinguishable from a
    #: mark the other client has not heard about yet, and merging would quietly
    #: resurrect everything anybody ever deleted.
    annotations: list[Annotation] = Field(default_factory=list)

    #: The audio cleanup that was switched on when this entry was written, as an
    #: ffmpeg filter specification -- or "" for the recording as it was given.
    #:
    #: An observation made while listening through 20 dB of noise reduction is a
    #: different claim from the same words written against the raw recording, because
    #: denoising pushed hard invents detail that can sound like speech. A record that
    #: does not say which was heard cannot be checked by anybody later, including the
    #: reviewer themselves. Stored as the specification rather than a description
    #: because it is exactly reproducible: anybody with ffmpeg can hear what was heard.
    audio_filters: str = ""

    # 1. File Name (plus provenance about the file it names)
    file_name: str
    media_path: str = ""
    media_sha256: str | None = None
    media_kind: MediaKind = MediaKind.VIDEO

    # 2. Event Timestamp -- where in the media the event starts, so the reviewer
    #    can jump straight back to it. Not a wall-clock time.
    event_offset_seconds: float | None = None

    # 3. Event Duration -- how long the event lasted.
    event_duration_seconds: float | None = None

    #: Total length of the media, kept for context in exports.
    media_duration_seconds: float | None = None

    # 4. Investigator Name
    investigator_name: str = ""

    # 5. Date Reviewed
    date_reviewed: _dt.date = Field(default_factory=_dt.date.today)

    # Provenance captured silently
    snapshot_path: str | None = None
    app_version: str | None = None
    machine_name: str | None = None
    created_at_utc: _dt.datetime = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    updated_at_utc: _dt.datetime = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    is_deleted: bool = False

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def _make_aware(cls, value: _dt.datetime | None) -> _dt.datetime | None:
        return ensure_aware(value)

    @field_validator("event_offset_seconds", "event_duration_seconds", "media_duration_seconds")
    @classmethod
    def _non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return max(0.0, float(value))

    @field_serializer("values")
    def _values_to_json(self, values: dict[str, Any]) -> dict[str, Any]:
        """Make the value map JSON-safe without flattening its types.

        Dates become ISO strings on the wire and are read back as dates, so a date
        field still compares as a date at both ends.
        """
        # datetime is a subclass of date, so one check covers both.
        return {
            field_id: value.isoformat() if isinstance(value, _dt.date) else value
            for field_id, value in values.items()
        }

    # -- derived helpers ---------------------------------------------------- #

    @property
    def has_duration(self) -> bool:
        return bool(self.event_duration_seconds)

    @property
    def event_end_seconds(self) -> float | None:
        """Where the event finishes, when a duration was recorded."""
        if self.event_offset_seconds is None or not self.event_duration_seconds:
            return None
        return self.event_offset_seconds + self.event_duration_seconds

    def summary(self, template: LogTemplate) -> str:
        """One line describing this entry, for the log table and marker tooltips."""
        return template.summarise(self.values)

    def value_for_role(self, template: LogTemplate, role: FieldRole) -> Any:
        """The answer to whichever field plays this role, or None."""
        field = template.field_for_role(role)
        return self.values.get(field.field_id) if field else None

    def validation_problems(self, template: LogTemplate) -> list[str]:
        """Human-readable reasons this entry is not ready to save.

        The universal half is checked here and the template's half by the template,
        which is the only thing that knows what it asked.

        ``template`` is required and has no default. Defaulting it to None would mean
        a caller that forgot it validated the reviewer's name and nothing else, and
        happily saved an entry with no observation at all.
        """
        problems: list[str] = []
        if not self.investigator_name.strip():
            problems.append("'Investigator Name' is required.")
        if (
            self.event_duration_seconds is not None
            and self.event_offset_seconds is not None
            and self.media_duration_seconds
            and self.event_offset_seconds + self.event_duration_seconds
            > self.media_duration_seconds + 1.0
        ):
            problems.append("The event runs past the end of the media.")

        for field, limit in MAX_LENGTHS.items():
            value = getattr(self, field, None)
            if isinstance(value, str) and len(value) > limit:
                label = _FIELD_LABELS.get(field, field)
                problems.append(f"'{label}' is {len(value)} characters; the maximum is {limit}.")

        problems.extend(template.validation_problems(self.values))
        return problems


class EntryRow(LogEntry):
    """A ``LogEntry`` as stored locally, carrying sync bookkeeping."""

    sync_state: SyncState = SyncState.PENDING
    sync_error: str | None = None
    synced_at_utc: _dt.datetime | None = None
    server_id: int | None = None

    #: Bumped by the store on every save. This is the optimistic-concurrency token
    #: the sync worker uses to prove the version it uploaded is still the current
    #: one. A wall-clock timestamp cannot do this job: Windows resolves
    #: datetime.now() to roughly 15 ms, so an edit made in the same tick as the
    #: upload snapshot carries an identical timestamp and would slip through.
    local_revision: int = 0

    #: Columns that exist only on the client and are never sent to the server.
    LOCAL_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"sync_state", "sync_error", "synced_at_utc", "server_id", "local_revision"}
    )

    def to_wire(self) -> LogEntry:
        return LogEntry.model_validate(self.model_dump(exclude=set(self.LOCAL_ONLY_FIELDS)))

    def wire_payload(self) -> dict:
        """JSON-ready dict for the API."""
        return self.to_wire().model_dump(mode="json")


class Case(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    case_id: str
    name: str
    root_path: str | None = None
    #: The template new entries in this case start on. None means the default.
    template_id: str | None = None
    created_at_utc: _dt.datetime = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
