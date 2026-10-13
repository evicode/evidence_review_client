"""The Files and Session Log panels.

What these guard is mostly honesty: a list that was cut short must not look
complete, and a count must not mean two different things.
"""

from __future__ import annotations

from .conftest import OBSERVATION


def test_a_truncated_scan_says_so(qt_app, tmp_path, monkeypatch) -> None:
    """The dock stopped at a ceiling and only wrote a log line, presenting a
    partial list as the whole folder. In an evidence tool a file that was never
    listed is a file that was never reviewed."""
    from evidence_review.ui import playlist_dock as dock_module
    from evidence_review.ui.playlist_dock import PlaylistDock

    for index in range(6):
        (tmp_path / f"clip{index}.mp4").write_bytes(b"x")

    monkeypatch.setattr(dock_module, "MAX_FILES", 3)

    dock = PlaylistDock()
    try:
        dock.open_folder(tmp_path)
        # isVisible() is False while the parent was never shown, so ask the
        # question that actually matters: is the warning on the label.
        assert "too large" in dock.scan_warning_label.text()
        assert not dock.scan_warning_label.isHidden()
    finally:
        dock.deleteLater()


def test_an_unreadable_folder_says_so(qt_app, tmp_path, monkeypatch) -> None:
    """An OSError during the walk only produced a log line, so an unreadable
    folder looked exactly like an empty one."""
    from pathlib import Path

    from evidence_review.ui.playlist_dock import PlaylistDock

    def explode(_self, _pattern):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "glob", explode)

    dock = PlaylistDock()
    try:
        dock.open_folder(tmp_path)
        assert "could not be read" in dock.scan_warning_label.text()
        assert "Permission denied" in dock.scan_warning_label.text()
    finally:
        dock.deleteLater()


def test_a_clean_scan_shows_no_warning(qt_app, tmp_path) -> None:
    """The warning must stay out of the way when nothing went wrong."""
    from evidence_review.ui.playlist_dock import PlaylistDock

    (tmp_path / "clip.mp4").write_bytes(b"x")

    dock = PlaylistDock()
    try:
        dock.open_folder(tmp_path)
        assert dock.scan_warning_label.text() == ""
        assert dock.scan_warning_label.isHidden()
    finally:
        dock.deleteLater()


def test_an_empty_folder_explains_itself(qt_app, tmp_path) -> None:
    """ "0 files · 0 B" said nothing about why, with the answer — the subfolders
    box — sitting directly above it."""
    from evidence_review.ui.playlist_dock import PlaylistDock

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "clip.mp4").write_bytes(b"x")

    dock = PlaylistDock()
    try:
        dock.recursive_check.setChecked(False)
        dock.open_folder(tmp_path)
        assert "No media here" in dock.summary_label.text()
        assert "subfolders" in dock.summary_label.text()
    finally:
        dock.deleteLater()


def test_an_empty_log_says_what_to_do(window) -> None:
    """ "0 entries" looked identical whether the case was empty or a filter had
    hidden everything, and said nothing about how to add one."""
    from .conftest import make_entry

    window.switch_case("willow-house", "Willow House")
    window.refresh_log()
    assert "press LOG" in window.log_dock.count_label.text()

    window._store.save(make_entry(case_id="willow-house", values={OBSERVATION: "Knocks."}))
    window.refresh_log()
    assert "1 entry" in window.log_dock.count_label.text()

    window.log_dock.search_edit.setText("nothing matches this")
    window.refresh_log()
    assert "filter" in window.log_dock.count_label.text().lower()


def test_the_two_sync_vocabularies_do_not_collide(qt_app) -> None:
    """The log table and the status bar sit a few centimetres apart. ↻ meant
    "waiting to upload" in one and "a cycle is running now" in the other."""
    from evidence_review.models import SyncState
    from evidence_review.ui.log_dock import SYNC_GLYPHS
    from evidence_review.ui.main_window import SYNC_INDICATORS

    bar = {glyph for glyph, _colour in SYNC_INDICATORS.values()}
    rows = {glyph for glyph, _colour, _tip in SYNC_GLYPHS.values()}

    # ↻ belongs to the bar alone: a row is a thing, not an activity.
    assert "↻" in bar
    assert "↻" not in rows

    # Where both use a glyph it has to mean the same kind of thing.
    assert SYNC_GLYPHS[SyncState.SYNCED][0] == SYNC_INDICATORS["idle"][0]


def test_custom_statuses_get_their_own_colour(qt_app) -> None:
    """Settings presents adding statuses as first-class, but every unknown one came
    back the same muted grey, so a case using custom statuses lost the colour
    coding that makes the log readable at a glance."""
    from evidence_review.ui.theme import STATUS_COLOURS, TEXT_MUTED, status_colour

    for known, colour in STATUS_COLOURS.items():
        assert status_colour(known) == colour, "a known status keeps its colour"

    invented = ["Possible Draught", "Pareidolia", "Staff Movement", "Animal"]
    colours = [status_colour(s) for s in invented]
    assert all(c != TEXT_MUTED for c in colours), "a custom status must not be grey"

    # Stable between calls, or the log would change colour as you scrolled.
    assert colours == [status_colour(s) for s in invented]
    assert status_colour("") == TEXT_MUTED


