"""CSV and XLSX export of log entries.

The position of the event in the media, then whatever the template asked, then the
provenance a reader needs to trust the row: the media path and its hash, the entry
id, the case, and when the row was created and last changed.

Columns are generated from the templates the entries were written on, so a field a
reviewer added is in the export without anything here being edited. The header a
field contributes is the field's own label.

Event Timestamp is a position *within the media*, not a wall-clock time, and Event
Ends At is derived from it and the duration rather than stored, so an export can be
read without the application that produced it.

Anything a spreadsheet would treat as a formula is prefixed before it is written,
so text a reviewer typed cannot execute when the file is opened.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from pathlib import Path

from .models import EntryRow
from .templates import FieldRole, LogTemplate, TemplateField
from .util import format_timecode, to_iso

log = logging.getLogger(__name__)

#: Prefix marking a column that comes from a template field, followed by its id.
FIELD_PREFIX = "field:"

#: Universal columns before the template's, in report order.
_LEADING_COLUMNS: tuple[tuple[str, str], ...] = (("File Name", "file_name"),)
_POSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Event Timestamp", "_event_timestamp"),
    ("Event Duration", "_event_duration"),
    ("Event Ends At", "_event_end"),
    ("Media Duration", "_media_duration"),
)
#: Universal columns after the template's.
_TRAILING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Investigator Name", "investigator_name"),
    ("Date Reviewed", "_date_reviewed"),
    ("Media Path", "media_path"),
    ("SHA-256", "media_sha256"),
    ("Snapshot", "snapshot_path"),
    ("Entry ID", "entry_id"),
    ("Case ID", "case_id"),
    ("Template", "_template"),
    ("Created (UTC)", "_created"),
    ("Updated (UTC)", "_updated"),
    ("Sync State", "_sync_state"),
)


def columns_for(templates: Sequence[LogTemplate]) -> list[tuple[str, str]]:
    """The report's columns for the templates the entries were written on.

    More than one template can appear in a single export -- a case whose template
    was changed has entries of both shapes -- so the fields are unioned rather than
    taken from one. Every answer therefore gets a column of its own: nothing is
    squeezed into a field that happens to share a position, and no row loses a
    value because it was written on the other form.

    The location field leads, beside the file name, matching the modal.
    """
    columns = list(_LEADING_COLUMNS)
    seen: set[str] = set()
    fields: list[TemplateField] = []
    for template in templates:
        for field in template.ordered_fields:
            if field.field_id in seen:
                continue
            seen.add(field.field_id)
            fields.append(field)

    headers = _disambiguate(fields, templates)
    for field in fields:
        if field.role is FieldRole.LOCATION:
            columns.append((headers[field.field_id], FIELD_PREFIX + field.field_id))
    columns.extend(_POSITION_COLUMNS)
    for field in fields:
        if field.role is not FieldRole.LOCATION:
            columns.append((headers[field.field_id], FIELD_PREFIX + field.field_id))
    return columns + list(_TRAILING_COLUMNS)


def _disambiguate(
    fields: Sequence[TemplateField], templates: Sequence[LogTemplate]
) -> dict[str, str]:
    """Header per field, qualified by template name where two labels collide.

    Two templates in one export will both have a "Status", and two columns under the
    same heading are unreadable -- the reader cannot tell which form a cell came from,
    and a spreadsheet filter on one silently ignores the other. Only the ambiguous
    ones are qualified, so a single-template export keeps its plain headers.
    """
    owner = {field.field_id: template.name for template in templates for field in template.fields}

    # Grouped by label, because the decision belongs to the label and not to each
    # field in turn. Qualifying one column by its template and the next by its field
    # name -- which is what happens if they are taken one at a time -- leaves a reader
    # comparing "Reading (Survey)" with "Reading (outdoor)" and none the wiser.
    sharing: dict[str, list[TemplateField]] = {}
    for field in fields:
        sharing.setdefault(field.label, []).append(field)

    headers: dict[str, str] = {}
    used: set[str] = set()
    for label, group in sharing.items():
        if len(group) == 1:
            chosen = {group[0].field_id: label}
        elif len({owner.get(field.field_id) for field in group}) > 1:
            # Different templates: the usual case, two forms that each have a Status.
            chosen = {
                field.field_id: f"{label} ({owner.get(field.field_id, '?')})" for field in group
            }
        else:
            # One template naming two of its own fields the same. The template name
            # cannot separate those, and a number would say nothing about which is
            # which, so the field's own name does the work.
            chosen = {field.field_id: f"{label} ({field.field_key})" for field in group}

        for field_id, heading in chosen.items():
            # Last resort, so a heading can never repeat: a spreadsheet with two
            # identical columns cannot be read.
            base, suffix = heading, 2
            while heading in used:
                heading = f"{base} {suffix}"
                suffix += 1
            used.add(heading)
            headers[field_id] = heading

    # Back into the order the columns are built in.
    return {field.field_id: headers[field.field_id] for field in fields}


#: Leading characters that make a spreadsheet treat a cell as a formula rather
#: than as text. Observations can arrive from other investigators through the
#: shared server, and exports are opened in Excel, so a cell beginning with one
#: of these must never be evaluated.
FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", chr(9), chr(13))


def is_formula_like(value: str) -> bool:
    return bool(value) and value.startswith(FORMULA_TRIGGERS)


def csv_safe(value: str) -> str:
    """Neutralise a formula-like cell for CSV.

    A leading apostrophe is the only defence CSV has, and it becomes part of the
    text a reader sees. The XLSX export instead marks the cell as text, so it
    keeps the observation byte-for-byte; prefer it when exact wording matters.
    """
    return f"'{value}" if is_formula_like(value) else value


#: Fill colours keyed by status, used in the XLSX export.
STATUS_COLOURS: dict[str, str] = {
    "Debunked": "FFC8E6C9",
    "Unexplained": "FFFFE0B2",
    "Corroborated": "FFBBDEFB",
    "Needs Review": "FFF5F5F5",
    "Inconclusive": "FFE1BEE7",
    "Equipment Artifact": "FFD7CCC8",
    "Contamination": "FFFFCDD2",
}


def _value(entry: EntryRow, key: str, fields: dict[str, TemplateField]) -> str:
    if key.startswith(FIELD_PREFIX):
        field_id = key[len(FIELD_PREFIX) :]
        field = fields.get(field_id)
        value = entry.values.get(field_id)
        if field is not None:
            return field.display(value)
        # A value whose field this client has never seen. Shown as text rather than
        # left out, because it is still somebody's answer.
        return "" if value is None else str(value)

    if not key.startswith("_"):
        return str(getattr(entry, key) or "")

    if key == "_template":
        return entry.template_id
    if key == "_event_timestamp":
        # Where in the media the event is, so a reader can find it again. Empty
        # when there is none - a still image has no position - because a literal
        # "N/A" in a timecode column is a magic string that breaks sorting and
        # filtering in the spreadsheet this is opened in. The duration branch
        # directly below has always returned "" for the same situation.
        return (
            format_timecode(entry.event_offset_seconds)
            if entry.event_offset_seconds is not None
            else ""
        )
    if key == "_event_duration":
        return format_timecode(entry.event_duration_seconds) if entry.event_duration_seconds else ""
    if key == "_event_end":
        end = entry.event_end_seconds
        return format_timecode(end) if end is not None else ""
    if key == "_media_duration":
        return (
            format_timecode(entry.media_duration_seconds, millis=False)
            if entry.media_duration_seconds
            else ""
        )
    if key == "_date_reviewed":
        return entry.date_reviewed.isoformat()
    if key == "_created":
        return to_iso(entry.created_at_utc) or ""
    if key == "_updated":
        return to_iso(entry.updated_at_utc) or ""
    if key == "_sync_state":
        return entry.sync_state.value
    return ""


def _pale(hex_colour: str) -> str | None:
    """Blend a UI colour towards white into an ARGB fill a spreadsheet can read.

    The screen palette is tuned for dark text on a dark background and is far too
    saturated behind black cells in Excel. Blending keeps a template's own status
    colours recognisable in the export instead of dropping them, which is what
    happened to every status outside the hardcoded seven.
    """
    text = hex_colour.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        channels = [int(text[index : index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        return None
    blended = [round(channel + (255 - channel) * 0.72) for channel in channels]
    return "FF" + "".join(f"{channel:02X}" for channel in blended)


def _status_fill(entry: EntryRow, fields: dict[str, TemplateField]) -> str | None:
    """The fill for this entry's status cell, from the template's own option colour.

    Only the status-role field is filled in the workbook. Shading every coloured
    choice would turn a report into a paint chart, and the status is the one a reader
    scans down.
    """
    for field_id, field in fields.items():
        if field.role is not FieldRole.STATUS:
            continue
        value = entry.values.get(field_id)
        if value is None:
            continue
        text = str(value)
        # The curated spreadsheet palette wins where it has an answer, so existing
        # paranormal exports look exactly as they did.
        known = STATUS_COLOURS.get(text)
        if known:
            return known
        option = field.option_for(text)
        if option is not None and option.colour:
            return _pale(option.colour)
    return None


def _field_index(templates: Sequence[LogTemplate]) -> dict[str, TemplateField]:
    return {field.field_id: field for template in templates for field in template.fields}


def _rows(
    entries: Sequence[EntryRow],
    columns: Sequence[tuple[str, str]],
    fields: dict[str, TemplateField],
) -> list[list[str]]:
    return [[_value(entry, key, fields) for _, key in columns] for entry in entries]


def export_csv(
    entries: Sequence[EntryRow], target: str | Path, templates: Sequence[LogTemplate]
) -> Path:
    """Write a UTF-8 CSV with a BOM, so Excel opens it with the right encoding."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = columns_for(templates)
    fields = _field_index(templates)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([header for header, _ in columns])
        writer.writerows(
            [csv_safe(cell) for cell in row] for row in _rows(entries, columns, fields)
        )
    log.info("Exported %d entries to %s", len(entries), target)
    return target


