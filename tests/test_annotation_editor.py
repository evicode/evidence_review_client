"""Drawing marks on a frame, driven through real mouse events.

The thing that can actually be wrong here is the mapping between where the reviewer
clicks and where the mark lands on the picture. The frame is letterboxed inside the
widget, so a click is not at the same place in the widget as it is in the picture, and
an error there puts every circle somewhere other than the thing it was drawn around --
without anything failing.

So these press and drag the mouse rather than calling the model directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from evidence_review.annotations import Annotation, AnnotationKind
from evidence_review.ui.annotation_editor import AnnotationEditor

from .conftest import make_entry


@pytest.fixture
def frame() -> QImage:
    """A 16:9 frame, so there is real letterboxing to get wrong."""
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(QColor("#334455"))
    return image


def open_editor(qt_app, entry, frame: QImage, annotations=None) -> AnnotationEditor:
    """A shown editor, sized so the picture has somewhere to be letterboxed."""
    dialog = AnnotationEditor(entry, frame=frame, annotations=annotations)
    dialog.resize(1040, 700)
    dialog.show()
    qt_app.processEvents()
    return dialog


@pytest.fixture
def editor(qt_app, frame: QImage):
    """Closed again afterwards.

    qt_app is session-scoped, so a dialog left shown outlives its test and goes on
    taking keystrokes. That is not hypothetical: leaving these open broke a shortcut
    test two files later, which passed on its own and failed in the full run.
    """
    entry = make_entry(
        media_path=r"C:\ev\Cam4.mkv",
        event_offset_seconds=23.9,
        event_duration_seconds=4.0,
    )
    dialog = open_editor(qt_app, entry, frame)
    yield dialog
    dialog.close()
    dialog.deleteLater()
    qt_app.processEvents()


def at(editor: AnnotationEditor, fx: float, fy: float) -> QPoint:
    """The point in the widget corresponding to a fraction of the picture."""
    rect = editor.canvas.video_rect()
    return QPoint(
        int(rect.left() + fx * rect.width()), int(rect.top() + fy * rect.height())
    )


def drag(editor: AnnotationEditor, start: tuple[float, float], end: tuple[float, float]) -> None:
    canvas = editor.canvas
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     at(editor, *start))
    QTest.mouseMove(canvas, at(editor, *end))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       at(editor, *end))


def pick_tool(editor: AnnotationEditor, label: str) -> None:
    from evidence_review.ui.annotation_editor import TOOLS

    index = next(i for i, (name, _) in enumerate(TOOLS) if name == label)
    editor._tool_group.button(index).click()


# --------------------------------------------------------------------------- #


def test_the_picture_is_letterboxed_not_stretched(editor: AnnotationEditor) -> None:
    """A stretched frame would put every mark in the wrong place and look fine."""
    rect = editor.canvas.video_rect()
    assert rect.width() / rect.height() == pytest.approx(16 / 9, abs=0.01)


def test_a_dragged_mark_lands_where_the_mouse_went(editor: AnnotationEditor) -> None:
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))

    (mark,) = editor.canvas.annotations()
    assert mark.kind is AnnotationKind.ELLIPSE
    assert (mark.x1, mark.y1) == pytest.approx((0.25, 0.30), abs=0.02)
    assert (mark.x2, mark.y2) == pytest.approx((0.55, 0.70), abs=0.02)


def test_a_new_mark_is_selected_so_the_panel_acts_on_it(editor: AnnotationEditor) -> None:
    pick_tool(editor, "Box")
    drag(editor, (0.2, 0.2), (0.4, 0.4))
    assert editor.canvas.selected() is not None
    assert editor.text_edit.isEnabled()


def test_typing_a_label_reaches_the_mark_and_the_list(editor: AnnotationEditor) -> None:
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))

    editor.text_edit.setPlainText("Figure in doorway")

    assert editor.canvas.selected().text == "Figure in doorway"
    assert "Figure in doorway" in editor.list_widget.item(0).text()


def test_the_select_tool_moves_a_mark_without_resizing_it(editor: AnnotationEditor) -> None:
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))
    before = editor.canvas.selected()

    pick_tool(editor, "Select")
    drag(editor, (0.40, 0.50), (0.50, 0.50))

    after = editor.canvas.selected()
    assert after.x1 - before.x1 == pytest.approx(0.10, abs=0.02)
    assert (after.x2 - after.x1) == pytest.approx(before.x2 - before.x1, abs=0.005)


def test_the_select_tool_never_draws_a_new_mark(editor: AnnotationEditor) -> None:
    """It used to be easy to leave a stray zero-sized mark behind every time somebody
    clicked the picture to select something."""
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))

    pick_tool(editor, "Select")
    drag(editor, (0.10, 0.10), (0.15, 0.15))

    assert len(editor.canvas.annotations()) == 1


def test_clicking_a_mark_selects_that_mark(editor: AnnotationEditor) -> None:
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))
    pick_tool(editor, "Arrow")
    drag(editor, (0.80, 0.15), (0.68, 0.35))

    pick_tool(editor, "Select")
    QTest.mouseClick(editor.canvas, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, at(editor, 0.40, 0.50))

    assert editor.canvas.selected().kind is AnnotationKind.ELLIPSE


def test_a_plain_click_leaves_a_mark_big_enough_to_see(editor: AnnotationEditor) -> None:
    """A click without a drag used to leave a zero-sized mark: invisible, and
    impossible to select again to get rid of."""
    pick_tool(editor, "Circle")
    QTest.mouseClick(editor.canvas, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, at(editor, 0.5, 0.5))

    (mark,) = editor.canvas.annotations()
    assert abs(mark.x2 - mark.x1) > 0.02
    assert abs(mark.y2 - mark.y1) > 0.02


# --------------------------------------------------------------------------- #
# The time window
# --------------------------------------------------------------------------- #


def test_a_mark_with_no_window_takes_the_events_own_span(editor: AnnotationEditor) -> None:
    """Without this a mark saved straight after drawing sits at 0..0 and is never
    drawn -- the reviewer's work silently amounting to nothing."""
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))

    editor._save()

    (saved,) = editor.saved_annotations
    assert saved.start_seconds == pytest.approx(23.9)
    assert saved.end_seconds == pytest.approx(27.9)


