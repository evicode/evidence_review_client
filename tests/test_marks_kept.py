"""Marks on the timeline: kept until logged, movable by their ends, shown in the entry view.

Phase 2 of docs/PLAN_UX_OVERHAUL.md (2026-10-07):

* marks tagged while watching used to live in memory only, and vanished when
  another file was opened or the app closed;
* a logged event's timing could only be changed by retyping it in the edit form;
* the entry view showed the frame without the marks drawn on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from evidence_review.annotations import Annotation, AnnotationKind
from evidence_review.autofill import MediaContext
from evidence_review.models import SyncState
from evidence_review.ui.entry_detail import EntryDetailDialog
from evidence_review.ui.marker_bar import EventMark, MarkerBar

from .conftest import OBSERVATION, make_entry

MEDIA = r"C:\evidence\Cam4.mkv"


# --------------------------------------------------------------------------- #
# The bar: dragging an end
# --------------------------------------------------------------------------- #


@pytest.fixture
def bar(qt_app) -> MarkerBar:
    widget = MarkerBar()
    widget.resize(1000, 20)  # 10 px per second over a 100 s file
    widget.set_duration(100.0)
    widget.set_marks([EventMark(start=20.0, end=30.0, entry_id="e1")])
    widget.show()
    return widget


def drag(bar: MarkerBar, from_x: int, to_x: int) -> None:
    QTest.mousePress(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(from_x, 10))
    QTest.mouseMove(bar, QPoint((from_x + to_x) // 2, 10))
    QTest.mouseMove(bar, QPoint(to_x, 10))
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(to_x, 10))


def test_dragging_the_end_of_an_event_changes_its_end(bar: MarkerBar) -> None:
    got: list[tuple[int, float, float]] = []
    bar.mark_resized.connect(lambda i, s, e: got.append((i, s, e)))
    drag(bar, 300, 350)
    assert got and got[0][0] == 0
    assert got[0][1] == pytest.approx(20.0) and got[0][2] == pytest.approx(35.0, abs=0.2)


def test_dragging_the_start_never_passes_the_end(bar: MarkerBar) -> None:
    got: list[tuple[int, float, float]] = []
    bar.mark_resized.connect(lambda i, s, e: got.append((i, s, e)))
    drag(bar, 200, 400)
    assert got[0][1] <= got[0][2] == pytest.approx(30.0)


def test_a_click_on_an_end_selects_without_moving(bar: MarkerBar) -> None:
    moved: list[object] = []
    selected: list[int] = []
    bar.mark_resized.connect(lambda *a: moved.append(a))
    bar.mark_activated.connect(selected.append)
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 10))
    assert selected == [0] and moved == []


# --------------------------------------------------------------------------- #
# The store: unlogged marks are kept per file
# --------------------------------------------------------------------------- #


def test_unlogged_marks_are_kept_per_file(store) -> None:
    a = store.save_pending_mark(MEDIA, 10.0, 12.0)
    store.save_pending_mark(r"C:\evidence\Other.wav", 1.0, None)
    assert store.pending_marks_for(MEDIA.lower()) == [(a, 10.0, 12.0)], "matched regardless of capitals"
    store.save_pending_mark(MEDIA, 11.0, 13.0, a)
    assert store.pending_marks_for(MEDIA) == [(a, 11.0, 13.0)]
    store.delete_pending_mark(a)
    assert store.pending_marks_for(MEDIA) == []


# --------------------------------------------------------------------------- #
# The main window
# --------------------------------------------------------------------------- #


def open_without_player(window) -> None:
    """Point the window at a file the way load_media does, without needing mpv."""
    window._media_context = MediaContext(path=Path(MEDIA), kind="video", duration_seconds=100.0)


def test_a_new_mark_is_kept_and_discarding_forgets_it(window) -> None:
    open_without_player(window)
    mark = EventMark(start=5.0, end=8.0)
    window._keep_pending(mark)
    window._refresh_marks()
    assert window._store.pending_marks_for(MEDIA) == [(mark.mark_id, 5.0, 8.0)]
    window._discard_mark(window._index_of(mark))
    assert window._store.pending_marks_for(MEDIA) == []
    assert mark not in window._pending_marks


def test_dragging_a_logged_event_saves_a_new_version_of_its_entry(window) -> None:
    open_without_player(window)
    entry = make_entry(case_id=window._settings.review.case_id, media_path=MEDIA,
                       event_offset_seconds=20.0, event_duration_seconds=10.0)
    entry.sync_state = SyncState.SYNCED
    window._store.save(entry)
    window._refresh_marks()
    index = next(i for i, m in enumerate(window._marks) if m.entry_id == entry.entry_id)
    window._on_mark_resized(index, 18.0, 33.0)
    stored = window._store.get(entry.entry_id)
    assert stored.event_offset_seconds == pytest.approx(18.0)
    assert stored.event_duration_seconds == pytest.approx(15.0)
    assert stored.sync_state is SyncState.PENDING, "the new timing goes to the server"


# --------------------------------------------------------------------------- #
# The entry view shows the marks
# --------------------------------------------------------------------------- #


def test_the_entry_view_lists_the_marks_on_its_frame(qt_app, store, tmp_path) -> None:
    from evidence_review.config import Settings

    frame = tmp_path / "frame.png"
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("#334455"))
    image.save(str(frame))
    entry = make_entry(values={OBSERVATION: "Knock"}, snapshot_path=str(frame))
    store.save(entry)
    store.save_annotations(entry.entry_id, [
        Annotation(entry_id=entry.entry_id, kind=AnnotationKind.ELLIPSE, x1=0.2, y1=0.2, x2=0.5, y2=0.6,
                   text="<b>Figure</b> here", start_seconds=0, end_seconds=5),
    ])
    dialog = EntryDetailDialog(store.get(entry.entry_id), store=store, settings=Settings(), commit=lambda e: None)
    from PySide6.QtWidgets import QLabel

    texts = [label.text() for label in dialog.findChildren(QLabel)]
    line = next((t for t in texts if "Ellipse" in t), "")
    assert "&lt;b&gt;Figure&lt;/b&gt; here" in line, "listed, with its words escaped"
    dialog.deleteLater()