def export_xlsx(
    entries: Sequence[EntryRow], target: str | Path, templates: Sequence[LogTemplate]
) -> Path:
    """Write a formatted workbook: frozen header, sized columns, status colouring."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Evidence Log"

    columns = columns_for(templates)
    fields = _field_index(templates)
    headers = [header for header, _ in columns]
    sheet.append(headers)

    header_font = Font(bold=True, color="FFFFFFFF")
    header_fill = PatternFill("solid", fgColor="FF37474F")
    for index in range(1, len(headers) + 1):
        cell = sheet.cell(row=1, column=index)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", horizontal="left")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    # Found by role and type rather than by header text. The old code looked up
    # "Status" and "What Was Heard or Seen" by name, so a template that calls them
    # anything else would have raised ValueError mid-export.
    status_fields = {field.field_id for field in fields.values() if field.role is FieldRole.STATUS}
    status_index = next(
        (
            index
            for index, (_, key) in enumerate(columns, start=1)
            if key.removeprefix(FIELD_PREFIX) in status_fields
        ),
        None,
    )
    snapshot_index = headers.index("Snapshot") + 1
    wrap_indices = {
        index
        for index, (_, key) in enumerate(columns, start=1)
        if (field := fields.get(key.removeprefix(FIELD_PREFIX))) is not None
        and field.field_type.is_prose
    }

    for offset, entry in enumerate(entries):
        row_number = offset + 2
        for column, (_, key) in enumerate(columns, start=1):
            text = _value(entry, key, fields)
            cell = sheet.cell(row=row_number, column=column, value=text)
            # openpyxl infers a formula from a leading "="; force text so the
            # observation is preserved exactly and never evaluated.
            if is_formula_like(text):
                cell.data_type = "s"
            cell.alignment = Alignment(
                vertical="top", wrap_text=column in wrap_indices, horizontal="left"
            )

        if status_index is not None:
            colour = _status_fill(entry, fields)
            if colour:
                sheet.cell(row=row_number, column=status_index).fill = PatternFill(
                    "solid", fgColor=colour
                )
        if entry.snapshot_path and Path(entry.snapshot_path).exists():
            cell = sheet.cell(row=row_number, column=snapshot_index)
            cell.value = Path(entry.snapshot_path).name
            cell.hyperlink = Path(entry.snapshot_path).as_uri()
            cell.style = "Hyperlink"

    # Size columns from content, within sensible bounds.
    for column, header in enumerate(headers, start=1):
        widest = len(header)
        for offset in range(len(entries)):
            value = sheet.cell(row=offset + 2, column=column).value
            if value:
                widest = max(widest, min(len(str(value)), 60))
        sheet.column_dimensions[get_column_letter(column)].width = min(max(widest + 2, 10), 62)

    workbook.save(target)
    log.info("Exported %d entries to %s", len(entries), target)
    return target


def export(
    entries: Sequence[EntryRow], target: str | Path, templates: Sequence[LogTemplate]
) -> Path:
    """Dispatch on the target's extension.

    ``templates`` has no default on purpose. Every answer an entry holds reaches the
    file through its field definition, so a caller that omitted them would write a
    spreadsheet with the timestamps and none of the observations -- and would have no
    way to tell. Making it required turns that into a TypeError at the call site.
    """
    target = Path(target)
    if target.suffix.lower() in (".xlsx", ".xlsm"):
        return export_xlsx(entries, target, templates)
    return export_csv(entries, target, templates)
