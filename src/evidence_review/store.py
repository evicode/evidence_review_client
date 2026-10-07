"""Local SQLite store. Write-first, crash-safe, and the source of truth for the client.

Saving a log entry must never depend on the network, so every entry lands here
inside a transaction before the sync worker ever sees it. Connections are
thread-local and the database runs in WAL mode, which allows the sync worker to
read while the UI thread writes.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .annotations import Annotation, AnnotationKind
from .builtin_templates import BUILTIN_TEMPLATES, DEFAULT_TEMPLATE_ID
from .models import (
    LEGACY_LIMITS,
    LEGACY_ROLE_COLUMNS,
    Case,
    EntryRow,
    LogEntry,
    MediaKind,
    SyncState,
)
from .templates import (
    FieldOption,
    FieldRole,
    FieldType,
    LogTemplate,
    TemplateField,
    TemplateRule,
)
from .util import from_iso_date, from_iso_datetime, to_iso, utc_now

log = logging.getLogger(__name__)

#: Distinguishes "not cached" from "cached as None", which is what a template id that
#: does not exist is stored as. Without it a missing template is re-read every time.
_MISSING = object()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS log_entries (
                entry_id                     TEXT PRIMARY KEY,
                case_id                      TEXT NOT NULL DEFAULT 'default',
                file_name                    TEXT NOT NULL,
                media_path                   TEXT NOT NULL DEFAULT '',
                media_sha256                 TEXT,
                media_kind                   TEXT NOT NULL DEFAULT 'video',
                area                         TEXT NOT NULL DEFAULT '',
                file_time_seconds            REAL,
                file_time_end_seconds        REAL,
                duration_seconds             REAL,
                event_timestamp              TEXT,
                event_timestamp_source       TEXT NOT NULL DEFAULT 'none',
                event_timestamp_is_estimated INTEGER NOT NULL DEFAULT 0,
                observation                  TEXT NOT NULL DEFAULT '',
                debunk_reasoning             TEXT,
                status                       TEXT NOT NULL DEFAULT 'Needs Review',
                investigator_name            TEXT NOT NULL DEFAULT '',
                date_reviewed                TEXT NOT NULL,
                snapshot_path                TEXT,
                app_version                  TEXT,
                machine_name                 TEXT,
                created_at_utc               TEXT NOT NULL,
                updated_at_utc               TEXT NOT NULL,
                is_deleted                   INTEGER NOT NULL DEFAULT 0,
                sync_state                   TEXT NOT NULL DEFAULT 'pending',
                sync_error                   TEXT,
                synced_at_utc                TEXT,
                server_id                    INTEGER
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_entries_case ON log_entries(case_id, event_timestamp)",
            "CREATE INDEX IF NOT EXISTS ix_entries_sync ON log_entries(sync_state) "
            "WHERE sync_state <> 'synced'",
            "CREATE INDEX IF NOT EXISTS ix_entries_media ON log_entries(media_path, file_time_seconds)",
            "CREATE INDEX IF NOT EXISTS ix_entries_area ON log_entries(area)",
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id        TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                root_path      TEXT,
                created_at_utc TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS media_hash_cache (
                media_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns   INTEGER NOT NULL,
                sha256     TEXT NOT NULL,
                hashed_at  TEXT NOT NULL,
                PRIMARY KEY (media_path, size_bytes, mtime_ns)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS folder_area_memory (
                folder     TEXT PRIMARY KEY,
                area       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
]

MIGRATIONS.append(
    (
        2,
        (
            # event_timestamp keeps the offset it was recorded in, which is what a
            # reader needs to see. That makes it useless for ORDER BY: comparing
            # "20:00+05:30" with "16:00+00:00" as text puts the earlier event last.
            # This column holds the same instant normalised to UTC, purely to sort.
            "ALTER TABLE log_entries ADD COLUMN event_timestamp_utc TEXT",
            "CREATE INDEX IF NOT EXISTS ix_entries_case_event_utc "
            "ON log_entries(case_id, event_timestamp_utc)",
        ),
    )
)


MIGRATIONS.append(
    (
        3,
        (
            # An optimistic-concurrency token for the sync worker. See
            # LocalStore.mark_synced for why updated_at_utc cannot serve.
            "ALTER TABLE log_entries ADD COLUMN local_revision INTEGER NOT NULL DEFAULT 1",
        ),
    )
)


def _backfill_event_timestamp_utc(conn: sqlite3.Connection) -> None:
    """Populate the new sort column from existing rows.

    Done in Python rather than SQL so the stored text is byte-identical to what
    new rows write; a mix of "+00:00" and "Z" spellings would sort inconsistently.
    """
    rows = conn.execute(
        "SELECT entry_id, event_timestamp FROM log_entries WHERE event_timestamp IS NOT NULL"
    ).fetchall()
    for row in rows:
        parsed = from_iso_datetime(row["event_timestamp"])
        if parsed is None:
            continue
        conn.execute(
            "UPDATE log_entries SET event_timestamp_utc = ? WHERE entry_id = ?",
            (utc_sort_key(parsed), row["entry_id"]),
        )
    if rows:
        log.info("Backfilled event_timestamp_utc for %d entries", len(rows))


MIGRATIONS.append(
    (
        4,
        (
            # Event Timestamp was modelled as a wall-clock time. It is really the
            # position in the media where the event starts, so the reviewer can jump
            # back to it, and the companion field is how long the event lasted --
            # not the length of the file. Indexes on the old columns are dropped
            # first because SQLite refuses to drop an indexed column.
            "DROP INDEX IF EXISTS ix_entries_case",
            "DROP INDEX IF EXISTS ix_entries_case_event_utc",
            "DROP INDEX IF EXISTS ix_entries_media",
            "ALTER TABLE log_entries ADD COLUMN event_offset_seconds REAL",
            "ALTER TABLE log_entries ADD COLUMN event_duration_seconds REAL",
            "ALTER TABLE log_entries ADD COLUMN media_duration_seconds REAL",
            # Carry the old values across: the start offset was file_time_seconds,
            # and an explicit end point becomes a duration.
            "UPDATE log_entries SET event_offset_seconds = file_time_seconds, "
            "media_duration_seconds = duration_seconds, "
            "event_duration_seconds = CASE "
            "  WHEN file_time_end_seconds IS NOT NULL AND file_time_seconds IS NOT NULL "
            "   AND file_time_end_seconds > file_time_seconds "
            "  THEN file_time_end_seconds - file_time_seconds ELSE NULL END",
            "ALTER TABLE log_entries DROP COLUMN event_timestamp",
            "ALTER TABLE log_entries DROP COLUMN event_timestamp_source",
            "ALTER TABLE log_entries DROP COLUMN event_timestamp_is_estimated",
            "ALTER TABLE log_entries DROP COLUMN event_timestamp_utc",
            "ALTER TABLE log_entries DROP COLUMN file_time_seconds",
            "ALTER TABLE log_entries DROP COLUMN file_time_end_seconds",
            "ALTER TABLE log_entries DROP COLUMN duration_seconds",
            "CREATE INDEX IF NOT EXISTS ix_entries_case_created "
            "ON log_entries(case_id, created_at_utc)",
            "CREATE INDEX IF NOT EXISTS ix_entries_media_offset "
            "ON log_entries(media_path, event_offset_seconds)",
        ),
    )
)


MIGRATIONS.append(
    (
        5,
        (
            # The pull cursor was worker-instance state, so it was discarded every
            # time the worker was rebuilt -- on app restart and on every case
            # switch. A reviewer moving between cases in one sitting re-pulled each
            # case from the beginning each time. It belongs to the case, and it has
            # to outlive the worker, so it is stored here.
            "ALTER TABLE cases ADD COLUMN pull_cursor TEXT",
        ),
    )
)


MIGRATIONS.append(
    (
        6,
        (
            # Templates. A template owns the domain half of the form -- what the
            # reviewer is asked -- so that half stops being four fixed columns.
            """
            CREATE TABLE IF NOT EXISTS templates (
                template_id    TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
                version        INTEGER NOT NULL DEFAULT 1,
                is_builtin     INTEGER NOT NULL DEFAULT 0,
                is_customised  INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """,
            # field_id is a uuid and field_key is renameable, because a reviewer
            # will rename a field and must not orphan every value that points at
            # it. config holds only type-specific knobs (rows, min, max); no user
            # answer is ever stored in it.
            """
            CREATE TABLE IF NOT EXISTS template_fields (
                field_id    TEXT PRIMARY KEY,
                template_id TEXT NOT NULL REFERENCES templates(template_id) ON DELETE CASCADE,
                field_key   TEXT NOT NULL,
                label       TEXT NOT NULL,
                field_type  TEXT NOT NULL DEFAULT 'text',
                role        TEXT,
                position    INTEGER NOT NULL DEFAULT 0,
                is_required INTEGER NOT NULL DEFAULT 0,
                help_text   TEXT NOT NULL DEFAULT '',
                config      TEXT NOT NULL DEFAULT '{}',
                in_table    INTEGER NOT NULL DEFAULT 0,
                UNIQUE (template_id, field_key)
            )
            """,
            # Options are rows, not a JSON list, because each one carries data of
            # its own: the colour the log table paints it, and whether a new entry
            # starts on it.
            """
            CREATE TABLE IF NOT EXISTS template_field_options (
                option_id  TEXT PRIMARY KEY,
                field_id   TEXT NOT NULL REFERENCES template_fields(field_id) ON DELETE CASCADE,
                value      TEXT NOT NULL,
                label      TEXT NOT NULL DEFAULT '',
                colour     TEXT,
                position   INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                UNIQUE (field_id, value)
            )
            """,
            # "Y is required when X is V". The paranormal debunk rule used to be an
            # if-statement in the entry model, which meant no other template could
            # ever have one.
            """
            CREATE TABLE IF NOT EXISTS template_rules (
                rule_id           TEXT PRIMARY KEY,
                template_id       TEXT NOT NULL REFERENCES templates(template_id) ON DELETE CASCADE,
                when_field_id     TEXT NOT NULL,
                when_value        TEXT NOT NULL,
                requires_field_id TEXT NOT NULL
            )
            """,
            # One row per answer. Typed columns rather than a single stringly-typed
            # value, so a number sorts as a number and a date compares as a date.
            #
            # field_id is deliberately NOT a foreign key. A value can arrive from a
            # colleague on a template version this client has not pulled yet, and
            # the one thing this table must never do is drop a reviewer's words
            # because it has not heard of the question.
            """
            CREATE TABLE IF NOT EXISTS entry_values (
                entry_id     TEXT NOT NULL REFERENCES log_entries(entry_id) ON DELETE CASCADE,
                field_id     TEXT NOT NULL,
                value_text   TEXT,
                value_number REAL,
                value_date   TEXT,
                PRIMARY KEY (entry_id, field_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_values_text ON entry_values(field_id, value_text)",
            "CREATE INDEX IF NOT EXISTS ix_values_number ON entry_values(field_id, value_number)",
            "CREATE INDEX IF NOT EXISTS ix_values_date ON entry_values(field_id, value_date)",
            "CREATE INDEX IF NOT EXISTS ix_fields_template ON template_fields(template_id, position)",
            # Which form an entry was written with. On the entry and not only on the
            # case, because a case's template can change and an entry already
            # written must keep the shape it was written in. An evidence record that
            # silently re-interprets itself is not an evidence record.
            f"ALTER TABLE log_entries ADD COLUMN template_id TEXT NOT NULL "
            f"DEFAULT '{DEFAULT_TEMPLATE_ID}'",
            "ALTER TABLE log_entries ADD COLUMN template_version INTEGER NOT NULL DEFAULT 1",
            # The template new entries in this case start on.
            "ALTER TABLE cases ADD COLUMN template_id TEXT",
        ),
    )
)