def test_a_playback_failure_stays_attached_to_the_file(window, tmp_path) -> None:
    """It used to be an eight-second status-bar message and a log line. The
    question - which of these two hundred files could I not review? - gets asked
    at the end, by which time the message is long gone."""
    from evidence_review.autofill import MediaContext
    from evidence_review.ui.playlist_dock import FAILED_GLYPH

    broken = tmp_path / "corrupt.mp4"
    broken.write_bytes(b"not a video")
    (tmp_path / "fine.mp4").write_bytes(b"also not a video, but untried")

    window.playlist_dock.open_folder(tmp_path)
    window._media_context = MediaContext(path=broken, kind="video")

    window._on_backend_error("Could not decode: unsupported codec")

    # Asserted on what the reviewer can actually see, not on internal state.
    listed = {}
    for index in range(window.playlist_dock.list_widget.count()):
        item = window.playlist_dock.list_widget.item(index)
        listed[item.text()] = item.toolTip()

    failed = [text for text in listed if text.startswith(FAILED_GLYPH)]
    assert failed == [f"{FAILED_GLYPH}  corrupt.mp4"]
    assert "unsupported codec" in listed[failed[0]], "the reason has to be readable"
    assert any(text.startswith("▶") and "fine.mp4" in text for text in listed)
    assert "would not play" in window.playlist_dock.summary_label.text()


def test_a_clean_folder_mentions_no_failures(window, tmp_path) -> None:
    from evidence_review.ui.playlist_dock import FAILED_GLYPH

    (tmp_path / "fine.mp4").write_bytes(b"x")
    window.playlist_dock.open_folder(tmp_path)
    assert "would not play" not in window.playlist_dock.summary_label.text()
    texts = [
        window.playlist_dock.list_widget.item(i).text()
        for i in range(window.playlist_dock.list_widget.count())
    ]
    assert not any(text.startswith(FAILED_GLYPH) for text in texts)


def test_one_rendering_for_a_field_with_no_value(qt_app, tmp_path) -> None:
    """Three spellings of "nothing here" could be on screen at once for the same
    still image: --:--:-- in the transport, N/A in the LOG modal, — in the table.

    --:--:-- survives, because it is a different thing: a timecode with no
    reading, shaped like the value it will hold. A *field* with no value is
    NO_VALUE everywhere.
    """
    from evidence_review.autofill import MediaContext
    from evidence_review.builtin_templates import PARANORMAL_TEMPLATE
    from evidence_review.config import Settings
    from evidence_review.store import LocalStore
    from evidence_review.ui.log_dialog import LogEntryDialog
    from evidence_review.ui.log_dock import LogTableModel
    from evidence_review.util import NO_VALUE

    from .conftest import make_entry

    # The table, for an entry with no position and no duration.
    model = LogTableModel()
    model.set_template(PARANORMAL_TEMPLATE, {PARANORMAL_TEMPLATE.template_id: PARANORMAL_TEMPLATE})
    model.set_entries([make_entry(event_offset_seconds=None, event_duration_seconds=None)])
    position = next(c for c in model.columns if c.kind == "offset")
    duration = next(c for c in model.columns if c.kind == "duration")
    assert model._display(model.entries[0], position) == NO_VALUE
    assert model._display(model.entries[0], duration) == NO_VALUE

    # The modal, for a still image.
    still = tmp_path / "photo.jpg"
    still.write_bytes(b"x")
    store = LocalStore(tmp_path / "evidence.db")
    try:
        dialog = LogEntryDialog(
            context=MediaContext(path=still, kind="image"),
            settings=Settings(),
            store=store,
            event_offset_seconds=None,
        )
        try:
            assert dialog.media_length_label.text() == NO_VALUE
            assert dialog.offset_edit.placeholderText() == NO_VALUE
            assert "N/A" not in dialog.media_length_label.text()
        finally:
            dialog.deleteLater()
    finally:
        store.close()


def test_the_timecode_placeholder_is_not_the_no_value_mark(qt_app) -> None:
    """A clock with no reading keeps the width of the timecode it stands in for,
    so it stays --:--:-- rather than collapsing to a dash."""
    from evidence_review.ui.transport_bar import TransportBar
    from evidence_review.util import NO_VALUE, format_timecode

    assert format_timecode(None) == "--:--:--"
    assert format_timecode(None) != NO_VALUE

    bar = TransportBar()
    try:
        bar.set_media_loaded(False)
        assert bar.time_label.text() == "--:--:-- / --:--:--"
    finally:
        bar.deleteLater()