def test_an_event_with_no_duration_still_gives_a_usable_window(qt_app, frame) -> None:
    entry = make_entry(event_offset_seconds=10.0, event_duration_seconds=None)
    dialog = open_editor(qt_app, entry, frame)
    try:
        pick_tool(dialog, "Circle")
        drag(dialog, (0.3, 0.3), (0.6, 0.6))
        dialog._save()

        (saved,) = dialog.saved_annotations
        assert saved.end_seconds > saved.start_seconds, "a zero-length window shows nothing"
    finally:
        dialog.close()


def test_a_window_the_reviewer_set_is_not_overwritten(qt_app, frame) -> None:
    entry = make_entry(event_offset_seconds=23.9, event_duration_seconds=4.0)
    existing = Annotation(
        entry_id=entry.entry_id, kind=AnnotationKind.ELLIPSE,
        start_seconds=25.0, end_seconds=26.0,
    )
    dialog = open_editor(qt_app, entry, frame, annotations=[existing])
    try:
        dialog.canvas.select(existing.annotation_id)

        assert dialog.from_edit.seconds() == 25.0
        assert dialog.until_edit.seconds() == 26.0

        dialog._save()
        (saved,) = dialog.saved_annotations
        assert (saved.start_seconds, saved.end_seconds) == (25.0, 26.0)
    finally:
        dialog.close()


# --------------------------------------------------------------------------- #


def test_removing_a_mark_clears_the_list_and_the_panel(editor: AnnotationEditor) -> None:
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))

    editor.canvas.remove_selected()

    assert editor.canvas.annotations() == []
    assert editor.list_widget.count() == 0
    assert not editor.text_edit.isEnabled()


def test_cancelling_saves_nothing(editor: AnnotationEditor) -> None:
    """The dialog must not be able to write through the canvas behind the reviewer."""
    pick_tool(editor, "Circle")
    drag(editor, (0.25, 0.30), (0.55, 0.70))

    editor.reject()

    assert editor.saved_annotations is None


# --------------------------------------------------------------------------- #
# The route into the editor, not just the editor
# --------------------------------------------------------------------------- #


def test_every_directory_the_app_writes_to_is_created(tmp_path) -> None:
    """A path that is referenced but never made is a crash waiting for the one user
    who gets there first."""
    from evidence_review.config import AppPaths

    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    ).ensure()

    for directory in (
        paths.config_dir, paths.data_dir, paths.log_dir,
        paths.snapshots_dir, paths.exports_dir, paths.cache_dir,
    ):
        assert directory.is_dir(), f"{directory} was referenced but never created"


def test_annotating_an_entry_reaches_the_editor(window, monkeypatch, tmp_path) -> None:
    """Drives MainWindow._annotate_entry, which is how a reviewer actually gets there.

    The editor itself was covered from the start; this path was not, and it held an
    `AppPaths.cache_dir` that did not exist — so annotating anything raised
    AttributeError while every test of the dialog passed.
    """
    from PySide6.QtGui import QImage

    from evidence_review.ui import main_window as module

    media = tmp_path / "Cam4.mkv"
    media.write_bytes(b"not really media, but it is on disk")

    entry = window._store.save(
        make_entry(media_path=str(media), event_offset_seconds=12.0,
                   event_duration_seconds=3.0)
    )

    # Stand in for the player: the question here is the route, not mpv.
    frame = QImage(320, 240, QImage.Format.Format_RGB32)
    frame.fill(0)
    monkeypatch.setattr(module.MainWindow, "_frame_for", lambda self, _entry: frame)

    opened: list[object] = []

    class Stub:
        DialogCode = module.AnnotationEditor.DialogCode

        def __init__(self, *args, **kwargs) -> None:
            opened.append(kwargs.get("annotations"))

        def exec(self) -> int:
            return module.AnnotationEditor.DialogCode.Rejected

        @property
        def saved_annotations(self):
            return None

    monkeypatch.setattr(module, "AnnotationEditor", Stub)
    window._annotate_entry(entry)

    assert opened, "the editor was never opened"


def test_capturing_the_frame_to_annotate_uses_a_directory_that_exists(
    window, monkeypatch, tmp_path
) -> None:
    """The actual line that crashed: the cache directory has to be real and writable."""
    media = tmp_path / "clip.mkv"
    media.write_bytes(b"stand-in")

    entry = window._store.save(
        make_entry(media_path=str(media), event_offset_seconds=5.0)
    )

    captured: list[str] = []

    class Backend:
        def pause(self) -> None: ...

        def video_size(self):
            return (320, 240)

        def capture_frame(self, target) -> bool:
            captured.append(str(target))
            return False  # stop before QImage; the path is what is under test

    # A failed capture warns, and a modal warning blocks for ever offscreen.
    from evidence_review.ui import main_window as module

    monkeypatch.setattr(module.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(window, "load_media", lambda *a, **k: None)
    monkeypatch.setattr(window, "_seek_absolute", lambda *a, **k: None)
    window._mpv_backend = Backend()
    window._media_context = None

    window._frame_for(entry)

    assert captured, "no frame capture was attempted"
    target = Path(captured[0])
    assert target.parent.is_dir(), f"{target.parent} does not exist"