def _seed_templates_and_backfill(conn: sqlite3.Connection) -> None:
    """Install the built-in templates and move existing answers into entry_values.

    Every entry written before this migration was written on the paranormal form,
    so its four domain columns are copied onto that template's fields. The columns
    are left in place: they are verified here and dropped in a later release, so a
    reviewer who has to roll back still has their data.
    """
    for template in BUILTIN_TEMPLATES:
        _write_template(conn, template)

    paranormal = BUILTIN_TEMPLATES[0]
    legacy = {
        "area": "area",
        "observation": "observation",
        "debunk_reasoning": "debunk_reasoning",
        "status": "status",
    }
    field_ids: dict[str, str] = {}
    for column, key in legacy.items():
        field = paranormal.field_by_key(key)
        if field is not None:
            field_ids[column] = field.field_id

    rows = conn.execute(
        "SELECT entry_id, area, observation, debunk_reasoning, status FROM log_entries"
    ).fetchall()
    copied = 0
    for row in rows:
        for column, field_id in field_ids.items():
            value = row[column]
            if value is None or not str(value).strip():
                continue
            conn.execute(
                "INSERT OR REPLACE INTO entry_values "
                "(entry_id, field_id, value_text, value_number, value_date) "
                "VALUES (?, ?, ?, NULL, NULL)",
                (row["entry_id"], field_id, str(value)),
            )
            copied += 1

    if rows:
        # Verified before the columns are ever dropped: every non-empty answer
        # that was in a column is now also a row.
        expected = sum(
            1
            for row in rows
            for column in field_ids
            if row[column] is not None and str(row[column]).strip()
        )
        stored = conn.execute(
            "SELECT COUNT(*) AS n FROM entry_values WHERE field_id IN "
            f"({','.join('?' for _ in field_ids)})",
            tuple(field_ids.values()),
        ).fetchone()["n"]
        if stored != expected:
            raise RuntimeError(
                f"Template backfill lost data: expected {expected} values, stored {stored}. "
                "The four original columns are untouched; no data has been lost."
            )
        log.info(
            "Migrated %d answers from %d entries onto the paranormal template", copied, len(rows)
        )


MIGRATIONS.append(
    (
        7,
        (
            # Marks drawn over the picture: text, arrows, circles, boxes.
            #
            # A separate table, never a change to the media and never a change to the
            # entry's answers. An annotation is somebody's reading of the footage, and
            # the recording it points at has to stay exactly as the camera wrote it.
            #
            # Geometry is stored as fractions of the frame, not pixels, so the same mark
            # lands in the same place on a preview window, on a 4K export and on a
            # colleague's screen. Pixels would put the circle on a face in one and a
            # doorway in the other.
            """
            CREATE TABLE IF NOT EXISTS annotations (
                annotation_id  TEXT PRIMARY KEY,
                entry_id       TEXT NOT NULL REFERENCES log_entries(entry_id) ON DELETE CASCADE,
                kind           TEXT NOT NULL,
                x1             REAL NOT NULL,
                y1             REAL NOT NULL,
                x2             REAL NOT NULL,
                y2             REAL NOT NULL,
                text           TEXT NOT NULL DEFAULT '',
                colour         TEXT NOT NULL,
                stroke         REAL NOT NULL,
                font_scale     REAL NOT NULL,
                start_seconds  REAL NOT NULL,
                end_seconds    REAL NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                is_deleted     INTEGER NOT NULL DEFAULT 0
            )
            """,
            # Reading them back is always "every mark on this entry, in the order they
            # were made", which is what this index serves.
            "CREATE INDEX IF NOT EXISTS ix_annotations_entry "
            "ON annotations(entry_id, created_at_utc)",
        ),
    )
)


