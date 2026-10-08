"""Exports are the deliverable a reader outside the team sees.

What matters most here is that nothing a reviewer typed can execute when the file
is opened, and that a timecode survives the round trip into a spreadsheet."""

from __future__ import annotations

import csv

import pytest

from evidence_review.builtin_templates import PARANORMAL_TEMPLATE
from evidence_review.export import columns_for, export, export_csv, export_xlsx

from .conftest import OBSERVATION, make_entry

#: Every export call needs the templates its entries were written on;
#: these tests all use the paranormal one.
TEMPLATES = [PARANORMAL_TEMPLATE]


def read_csv(path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    return list(reader.fieldnames or []), rows


def test_csv_has_every_requested_field(tmp_path, sample_entry) -> None:
    target = export_csv([sample_entry], tmp_path / "log.csv", TEMPLATES)
    headers, rows = read_csv(target)

    for required in (
        "File Name",
        "Area",
        "Event Timestamp",
        "Event Duration",
        "What Was Heard or Seen",
        "Debunk Reasoning",
        "Status",
        "Investigator Name",
        "Date Reviewed",
    ):
        assert required in headers, f"{required} is missing from the export"

    assert len(rows) == 1
    assert rows[0]["File Name"] == "Basement_Cam2.mp4"
    assert rows[0]["Area"] == "Basement"
    assert rows[0]["Event Timestamp"] == "00:01:23.400"
    assert rows[0]["Event Duration"] == "00:00:03.700"
    assert rows[0]["Event Ends At"] == "00:01:27.100"
    assert rows[0]["Status"] == "Unexplained"
    assert rows[0]["Investigator Name"] == "Jane Doe"
    assert rows[0]["Date Reviewed"] == "2026-09-15"


def test_still_images_have_no_position(tmp_path) -> None:
    from evidence_review.models import MediaKind

    entry = make_entry(media_kind=MediaKind.IMAGE, event_offset_seconds=None, file_name="a.jpg")
    _headers, rows = read_csv(export_csv([entry], tmp_path / "log.csv", TEMPLATES))
    # Empty, not "N/A": a magic string in a timecode column breaks sorting and
    # filtering in the spreadsheet this gets opened in, and the duration column
    # beside it has always left the cell empty for exactly this situation.
    assert rows[0]["Event Timestamp"] == ""
    assert rows[0]["Event Duration"] == "", "the two must agree about having no value"


def test_an_event_without_a_duration_exports_cleanly(tmp_path) -> None:
    entry = make_entry(event_offset_seconds=83.4, event_duration_seconds=None)
    _headers, rows = read_csv(export_csv([entry], tmp_path / "log.csv", TEMPLATES))
    assert rows[0]["Event Timestamp"] == "00:01:23.400"
    assert rows[0]["Event Duration"] == ""
    assert rows[0]["Event Ends At"] == ""


def test_empty_export_still_writes_headers(tmp_path) -> None:
    headers, rows = read_csv(export_csv([], tmp_path / "empty.csv", TEMPLATES))
    assert headers == [header for header, _ in columns_for([PARANORMAL_TEMPLATE])]
    assert rows == []


def test_multiline_observations_survive_the_round_trip(tmp_path) -> None:
    text = 'Two knocks.\nThen a third, quieter one.\nPossible "tapping" on glass.'
    entry = make_entry(values={OBSERVATION: text})
    _headers, rows = read_csv(export_csv([entry], tmp_path / "log.csv", TEMPLATES))
    assert rows[0]["What Was Heard or Seen"] == text


def test_csv_uses_a_bom_so_excel_reads_utf8(tmp_path) -> None:
    entry = make_entry(values={OBSERVATION: "Voice said “hello” — clearly"})
    target = export_csv([entry], tmp_path / "log.csv", TEMPLATES)
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_xlsx_is_written_and_readable(tmp_path, sample_entry) -> None:
    openpyxl = pytest.importorskip("openpyxl")

    target = export_xlsx([sample_entry], tmp_path / "log.xlsx", TEMPLATES)
    workbook = openpyxl.load_workbook(target)
    sheet = workbook["Evidence Log"]

    headers = [cell.value for cell in sheet[1]]
    assert "What Was Heard or Seen" in headers
    assert sheet.freeze_panes == "A2"

    row = {headers[index]: cell.value for index, cell in enumerate(sheet[2])}
    assert row["File Name"] == "Basement_Cam2.mp4"
    assert row["Status"] == "Unexplained"


def test_export_dispatches_on_extension(tmp_path, sample_entry) -> None:
    pytest.importorskip("openpyxl")
    assert export([sample_entry], tmp_path / "a.csv", TEMPLATES).suffix == ".csv"
    assert export([sample_entry], tmp_path / "a.xlsx", TEMPLATES).suffix == ".xlsx"


def test_export_creates_missing_directories(tmp_path, sample_entry) -> None:
    target = export_csv([sample_entry], tmp_path / "nested" / "deeper" / "log.csv", TEMPLATES)
    assert target.is_file()


def test_two_fields_with_the_same_label_get_different_headings() -> None:
    """Nothing stops a reviewer naming two fields "Reading". Two columns under one
    heading make a spreadsheet unreadable -- nobody can tell which is which.

    The template name does not separate them, since both are on the same template, so
    the field's own name is used.
    """
    from evidence_review.templates import FieldType, LogTemplate, TemplateField

    template = LogTemplate(
        template_id="t",
        name="Survey",
        fields=[
            TemplateField(
                field_id="a",
                field_key="indoor",
                label="Reading",
                field_type=FieldType.NUMBER,
                position=0,
            ),
            TemplateField(
                field_id="b",
                field_key="outdoor",
                label="Reading",
                field_type=FieldType.NUMBER,
                position=1,
            ),
        ],
    )
    headings = [heading for heading, _ in columns_for([template])]
    readings = [heading for heading in headings if heading.startswith("Reading")]

    assert len(readings) == 2, "both fields need a column"
    assert len(headings) == len(set(headings)), "no heading may repeat anywhere"
    # Named, not numbered. "Reading 2" tells the reader nothing about which is which;
    # the field's own name is the only thing to hand that does.
    assert set(readings) == {"Reading (indoor)", "Reading (outdoor)"}, readings


def test_a_single_template_keeps_plain_headings() -> None:
    """Qualifying is only for a collision. An ordinary export must not be cluttered."""
    headings = [heading for heading, _ in columns_for([PARANORMAL_TEMPLATE])]
    assert "Status" in headings
    assert "What Was Heard or Seen" in headings
    assert not any("(" in heading for heading in headings if heading.startswith("Status"))