MIGRATIONS.append(
    (
        8,
        (
            # What the reviewer was listening through when they wrote the entry.
            #
            # Empty means the recording as it was given, which is both the default and
            # the common case. Anything else is an ffmpeg filter specification: exactly
            # what was applied, exactly reproducible. An observation made under heavy
            # noise reduction is a different claim from the same words written against
            # the raw audio, and the record has to be able to say which.
            "ALTER TABLE log_entries ADD COLUMN audio_filters TEXT NOT NULL DEFAULT ''",
        ),
    )
)


#: Python steps that must run after a migration's SQL, keyed by version.
POST_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _backfill_event_timestamp_utc,
    6: _seed_templates_and_backfill,
}


def utc_sort_key(value: _dt.datetime | None) -> str | None:
    """A UTC-normalised ISO string, where text ordering equals chronological order."""
    if value is None:
        return None
    return value.astimezone(_dt.UTC).isoformat()


#: Wire columns, in the order used by INSERT/UPDATE statements.
_WIRE_COLUMNS: tuple[str, ...] = (
    "entry_id",
    "case_id",
    "template_id",
    "template_version",
    "file_name",
    "media_path",
    "media_sha256",
    "media_kind",
    "area",
    "event_offset_seconds",
    "event_duration_seconds",
    "media_duration_seconds",
    "observation",
    "debunk_reasoning",
    "status",
    "investigator_name",
    "date_reviewed",
    "snapshot_path",
    "app_version",
    "machine_name",
    "audio_filters",
    "created_at_utc",
    "updated_at_utc",
    "is_deleted",
)

_SYNC_COLUMNS: tuple[str, ...] = ("sync_state", "sync_error", "synced_at_utc", "server_id")

#: Derived, never sent over the wire; maintained alongside event_timestamp.
_DERIVED_COLUMNS: tuple[str, ...] = ("local_revision",)

#: Columns the upsert must not copy verbatim from the incoming row.
_NO_OVERWRITE_ON_CONFLICT: frozenset[str] = frozenset({"entry_id", "local_revision"})
_ALL_COLUMNS: tuple[str, ...] = _WIRE_COLUMNS + _SYNC_COLUMNS + _DERIVED_COLUMNS


# --------------------------------------------------------------------------- #
# Template persistence
# --------------------------------------------------------------------------- #


class TemplateError(ValueError):
    """A template that cannot be stored as it stands."""


def validate_template(template: LogTemplate) -> list[str]:
    """Why this template cannot be stored, if it cannot.

    Checked before writing rather than left to the database. The schema enforces
    these too, but a UNIQUE constraint surfacing as a raw IntegrityError tells the
    caller nothing it can act on -- and a template arriving over sync would take the
    worker down with it rather than being reported and skipped.
    """
    problems: list[str] = []
    if not template.template_id.strip():
        problems.append("A template needs an id.")
    if not template.name.strip():
        problems.append("A template needs a name.")

    seen_keys: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for field in template.fields:
        seen_keys[field.field_key] = seen_keys.get(field.field_key, 0) + 1
        seen_ids[field.field_id] = seen_ids.get(field.field_id, 0) + 1
    for key, count in sorted(seen_keys.items()):
        if count > 1:
            problems.append(f"Two fields are both named {key!r}.")
    for field_id, count in sorted(seen_ids.items()):
        if count > 1:
            problems.append(f"Two fields share the id {field_id!r}.")

    for field in template.fields:
        values: dict[str, int] = {}
        for option in field.options:
            values[option.value] = values.get(option.value, 0) + 1
        for value, count in sorted(values.items()):
            if count > 1:
                problems.append(f"{field.label!r} lists the choice {value!r} twice.")
    return problems


def _write_template(conn: sqlite3.Connection, template: LogTemplate) -> None:
    """Store a template, replacing its fields, options and rules wholesale.

    Replacement rather than a diff because a template is edited as a whole: the
    reviewer reorders, retypes and deletes in one sitting and presses Save once.
    Deleting the children cascades, and the rows are rewritten from the model, so
    there is no path where a removed field lingers.

    Values are not touched. A value whose field has just been deleted stays in
    entry_values, unreachable but intact, because the reviewer may put the field
    back and because evidence is not deleted as a side effect of editing a form.
    """
    problems = validate_template(template)
    if problems:
        raise TemplateError("; ".join(problems))

    now = to_iso(utc_now())
    conn.execute(
        "INSERT INTO templates (template_id, name, description, version, is_builtin, "
        "is_customised, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(template_id) DO UPDATE SET name=excluded.name, "
        "description=excluded.description, version=excluded.version, "
        "is_builtin=excluded.is_builtin, is_customised=excluded.is_customised, "
        "updated_at_utc=excluded.updated_at_utc",
        (
            template.template_id,
            template.name,
            template.description,
            template.version,
            int(template.is_builtin),
            int(template.is_customised),
            now,
            now,
        ),
    )
    conn.execute("DELETE FROM template_rules WHERE template_id = ?", (template.template_id,))
    conn.execute("DELETE FROM template_fields WHERE template_id = ?", (template.template_id,))

    for field in template.fields:
        conn.execute(
            "INSERT INTO template_fields (field_id, template_id, field_key, label, field_type, "
            "role, position, is_required, help_text, config, in_table) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                field.field_id,
                template.template_id,
                field.field_key,
                field.label,
                field.field_type.value,
                field.role.value if field.role else None,
                field.position,
                int(field.is_required),
                field.help_text,
                json.dumps(field.config, sort_keys=True),
                int(field.in_table),
            ),
        )
        for option in field.options:
            conn.execute(
                "INSERT INTO template_field_options (option_id, field_id, value, label, colour, "
                "position, is_default) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    option.option_id,
                    field.field_id,
                    option.value,
                    option.label,
                    option.colour,
                    option.position,
                    int(option.is_default),
                ),
            )

    for rule in template.rules:
        conn.execute(
            "INSERT INTO template_rules (rule_id, template_id, when_field_id, when_value, "
            "requires_field_id) VALUES (?, ?, ?, ?, ?)",
            (
                rule.rule_id,
                template.template_id,
                rule.when_field_id,
                rule.when_value,
                rule.requires_field_id,
            ),
        )


def _read_template(conn: sqlite3.Connection, template_id: str) -> LogTemplate | None:
    row = conn.execute("SELECT * FROM templates WHERE template_id = ?", (template_id,)).fetchone()
    if row is None:
        return None

    options_by_field: dict[str, list[FieldOption]] = {}
    option_rows = conn.execute(
        "SELECT o.* FROM template_field_options o JOIN template_fields f "
        "ON f.field_id = o.field_id WHERE f.template_id = ? ORDER BY o.position",
        (template_id,),
    )
    for option in option_rows:
        options_by_field.setdefault(option["field_id"], []).append(
            FieldOption(
                option_id=option["option_id"],
                value=option["value"],
                label=option["label"] or option["value"],
                colour=option["colour"],
                position=option["position"],
                is_default=bool(option["is_default"]),
            )
        )

    fields: list[TemplateField] = []
    for field in conn.execute(
        "SELECT * FROM template_fields WHERE template_id = ? ORDER BY position", (template_id,)
    ):
        try:
            config = json.loads(field["config"] or "{}")
        except json.JSONDecodeError:
            config = {}
        fields.append(
            TemplateField(
                field_id=field["field_id"],
                field_key=field["field_key"],
                label=field["label"],
                field_type=FieldType(field["field_type"]),
                role=FieldRole(field["role"]) if field["role"] else None,
                position=field["position"],
                is_required=bool(field["is_required"]),
                help_text=field["help_text"] or "",
                config=config if isinstance(config, dict) else {},
                options=options_by_field.get(field["field_id"], []),
                in_table=bool(field["in_table"]),
            )
        )

    rules = [
        TemplateRule(
            rule_id=rule["rule_id"],
            when_field_id=rule["when_field_id"],
            when_value=rule["when_value"],
            requires_field_id=rule["requires_field_id"],
        )
        for rule in conn.execute(
            "SELECT * FROM template_rules WHERE template_id = ?", (template_id,)
        )
    ]

    return LogTemplate(
        template_id=row["template_id"],
        name=row["name"],
        description=row["description"] or "",
        version=row["version"],
        is_builtin=bool(row["is_builtin"]),
        is_customised=bool(row["is_customised"]),
        fields=fields,
        rules=rules,
    )


def _value_params(
    field: TemplateField | None, value: Any
) -> tuple[str | None, float | None, str | None]:
    """Spread one answer across the typed value columns.

    A field this client has not heard of is stored as text. It is a colleague's
    answer on a template version not pulled yet, and keeping it as text is how it
    survives until the template arrives.
    """
    if field is None:
        return (None if value is None else str(value)), None, None

    storage = field.field_type.storage
    if storage == "value_number":
        if isinstance(value, bool):
            return None, float(value), None
        try:
            return None, float(value), None
        except (TypeError, ValueError):
            return str(value), None, None
    if storage == "value_date":
        if isinstance(value, _dt.date):
            return None, None, value.isoformat()
        return str(value), None, None
    return str(value), None, None


def _value_from_row(field: TemplateField | None, row: sqlite3.Row) -> Any:
    if row["value_number"] is not None:
        number = float(row["value_number"])
        if field is not None and field.field_type is FieldType.BOOLEAN:
            return bool(number)
        return number
    if row["value_date"] is not None:
        return from_iso_date(row["value_date"]) or row["value_date"]
    return row["value_text"]


# --------------------------------------------------------------------------- #
# Row mapping
# --------------------------------------------------------------------------- #


def _row_to_entry(
    row: sqlite3.Row,
    values: dict[str, Any] | None = None,
    annotations: list[Annotation] | None = None,
) -> EntryRow:
    return EntryRow(
        entry_id=row["entry_id"],
        case_id=row["case_id"],
        template_id=row["template_id"],
        template_version=row["template_version"],
        values=values or {},
        annotations=annotations or [],
        file_name=row["file_name"],
        media_path=row["media_path"] or "",
        media_sha256=row["media_sha256"],
        media_kind=MediaKind(row["media_kind"]),
        event_offset_seconds=row["event_offset_seconds"],
        event_duration_seconds=row["event_duration_seconds"],
        media_duration_seconds=row["media_duration_seconds"],
        # area, observation, debunk_reasoning and status are deliberately not read.
        # They are write-only projections of values; see LEGACY_ROLE_COLUMNS.
        investigator_name=row["investigator_name"] or "",
        date_reviewed=from_iso_date(row["date_reviewed"]) or utc_now().date(),
        snapshot_path=row["snapshot_path"],
        app_version=row["app_version"],
        machine_name=row["machine_name"],
        audio_filters=row["audio_filters"] or "",
        created_at_utc=from_iso_datetime(row["created_at_utc"]) or utc_now(),
        updated_at_utc=from_iso_datetime(row["updated_at_utc"]) or utc_now(),
        is_deleted=bool(row["is_deleted"]),
        local_revision=row["local_revision"] or 0,
        sync_state=SyncState(row["sync_state"]),
        sync_error=row["sync_error"],
        synced_at_utc=from_iso_datetime(row["synced_at_utc"]),
        server_id=row["server_id"],
    )


def _row_to_case(row: sqlite3.Row) -> Case:
    return Case(
        case_id=row["case_id"],
        name=row["name"],
        root_path=row["root_path"],
        template_id=row["template_id"],
        created_at_utc=from_iso_datetime(row["created_at_utc"]) or utc_now(),
    )


#: One statement, used by the editor's save and by the pull's merge alike. Two copies
#: of a sixteen-column upsert is how the two quietly stop agreeing.
_ANNOTATION_UPSERT = """
    INSERT INTO annotations (
        annotation_id, entry_id, kind, x1, y1, x2, y2, text, colour,
        stroke, font_scale, start_seconds, end_seconds,
        created_at_utc, updated_at_utc, is_deleted
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(annotation_id) DO UPDATE SET
        kind=excluded.kind, x1=excluded.x1, y1=excluded.y1,
        x2=excluded.x2, y2=excluded.y2, text=excluded.text,
        colour=excluded.colour, stroke=excluded.stroke,
        font_scale=excluded.font_scale,
        start_seconds=excluded.start_seconds,
        end_seconds=excluded.end_seconds,
        updated_at_utc=excluded.updated_at_utc,
        is_deleted=excluded.is_deleted
"""


def _annotation_params(entry_id: str, annotation: Annotation) -> tuple:
    return (
        annotation.annotation_id,
        entry_id,
        annotation.kind.value,
        annotation.x1, annotation.y1, annotation.x2, annotation.y2,
        annotation.text,
        annotation.colour,
        annotation.stroke,
        annotation.font_scale,
        annotation.start_seconds,
        annotation.end_seconds,
        to_iso(annotation.created_at_utc),
        to_iso(annotation.updated_at_utc),
        1 if annotation.is_deleted else 0,
    )


def _annotation_fingerprint(annotation: Annotation) -> str:
    """A digest of everything about a mark except when it was last touched."""
    return json.dumps(
        annotation.model_dump(exclude={"updated_at_utc", "created_at_utc"}),
        sort_keys=True,
        default=str,
    )


def _supersedes(incoming: Annotation, stored: Annotation) -> bool:
    """Should ``incoming`` replace ``stored``?

    Newer wins. The awkward case is a tie, and ties are not rare: this project already
    found that Windows resolves the wall clock to about 15 ms -- it is why entries carry
    a revision counter rather than trusting ``updated_at_utc`` -- and a measurement here
    produced two thousand readings with a single distinct value between them.

    A strict ``>`` therefore dropped a colleague's correction in silence whenever it
    landed in the same tick as the version already stored. On a tie the two
    fingerprints are compared instead: an identical mark is not rewritten, and two
    genuinely different edits resolve the same way on every machine that sees them.
    One of them still loses -- that is inherent to last-write-wins -- but every client
    agrees on which, and neither disappears without the other being kept.
    """
    if incoming.updated_at_utc > stored.updated_at_utc:
        return True
    if incoming.updated_at_utc < stored.updated_at_utc:
        return False
    mine, theirs = _annotation_fingerprint(incoming), _annotation_fingerprint(stored)
    return mine > theirs


def _row_to_annotation(row: sqlite3.Row) -> Annotation:
    """One stored mark.

    An unrecognised ``kind`` falls back to a rectangle rather than raising. A mark
    written by a newer client is still a mark somebody made about this footage, and
    drawing it as a box beats refusing to open the entry.
    """
    try:
        kind = AnnotationKind(row["kind"])
    except ValueError:
        log.warning("Unknown annotation kind %r; drawing it as a box", row["kind"])
        kind = AnnotationKind.RECTANGLE
    return Annotation(
        annotation_id=row["annotation_id"],
        entry_id=row["entry_id"],
        kind=kind,
        x1=row["x1"], y1=row["y1"], x2=row["x2"], y2=row["y2"],
        text=row["text"] or "",
        colour=row["colour"],
        stroke=row["stroke"],
        font_scale=row["font_scale"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        created_at_utc=from_iso_datetime(row["created_at_utc"]) or utc_now(),
        updated_at_utc=from_iso_datetime(row["updated_at_utc"]) or utc_now(),
        is_deleted=bool(row["is_deleted"]),
    )


def _legacy_projection(template: LogTemplate | None, values: dict[str, Any]) -> dict[str, Any]:
    """The four pre-template columns, derived from whichever fields hold those roles.

    See :data:`evidence_review.models.LEGACY_ROLE_COLUMNS`. Write-only: nothing
    reads these columns back, and they exist so the PHP web log and a rollback to
    1.1.0 still work.
    """
    out: dict[str, Any] = {"area": "", "observation": "", "debunk_reasoning": None, "status": ""}
    if template is None:
        return out
    for column, role in LEGACY_ROLE_COLUMNS.items():
        field = template.field_for_role(role)
        if field is None:
            continue
        text = field.display(values.get(field.field_id))
        limit = LEGACY_LIMITS.get(column)
        if limit:
            text = text[:limit]
        if text:
            out[column] = text
    return out


def _entry_to_params(entry: EntryRow, legacy: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "case_id": entry.case_id,
        "template_id": entry.template_id,
        "template_version": entry.template_version,
        "file_name": entry.file_name,
        "media_path": entry.media_path,
        "media_sha256": entry.media_sha256,
        "media_kind": entry.media_kind.value,
        "area": legacy["area"],
        "event_offset_seconds": entry.event_offset_seconds,
        "event_duration_seconds": entry.event_duration_seconds,
        "media_duration_seconds": entry.media_duration_seconds,
        # Only used when inserting; an update increments the stored value instead.
        "local_revision": 1,
        "observation": legacy["observation"],
        "debunk_reasoning": legacy["debunk_reasoning"],
        "status": legacy["status"],
        "investigator_name": entry.investigator_name,
        "date_reviewed": to_iso(entry.date_reviewed),
        "snapshot_path": entry.snapshot_path,
        "app_version": entry.app_version,
        "machine_name": entry.machine_name,
        "audio_filters": entry.audio_filters or "",
        "created_at_utc": to_iso(entry.created_at_utc),
        "updated_at_utc": to_iso(entry.updated_at_utc),
        "is_deleted": int(entry.is_deleted),
        "sync_state": entry.sync_state.value,
        "sync_error": entry.sync_error,
        "synced_at_utc": to_iso(entry.synced_at_utc),
        "server_id": entry.server_id,
    }


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class LocalStore:
    """Thread-safe SQLite store. Each thread gets its own connection."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        #: Templates are read on every save and every list; they change only when
        #: somebody edits one.
        #:
        #: Guarded by a lock of their own, because the UI thread and the sync worker
        #: both reach them. Without it a reader that has decided the cache holds an
        #: entry can be invalidated before it reads it -- KeyError from get_template,
        #: and an AttributeError from the field index, both in a background thread.
        #: Not the write lock: save() holds that for a whole transaction, and a pull
        #: should not wait on it just to read a label.
        self._cache_lock = threading.RLock()
        self._template_cache: dict[str, LogTemplate | None] = {}
        self._field_cache: dict[str, TemplateField] | None = None
        self._migrate()
        self.seed_builtin_templates()

    # -- connection management --------------------------------------------- #

    @property
    def conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "conn", None)
        if connection is None:
            connection = sqlite3.connect(
                self.db_path, timeout=30.0, isolation_level=None, check_same_thread=True
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._local.conn = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._local, "conn", None)
        if connection is not None:
            connection.close()
            self._local.conn = None

    def _migrate(self) -> None:
        with self._write_lock:
            conn = self.conn
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        try:
                            conn.execute(statement)
                        except sqlite3.OperationalError as exc:
                            # Tolerate a column that is already present, so a
                            # hand-repaired or partially-upgraded database can
                            # still be brought forward instead of refusing to open.
                            if "duplicate column name" not in str(exc).lower():
                                raise
                            log.debug("Migration %s: %s already applied", version, statement)
                    post_step = POST_MIGRATIONS.get(version)
                    if post_step is not None:
                        post_step(conn)
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                    conn.execute("COMMIT")
                    log.info("Applied local schema migration %s", version)
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

    # -- templates ---------------------------------------------------------- #

    def seed_builtin_templates(self) -> None:
        """Install or refresh the templates that ship.

        Runs on every open so a built-in that gains a field in a new release is
        picked up without a migration. A reviewer's own templates are never
        touched, and neither are stored values -- only the definitions.
        """
        with self._write_lock:
            for template in BUILTIN_TEMPLATES:
                stored = _read_template(self.conn, template.template_id)
                if stored is not None and stored.version >= template.version:
                    continue
                if stored is not None and stored.is_customised:
                    # The reviewer has changed this one. Overwriting it to pick up a
                    # new shipped field would silently delete their status list, and
                    # a vocabulary somebody curated is their work, not ours.
                    log.info(
                        "Leaving customised template %s at version %d; shipped version is %d",
                        stored.name,
                        stored.version,
                        template.version,
                    )
                    continue
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    _write_template(self.conn, template)
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                log.info("Installed built-in template %s v%d", template.name, template.version)
            self._invalidate_template_cache()

    def _invalidate_template_cache(self) -> None:
        with self._cache_lock:
            self._template_cache.clear()
            self._field_cache = None

    def upsert_template(self, template: LogTemplate) -> None:
        """Store a template. Its fields, options and rules are replaced wholesale."""
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                _write_template(self.conn, template)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            self._invalidate_template_cache()

    def get_template(self, template_id: str) -> LogTemplate | None:
        with self._cache_lock:
            cached = self._template_cache.get(template_id, _MISSING)
            if cached is not _MISSING:
                return cached

        # Read outside the lock: it is several queries, and holding a lock across them
        # would make every template read serialise behind the slowest one.
        template = _read_template(self.conn, template_id)

        with self._cache_lock:
            # Checked again, because an edit may have landed while this was reading.
            # The stored value wins; this one is already stale.
            existing = self._template_cache.get(template_id, _MISSING)
            if existing is not _MISSING:
                return existing
            self._template_cache[template_id] = template
        return template

    def list_templates(self) -> list[LogTemplate]:
        rows = self.conn.execute(
            "SELECT template_id FROM templates ORDER BY is_builtin DESC, name COLLATE NOCASE"
        ).fetchall()
        templates = [self.get_template(row["template_id"]) for row in rows]
        return [template for template in templates if template is not None]

    def delete_template(self, template_id: str) -> bool:
        """Remove a template. Refuses a built-in, and never touches stored values."""
        template = self.get_template(template_id)
        if template is None or template.is_builtin:
            return False
        with self._write_lock:
            self.conn.execute("DELETE FROM templates WHERE template_id = ?", (template_id,))
            self._invalidate_template_cache()
        return True

    def status_vocabulary(self, case_id: str) -> tuple[list[str], str]:
        """The status options of this case's template, and which one is the default."""
        field = self.template_for_case(case_id).status_field
        if field is None:
            return [], ""
        options = sorted(field.options, key=lambda option: option.position)
        default = next((option.value for option in options if option.is_default), "")
        return [option.value for option in options], default

    def apply_status_vocabulary(
        self, case_id: str, statuses: list[str], default_status: str | None = None
    ) -> bool:
        """Replace the status options of this case's template.

        Returns whether anything changed. Existing options keep their id and colour,
        so re-ordering the list does not repaint a log that was already readable, and
        a status that is removed and re-added comes back the colour it was.

        Removing a status does not touch entries that already hold it: the value
        stays, and the log falls back to a derived colour for it. Editing a form is
        not a reason to alter a record made with it.
        """
        template = self.template_for_case(case_id)
        field = template.status_field
        if field is None:
            return False

        wanted = list(dict.fromkeys(status for status in statuses if status.strip()))
        if not wanted:
            return False
        existing = {option.value: option for option in field.options}
        default = default_status if default_status in wanted else wanted[0]

        rebuilt = []
        for position, value in enumerate(wanted):
            option = existing.get(value)
            if option is None:
                option = FieldOption(value=value, label=value, position=position)
            option.position = position
            option.is_default = value == default
            rebuilt.append(option)

        before = [(o.value, o.position, o.is_default) for o in field.options]
        after = [(o.value, o.position, o.is_default) for o in rebuilt]
        if before == after:
            return False

        field.options = rebuilt
        template.is_customised = True
        self.upsert_template(template)
        return True

    def template_usage(self, template_id: str) -> int:
        """How many live entries were written on this template."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM log_entries WHERE template_id = ? AND is_deleted = 0",
            (template_id,),
        ).fetchone()
        return int(row["n"])

    def field_usage(self, field_id: str) -> int:
        """How many entries hold a value for this field, for the delete warning."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM entry_values WHERE field_id = ?", (field_id,)
        ).fetchone()
        return int(row["n"])

    def _field_index(self) -> dict[str, TemplateField]:
        """Every known field by id, for typing values on the way in and out.

        Returns the dict it built rather than re-reading the attribute, so a caller
        can never be handed the None another thread has just put there.
        """
        with self._cache_lock:
            cached = self._field_cache
        if cached is not None:
            return cached

        index: dict[str, TemplateField] = {}
        for template in self.list_templates():
            for field in template.fields:
                index[field.field_id] = field

        with self._cache_lock:
            self._field_cache = index
        return index

    # -- entries ------------------------------------------------------------ #

    def save(self, entry: EntryRow) -> EntryRow:
        """Insert or replace an entry and its answers. The LOG modal depends on this.

        Both halves land in one transaction, so there is no moment at which an
        entry exists without the answers it was saved with -- which is what lets
        the wire format stay one document per entry and stay idempotent.
        """
        template = self.get_template(entry.template_id)
        if template is not None:
            entry.values = template.coerce_values(entry.values)

        params = _entry_to_params(entry, _legacy_projection(template, entry.values))
        columns = ", ".join(_ALL_COLUMNS)
        placeholders = ", ".join(f":{name}" for name in _ALL_COLUMNS)
        updates = ", ".join(
            f"{name}=excluded.{name}"
            for name in _ALL_COLUMNS
            if name not in _NO_OVERWRITE_ON_CONFLICT
        )
        # Monotonic per-entry version, independent of clock resolution.
        updates += ", local_revision=log_entries.local_revision + 1"
        sql = (
            f"INSERT INTO log_entries ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(entry_id) DO UPDATE SET {updates}"
        )
        fields = self._field_index()
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(sql, params)
                # Replaced wholesale, not upserted key by key: an answer the
                # reviewer cleared has to disappear, and a per-key upsert would
                # leave the old one sitting there.
                self.conn.execute("DELETE FROM entry_values WHERE entry_id = ?", (entry.entry_id,))
                for field_id, value in entry.values.items():
                    text, number, date = _value_params(fields.get(field_id), value)
                    self.conn.execute(
                        "INSERT INTO entry_values "
                        "(entry_id, field_id, value_text, value_number, value_date) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (entry.entry_id, field_id, text, number, date),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return entry

    def _annotations_for(self, entry_ids: list[str]) -> dict[str, list[Annotation]]:
        """Marks for several entries in one query, tombstones included.

        Tombstones are included because these go on the wire: a removal has to reach a
        colleague as a deletion, and an absence would be indistinguishable from a mark
        they have not been told about yet.
        """
        if not entry_ids:
            return {}
        out: dict[str, list[Annotation]] = {entry_id: [] for entry_id in entry_ids}
        for start in range(0, len(entry_ids), 500):
            chunk = entry_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT * FROM annotations WHERE entry_id IN ({placeholders}) "
                f"ORDER BY created_at_utc, rowid",
                chunk,
            )
            for row in rows:
                out[row["entry_id"]].append(_row_to_annotation(row))
        return out

    def _values_for(self, entry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Answers for several entries in one query.

        One query per entry would make opening a case with a thousand entries a
        thousand round trips, which is the usual way a relational model gets
        blamed for being slow.
        """
        if not entry_ids:
            return {}
        fields = self._field_index()
        out: dict[str, dict[str, Any]] = {entry_id: {} for entry_id in entry_ids}
        # Chunked to stay under SQLITE_MAX_VARIABLE_NUMBER, which is 999 on the
        # builds that ship with older Pythons.
        for start in range(0, len(entry_ids), 500):
            chunk = entry_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.conn.execute(
                f"SELECT * FROM entry_values WHERE entry_id IN ({placeholders})", chunk
            ):
                out[row["entry_id"]][row["field_id"]] = _value_from_row(
                    fields.get(row["field_id"]), row
                )
        return out

    def get(self, entry_id: str) -> EntryRow | None:
        row = self.conn.execute(
            "SELECT * FROM log_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_entry(
            row,
            self._values_for([entry_id]).get(entry_id),
            self._annotations_for([entry_id]).get(entry_id),
        )

    def list_entries(
        self,
        *,
        case_id: str | None = None,
        include_deleted: bool = False,
        media_path: str | None = None,
        search: str | None = None,
        limit: int | None = None,
    ) -> list[EntryRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(case_id)
        if not include_deleted:
            clauses.append("is_deleted = 0")
        if media_path:
            # Matched without regard to case, because the clients are Windows and macOS
            # and both treat C:\Evidence\Cellar.wav and c:\evidence\cellar.wav as the
            # same file. An exact comparison silently detached a recording's entries
            # from it whenever the path reached the app with different capitalisation --
            # a file dialog and a folder listing do not always agree about the drive
            # letter -- and the reviewer saw an empty marker bar on a file they had
            # already worked through, with no way to tell the work was still there.
            clauses.append("media_path = ? COLLATE NOCASE")
            params.append(media_path)
        if search:
            # Every stored answer, not a hand-maintained list of four columns. A
            # field a reviewer added last week is searchable the moment it holds a
            # value, with nothing here to update.
            clauses.append(
                "(file_name LIKE ? OR entry_id IN "
                "(SELECT entry_id FROM entry_values WHERE value_text LIKE ?))"
            )
            like = f"%{search}%"
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Newest-logged first: this is a running session log. created_at_utc is
        # stored UTC-normalised, so text ordering is chronological ordering.
        sql = f"SELECT * FROM log_entries {where} ORDER BY created_at_utc DESC, rowid DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, params).fetchall()
        entry_ids = [row["entry_id"] for row in rows]
        values = self._values_for(entry_ids)
        marks = self._annotations_for(entry_ids)
        return [
            _row_to_entry(row, values.get(row["entry_id"]), marks.get(row["entry_id"]))
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Annotations
    # ------------------------------------------------------------------ #

    def annotations_for(
        self, entry_id: str, *, include_deleted: bool = False
    ) -> list[Annotation]:
        """Every mark on one entry, oldest first."""
        sql = "SELECT * FROM annotations WHERE entry_id = ?"
        if not include_deleted:
            sql += " AND is_deleted = 0"
        sql += " ORDER BY created_at_utc, rowid"
        return [_row_to_annotation(row) for row in self.conn.execute(sql, (entry_id,))]

    def annotations_for_media(
        self, media_path: str, *, case_id: str | None = None
    ) -> list[Annotation]:
        """Every mark on a recording, from whichever entries carry them.

        Exporting a whole recording has to pick up the marks made against all of its
        events, not just the one the reviewer happened to open the dialog from.
        """
        sql = (
            "SELECT a.* FROM annotations a "
            "JOIN log_entries e ON e.entry_id = a.entry_id "
            "WHERE a.is_deleted = 0 AND e.is_deleted = 0 AND e.media_path = ?"
        )
        params: list[Any] = [media_path]
        if case_id:
            sql += " AND e.case_id = ?"
            params.append(case_id)
        sql += " ORDER BY a.start_seconds, a.created_at_utc"
        return [_row_to_annotation(row) for row in self.conn.execute(sql, params)]

    def save_annotations(self, entry_id: str, annotations: list[Annotation]) -> None:
        """Replace this entry's marks with ``annotations``.

        Replace rather than merge, because the editor hands back the whole set it was
        working on and a mark the reviewer deleted has to actually go. Marks are
        tombstoned rather than erased, like entries: ``is_deleted`` is kept so an
        accidental deletion can be undone, so a record of what was there survives, and
        so the removal can be told apart on the wire from a mark a colleague simply has
        not heard about yet.

        The entry is queued for sync afterwards. Its answers are untouched -- annotating
        is not editing the observation -- but the document that goes to the server has
        changed, and without this the marks would never leave the machine.
        """
        with self._write_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                keep = {a.annotation_id for a in annotations}
                existing = {
                    row["annotation_id"]
                    for row in self.conn.execute(
                        "SELECT annotation_id FROM annotations WHERE entry_id = ?",
                        (entry_id,),
                    )
                }
                gone = existing - keep
                if gone:
                    self.conn.executemany(
                        "UPDATE annotations SET is_deleted = 1, updated_at_utc = ? "
                        "WHERE annotation_id = ?",
                        [(to_iso(utc_now()), aid) for aid in gone],
                    )
                for annotation in annotations:
                    self.conn.execute(
                        _ANNOTATION_UPSERT, _annotation_params(entry_id, annotation)
                    )
                # local_revision is the token the sync worker uses to prove the version
                # it uploaded is still the current one, so it has to move when the
                # marks move. updated_at_utc deliberately does not: that is when the
                # observation was last written, and nobody rewrote it.
                self.conn.execute(
                    "UPDATE log_entries SET sync_state = ?, sync_error = NULL, "
                    "local_revision = local_revision + 1 WHERE entry_id = ?",
                    (SyncState.PENDING.value, entry_id),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _write_annotations(self, entry_id: str, annotations: list[Annotation]) -> None:
        """Upsert marks exactly as given, touching nothing else.

        Used by the pull, which has already decided which marks win and must not
        tombstone anything or re-queue the entry for sync -- that would push straight
        back what was just taken.
        """
        with self._write_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for annotation in annotations:
                    self.conn.execute(_ANNOTATION_UPSERT, _annotation_params(entry_id, annotation))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def merge_annotations(self, entry_id: str, incoming: list[Annotation]) -> int:
        """Take a colleague's marks, newest wins per mark. Returns how many were taken.

        Merged one at a time by ``annotation_id`` rather than replaced wholesale, which
        is the difference between two investigators being able to annotate the same
        event and one of them silently losing their work. Replacing would mean whoever
        synced last owned the entry.

        A mark the server has not heard of is left alone rather than deleted: it is
        almost always one this client made and has not pushed yet.
        """
        if not incoming:
            return 0
        existing = {
            a.annotation_id: a
            for a in self.annotations_for(entry_id, include_deleted=True)
        }
        taken = [
            mark for mark in incoming
            if mark.annotation_id not in existing
            or _supersedes(mark, existing[mark.annotation_id])
        ]
        if not taken:
            return 0
        self._write_annotations(entry_id, taken)
        return len(taken)

    def annotation_counts(self, entry_ids: list[str]) -> dict[str, int]:
        """How many live marks each entry has, for showing a count without loading them."""
        unique = list(dict.fromkeys(entry_ids))
        counts: dict[str, int] = {}
        # Chunked to stay under SQLITE_MAX_VARIABLE_NUMBER, as _values_for is.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT entry_id, COUNT(*) AS n FROM annotations "
                f"WHERE is_deleted = 0 AND entry_id IN ({placeholders}) GROUP BY entry_id",
                chunk,
            )
            for row in rows:
                counts[row["entry_id"]] = int(row["n"])
        return counts

    def count_entries(self, *, case_id: str | None = None) -> int:
        if case_id:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM log_entries WHERE case_id = ? AND is_deleted = 0",
                (case_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM log_entries WHERE is_deleted = 0"
            ).fetchone()
        return int(row["n"])

    def soft_delete(self, entry_id: str) -> None:
        """Evidence records are never hard-deleted; mark and re-queue for sync.

        Bumping local_revision matters as much as setting the flag. A deletion is a
        content change, so if it lands while that entry is mid-upload, the worker
        must not be able to mark the pre-deletion version as synced -- otherwise the
        deletion is silently dropped and the entry stays live for the whole team.
        See the rule above mark_synced.
        """
        with self._write_lock:
            self.conn.execute(
                "UPDATE log_entries SET is_deleted = 1, updated_at_utc = ?, sync_state = ?, "
                "local_revision = local_revision + 1 WHERE entry_id = ?",
                (to_iso(utc_now()), SyncState.PENDING.value, entry_id),
            )

    def restore(self, entry_id: str) -> None:
        """Undo a soft delete.

        The counterpart soft_delete has always needed. Without it the confirmation
        promising the entry is "kept, not erased" was true of the database and false
        of the application: the row was still there and the reviewer had no way to
        reach it ever again.

        Bumps local_revision for the same reason the deletion does -- it is a content
        change, so an upload already in flight must not be able to mark the deleted
        version as synced and leave the restore stranded locally.
        """
        with self._write_lock:
            self.conn.execute(
                "UPDATE log_entries SET is_deleted = 0, updated_at_utc = ?, sync_state = ?, "
                "local_revision = local_revision + 1 WHERE entry_id = ?",
                (to_iso(utc_now()), SyncState.PENDING.value, entry_id),
            )

    def deleted_count(self, case_id: str | None = None) -> int:
        """How many entries in this case are flagged deleted.

        Shown beside the Show deleted toggle, so a reviewer can tell at a glance
        whether there is anything hidden without having to turn it on and look.
        """
        if case_id:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM log_entries WHERE case_id = ? AND is_deleted = 1",
                (case_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM log_entries WHERE is_deleted = 1"
            ).fetchone()
        return int(row["n"])

    # -- sync bookkeeping --------------------------------------------------- #

    def list_pending(self, limit: int = 50) -> list[EntryRow]:
        rows = self.conn.execute(
            "SELECT * FROM log_entries WHERE sync_state IN (?, ?) "
            "ORDER BY created_at_utc ASC LIMIT ?",
            (SyncState.PENDING.value, SyncState.ERROR.value, int(limit)),
        ).fetchall()
        # Values are loaded with the rows, because what gets uploaded is the entry
        # and its answers together as one document.
        values = self._values_for([row["entry_id"] for row in rows])
        return [_row_to_entry(row, values.get(row["entry_id"])) for row in rows]

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM log_entries WHERE sync_state <> ?",
            (SyncState.SYNCED.value,),
        ).fetchone()
        return int(row["n"])

    # Revision rule: any statement that changes an entry's *content* must bump
    # local_revision, so an upload in flight cannot claim to have sent it. Pure
    # sync bookkeeping (mark_synced, mark_sync_error, requeue_all) must not.

    def mark_synced(
        self,
        entry_id: str,
        server_id: int | None,
        expected_revision: int | None,
    ) -> bool:
        """Mark the uploaded version of an entry as synced. Returns whether it was.

        ``expected_revision`` is required, and deliberately so. The sync worker
        snapshots pending rows, uploads them, then reports success; if the reviewer
        edits one of those entries during the upload, the version on the server is
        already stale. Clearing the pending flag by id alone would mean that edit is
        never sent -- silent data loss in a tool whose whole promise is not to lose
        an entry. Making the guard a required argument means no caller can omit it
        by accident.

        The guard is a revision counter rather than ``updated_at_utc`` because
        Windows resolves the wall clock to about 15 ms: an edit saved in the same
        tick as the snapshot carries a byte-identical timestamp, and a timestamp
        comparison would wave it through.

        Passing ``None`` marks nothing: the caller cannot say which version was
        uploaded, so the entry stays queued.
        """
        if expected_revision is None:
            log.warning("Refusing to mark %s synced without the uploaded revision", entry_id)
            return False

        with self._write_lock:
            cursor = self.conn.execute(
                "UPDATE log_entries SET sync_state = ?, sync_error = NULL, "
                "synced_at_utc = ?, server_id = ? "
                "WHERE entry_id = ? AND local_revision = ?",
                (
                    SyncState.SYNCED.value,
                    to_iso(utc_now()),
                    server_id,
                    entry_id,
                    int(expected_revision),
                ),
            )
        if cursor.rowcount == 0:
            log.info(
                "Entry %s changed during upload; leaving it queued for the next cycle",
                entry_id,
            )
        return cursor.rowcount > 0

    def mark_sync_error(self, entry_id: str, message: str) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE log_entries SET sync_state = ?, sync_error = ? WHERE entry_id = ?",
                (SyncState.ERROR.value, message[:500], entry_id),
            )

    def requeue_all(self) -> int:
        """Force a full re-upload; useful after pointing at a fresh server."""
        with self._write_lock:
            cursor = self.conn.execute(
                "UPDATE log_entries SET sync_state = ?, sync_error = NULL",
                (SyncState.PENDING.value,),
            )
        return cursor.rowcount

    def merge_remote(self, entries: Iterable[LogEntry]) -> int:
        """Merge entries pulled from the server, honouring last-write-wins.

        A local row that is newer than the incoming one is kept and left pending,
        so the local edit still wins on the next push.
        """
        merged = 0
        for remote in entries:
            existing = self.get(remote.entry_id)

            # Marks are merged whichever version of the entry wins, and one at a time
            # by id. Two investigators annotating the same event is ordinary -- far
            # more ordinary than two of them rewriting one observation -- and letting
            # the entry's timestamp decide would throw away a colleague's marks just
            # because this machine happened to edit the observation more recently.
            if existing is not None and remote.annotations:
                self.merge_annotations(remote.entry_id, remote.annotations)

            if existing is not None and existing.updated_at_utc > remote.updated_at_utc:
                continue
            row = EntryRow.model_validate(
                {
                    **remote.model_dump(),
                    "sync_state": SyncState.SYNCED,
                    "synced_at_utc": utc_now(),
                    "server_id": existing.server_id if existing else None,
                }
            )
            self.save(row)
            if existing is None and remote.annotations:
                # A new entry: its marks arrive with it, and the row has to exist
                # before they can point at it.
                self.merge_annotations(remote.entry_id, remote.annotations)
            merged += 1
        return merged

    # -- vocabularies and autofill memory ----------------------------------- #

    def distinct_field_values(self, field_id: str, case_id: str | None = None) -> list[str]:
        """Every value this field has held, for completions and the filter bar.

        Works for any field without being told which ones are allowed, replacing
        the hand-maintained allowlist that only ever covered Area and Status.
        """
        sql = (
            "SELECT DISTINCT v.value_text AS value FROM entry_values v "
            "JOIN log_entries e ON e.entry_id = v.entry_id "
            "WHERE v.field_id = ? AND v.value_text IS NOT NULL AND v.value_text <> '' "
            "AND e.is_deleted = 0"
        )
        params: list[Any] = [field_id]
        if case_id:
            sql += " AND e.case_id = ?"
            params.append(case_id)
        sql += " ORDER BY value COLLATE NOCASE"
        return [row["value"] for row in self.conn.execute(sql, params)]

    def distinct_role_values(
        self, role: FieldRole, template_id: str, case_id: str | None = None
    ) -> list[str]:
        """The same, addressed by what the field is for rather than by its id."""
        template = self.get_template(template_id)
        field = template.field_for_role(role) if template else None
        return self.distinct_field_values(field.field_id, case_id) if field else []

    def remember_area_for_folder(self, folder: str, area: str) -> None:
        if not folder or not area:
            return
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO folder_area_memory (folder, area, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(folder) DO UPDATE SET area=excluded.area, updated_at=excluded.updated_at",
                (folder.lower(), area, to_iso(utc_now())),
            )

    def recall_area_for_folder(self, folder: str) -> str | None:
        if not folder:
            return None
        row = self.conn.execute(
            "SELECT area FROM folder_area_memory WHERE folder = ?", (folder.lower(),)
        ).fetchone()
        return row["area"] if row else None

    # -- media hash cache --------------------------------------------------- #

    def get_cached_hash(self, media_path: str, size_bytes: int, mtime_ns: int) -> str | None:
        row = self.conn.execute(
            "SELECT sha256 FROM media_hash_cache WHERE media_path = ? AND size_bytes = ? "
            "AND mtime_ns = ?",
            (media_path, size_bytes, mtime_ns),
        ).fetchone()
        return row["sha256"] if row else None

    def put_cached_hash(self, media_path: str, size_bytes: int, mtime_ns: int, sha256: str) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO media_hash_cache "
                "(media_path, size_bytes, mtime_ns, sha256, hashed_at) VALUES (?, ?, ?, ?, ?)",
                (media_path, size_bytes, mtime_ns, sha256, to_iso(utc_now())),
            )

    # -- cases -------------------------------------------------------------- #

    def upsert_case(self, case: Case) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO cases (case_id, name, root_path, template_id, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET name=excluded.name, "
                # COALESCE so an upsert without a root_path keeps the stored one.
                "root_path=COALESCE(excluded.root_path, cases.root_path), "
                "template_id=COALESCE(excluded.template_id, cases.template_id)",
                (
                    case.case_id,
                    case.name,
                    case.root_path,
                    case.template_id,
                    to_iso(case.created_at_utc),
                ),
            )

    def list_cases(self) -> list[Case]:
        rows = self.conn.execute("SELECT * FROM cases ORDER BY created_at_utc DESC")
        return [_row_to_case(row) for row in rows]

    def get_case(self, case_id: str) -> Case | None:
        row = self.conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return _row_to_case(row) if row else None

    def template_for_case(self, case_id: str) -> LogTemplate:
        """The template new entries in this case start on.

        Falls back to the default rather than returning None: a case that has not
        chosen, or one first met by pulling it from the server, still has to open.
        """
        case = self.get_case(case_id)
        if case is not None and case.template_id:
            template = self.get_template(case.template_id)
            if template is not None:
                return template
        template = self.get_template(DEFAULT_TEMPLATE_ID)
        if template is None:  # pragma: no cover - seeded on open
            self.seed_builtin_templates()
            template = self.get_template(DEFAULT_TEMPLATE_ID)
        assert template is not None
        return template

    def set_case_template(self, case_id: str, template_id: str) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE cases SET template_id = ? WHERE case_id = ?", (template_id, case_id)
            )

    def case_entry_counts(self) -> dict[str, int]:
        """Live entry count per case, for showing beside each name in the picker."""
        rows = self.conn.execute(
            "SELECT case_id, COUNT(*) AS n FROM log_entries WHERE is_deleted = 0 GROUP BY case_id"
        )
        return {row["case_id"]: row["n"] for row in rows}

    def get_pull_cursor(self, case_id: str) -> str | None:
        """Where the last pull for this case reached, or None to start from the top."""
        row = self.conn.execute(
            "SELECT pull_cursor FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        return row["pull_cursor"] if row else None

    def set_pull_cursor(self, case_id: str, cursor: str | None) -> None:
        with self._write_lock:
            # A plain UPDATE would silently do nothing for a case that has no local
            # row yet -- a case first met by pulling it -- and that case would then
            # re-pull from the beginning forever. The id stands in as the name until
            # something that knows the real one upserts over it.
            self.conn.execute(
                "INSERT INTO cases (case_id, name, created_at_utc, pull_cursor) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET pull_cursor=excluded.pull_cursor",
                (case_id, case_id, to_iso(utc_now()), cursor),
            )

    # -- maintenance -------------------------------------------------------- #

    def stats(self) -> dict[str, int]:
        return {
            "entries": self.count_entries(),
            "pending": self.pending_count(),
            "cases": len(self.list_cases()),
        }
