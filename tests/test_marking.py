"""Event marking: the model behind Space-Space-Enter, and the strip that draws it."""

from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication

# The marker bar is a real widget; render it without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from evidence_review.ui.marker_bar import EventMark, MarkerBar


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def bar(qt_app) -> MarkerBar:
    widget = MarkerBar()
    widget.resize(1000, 20)  # 1000 px for a 100 s file: 10 px per second
    widget.set_duration(100.0)
    return widget


# --------------------------------------------------------------------------- #
# EventMark
# --------------------------------------------------------------------------- #


def test_a_closed_mark_has_a_duration() -> None:
    mark = EventMark(start=10.0, end=14.5)
    assert mark.duration == pytest.approx(4.5)
    assert mark.effective_end == pytest.approx(14.5)
    assert not mark.is_logged


def test_an_open_mark_is_a_point_until_it_is_closed() -> None:
    """Space opens a mark; until the second press it has no end."""
    mark = EventMark(start=10.0)
    assert mark.duration is None
    assert mark.effective_end == 10.0


def test_a_logged_mark_knows_it() -> None:
    mark = EventMark(start=1.0, end=2.0, entry_id="abc", observation="knock", status="Unexplained")
    assert mark.is_logged


@pytest.mark.parametrize(
    ("seconds", "inside"),
    [(9.9, False), (10.0, True), (12.0, True), (14.5, True), (14.6, False)],
)
def test_contains(seconds: float, inside: bool) -> None:
    assert EventMark(start=10.0, end=14.5).contains(seconds) is inside


def test_a_backwards_duration_never_goes_negative() -> None:
    """Marks are normalised before construction, but the model must not produce
    a negative duration even if one slips through."""
    assert EventMark(start=10.0, end=8.0).duration == 0.0


# --------------------------------------------------------------------------- #
# MarkerBar geometry
# --------------------------------------------------------------------------- #


def test_marks_map_onto_the_timeline(bar: MarkerBar) -> None:
    assert bar._x_for(0.0) == pytest.approx(0.0)
    assert bar._x_for(50.0) == pytest.approx(500.0)
    assert bar._x_for(100.0) == pytest.approx(1000.0)


def test_positions_past_the_end_are_clamped(bar: MarkerBar) -> None:
    assert bar._x_for(500.0) == pytest.approx(1000.0)
    assert bar._x_for(-5.0) == pytest.approx(0.0)


def test_an_unloaded_file_has_no_geometry(qt_app) -> None:
    widget = MarkerBar()
    widget.resize(1000, 20)
    assert widget._x_for(10.0) == 0.0
    assert widget._seconds_at(500.0) == 0.0


def test_clicking_a_mark_finds_it(bar: MarkerBar) -> None:
    bar.set_marks([EventMark(start=10.0, end=20.0), EventMark(start=60.0, end=70.0)])
    assert bar._index_at(150.0) == 0  # 15 s
    assert bar._index_at(650.0) == 1  # 65 s
    assert bar._index_at(400.0) is None  # 40 s, between them


def test_a_short_mark_stays_clickable(bar: MarkerBar) -> None:
    """A half-second event is sub-pixel on a long file; it still has to be hittable."""
    bar.set_marks([EventMark(start=50.0, end=50.2)])
    assert bar._index_at(500.0) == 0


def test_a_short_mark_inside_a_long_one_wins(bar: MarkerBar) -> None:
    """Clicking the narrower mark is almost always what was meant."""
    bar.set_marks([EventMark(start=0.0, end=100.0), EventMark(start=49.0, end=51.0)])
    assert bar._index_at(500.0) == 1
    assert bar._index_at(100.0) == 0


def test_an_active_index_is_dropped_when_the_marks_shrink(bar: MarkerBar) -> None:
    """Logging a mark rebuilds the list; a stale index must not point off the end."""
    bar.set_marks([EventMark(start=1.0), EventMark(start=2.0), EventMark(start=3.0)])
    bar.set_active_index(2)
    bar.set_marks([EventMark(start=1.0)])
    assert bar._active_index is None


def test_the_strip_paints_without_a_file(qt_app) -> None:
    """Painting an empty strip must not raise; it is visible before anything loads."""
    from PySide6.QtGui import QPixmap

    widget = MarkerBar()
    widget.resize(400, 20)
    widget.render(QPixmap(400, 20))


def test_the_strip_paints_marks_and_an_open_mark(bar: MarkerBar) -> None:
    from PySide6.QtGui import QPixmap

    bar.set_marks(
        [
            EventMark(start=5.0, end=9.0, entry_id="a", status="Unexplained"),
            EventMark(start=30.0, end=31.0),
        ]
    )
    bar.set_active_index(0)
    bar.set_open_mark(60.0)
    bar.set_position(65.0)
    bar.render(QPixmap(1000, 20))


# --------------------------------------------------------------------------- #
# The transport bar has to survive a narrow window
# --------------------------------------------------------------------------- #


@pytest.fixture
def transport(qt_app):
    """A transport bar hosted and shown, which is the only way Qt delivers the
    resize events the responsive layout depends on."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from evidence_review.ui.transport_bar import TransportBar

    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    bar = TransportBar(jump_step=10.0)
    layout.addWidget(bar)
    bar.set_media_loaded(True)
    bar.set_playback_mode(True)
    bar.set_duration(1800.0)
    host.show()
    qt_app.processEvents()

    def resize(width: int):
        host.resize(width, 140)
        qt_app.processEvents()
        return bar

    yield resize
    host.close()


@pytest.mark.parametrize("width", [1600, 1280, 1100, 950, 800, 640])
def test_the_essential_controls_are_never_dropped(transport, width: int) -> None:
    """The row needs well over 1000px for everything. It used to overflow and clip
    instead of shrinking, which pushed the LOG button off the end entirely."""
    bar = transport(width)
    assert bar.log_button.isVisible(), f"LOG button lost at {width}px"
    assert bar.mark_button.isVisible(), f"Mark button lost at {width}px"
    assert bar.repeat_button.isVisible(), f"Repeat button lost at {width}px"
    assert bar.seek_slider.isVisible()
    assert bar.marker_bar.isVisible()
    assert bar.play_button.isVisible()


def test_controls_come_back_when_there_is_room(transport) -> None:
    """Shedding must be reversible, or widening the window leaves gaps."""
    narrow = transport(760)
    dropped_when_narrow = [w for w in narrow._optional_widgets if not w.isVisible()]
    assert dropped_when_narrow, "nothing was shed at 760px"

    wide = transport(2200)
    assert all(w.isVisible() for w in wide._optional_widgets), "controls did not return"


def test_controls_are_shed_cheapest_first(transport) -> None:
    """Speed is the first thing to go and the timecode the last, so the most useful
    controls survive longest.

    The timecode joined the list after the LOG button was found clipped on a real
    window: below about 900px even the irreducible controls do not fit, and at that
    point the position is still readable from the seek bar while a clipped LOG button
    -- the primary action -- is not usable at all. Mark navigation still outlives
    everything else that is merely a convenience.
    """
    bar = transport(2200)
    order = bar._optional_widgets
    assert order[0] is bar.speed_label
    assert order[-1] is bar.time_label
    assert order.index(bar.next_mark_button) > order.index(bar.snapshot_button)

    previously_dropped: set[int] = set()
    for width in (1600, 1400, 1200, 1000, 800):
        bar = transport(width)
        dropped = {id(w) for w in bar._optional_widgets if not w.isVisible()}
        assert previously_dropped <= dropped, (
            f"a control came back as the bar narrowed at {width}px"
        )
        previously_dropped = dropped


def test_a_still_image_hides_the_transport_entirely(transport) -> None:
    bar = transport(1600)
    bar.set_playback_mode(False)
    assert not bar.seek_slider.isVisible()
    assert not bar.marker_bar.isVisible()
    assert bar.log_button.isVisible(), "a still can still be logged"


# --------------------------------------------------------------------------- #
# A marked event has an end; everything that shows it must say so
# --------------------------------------------------------------------------- #


def test_a_marked_span_describes_its_duration() -> None:
    """Selecting a tagged event showed only its start, so a mark the reviewer had
    just closed looked like it had no end at all. The hover tooltip had been
    showing the span correctly all along - two copies of the same formatting, one
    of which had lost half of it."""
    from evidence_review.ui.marker_bar import EventMark

    span = EventMark(start=83.0, end=87.5).describe_span()
    assert "00:01:23" in span, "the start has to be there"
    assert "for" in span and "00:00:04" in span, "and so does how long it lasted"


def test_a_mark_with_no_end_describes_only_its_start() -> None:
    """An entry logged at a point, with no duration, has nothing to add."""
    from evidence_review.ui.marker_bar import EventMark

    assert EventMark(start=83.0).describe_span() == "00:01:23"
    assert EventMark(start=83.0, end=83.0).describe_span() == "00:01:23"


def test_selecting_a_pending_mark_reports_its_span(window, tmp_path, monkeypatch) -> None:
    """The reviewer's complaint, end to end: step onto an unlogged event with P or
    N and the status bar must say how long it is, not just where it starts."""
    from evidence_review.ui.marker_bar import EventMark

    messages: list[str] = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *a: messages.append(text))

    window._marks = [EventMark(start=83.0, end=87.5)]
    window._pending_marks = list(window._marks)
    window._active_mark = None
    window.cycle_mark(1)

    assert messages, "stepping to a mark has to report something"
    assert "00:01:23" in messages[-1]
    assert "00:00:04" in messages[-1], "the duration must be in the status bar too"
    assert "not logged yet" in messages[-1]


def test_clicking_a_mark_reports_its_span_too(window, monkeypatch) -> None:
    """Clicking a mark on the strip reported nothing at all - no start, no end,
    no observation. Stepping with P/N and clicking are the same act of selecting
    an event and must say the same thing."""
    from evidence_review.ui.marker_bar import EventMark

    messages: list[str] = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *a: messages.append(text))

    window._marks = [EventMark(start=10.0, end=12.0), EventMark(start=83.0, end=87.5)]
    window._activate_mark(1)

    assert messages, "clicking a mark must report something"
    assert "00:01:23" in messages[-1]
    assert "00:00:04" in messages[-1], "its duration too"
    assert "Event 2 of 2" in messages[-1]


def test_end_of_file_does_not_steal_a_fresh_mark_report(window, monkeypatch) -> None:
    """Selecting a mark seeks to it, and a mark near the end of the file ends
    playback at once - so "End of file" replaced the report of the very event the
    reviewer had just selected."""
    from evidence_review.ui.marker_bar import EventMark

    messages: list[str] = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *a: messages.append(text))

    window._marks = [EventMark(start=120.0, end=124.0)]
    window._activate_mark(0)
    assert "00:02:00" in messages[-1]

    # The backend reports the end a moment later, as it does for a mark near the end.
    window._on_playback_ended()
    assert messages[-1] != "End of file", "the mark report must survive"
    assert "00:02:00" in messages[-1]


def test_reaching_the_end_while_watching_still_says_so(window, monkeypatch) -> None:
    """The grace period protects a fresh report, not every end-of-file forever."""
    import time

    messages: list[str] = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *a: messages.append(text))

    # Nothing selected recently: the clock is well outside the grace period.
    window._mark_reported_at = time.monotonic() - (window.MARK_REPORT_GRACE_SECONDS + 1)
    window._on_playback_ended()
    assert messages[-1] == "End of file"


def test_space_space_actually_records_a_pending_mark(window) -> None:
    """The app's primary workflow, and it was not covered.

    Deleting the line that stores the mark left all 678 tests passing: tagging would
    appear to work, the strip would show the open mark growing, and closing it would
    quietly produce nothing to write up.
    """
    class Backend:
        supports_playback = True

        def __init__(self) -> None:
            self._at = 0.0

        def position(self) -> float:
            return self._at

    backend = Backend()
    window._backend = backend

    backend._at = 12.0
    window.toggle_mark()
    assert window._open_mark_start == pytest.approx(12.0)
    assert window._pending_marks == [], "nothing is recorded until the mark is closed"

    backend._at = 18.0
    window.toggle_mark()

    assert len(window._pending_marks) == 1, "closing the mark recorded nothing"
    mark = window._pending_marks[0]
    assert mark.start == pytest.approx(12.0)
    assert mark.effective_end == pytest.approx(18.0)
    assert window._open_mark_start is None


def test_a_mark_closed_after_seeking_backwards_is_not_inside_out(window) -> None:
    class Backend:
        supports_playback = True

        def __init__(self) -> None:
            self._at = 0.0

        def position(self) -> float:
            return self._at

    backend = Backend()
    window._backend = backend

    backend._at = 40.0
    window.toggle_mark()
    backend._at = 25.0
    window.toggle_mark()

    mark = window._pending_marks[0]
    assert mark.start < mark.effective_end


def test_the_log_button_is_never_clipped_in_audio_mode(transport) -> None:
    """Found on a real window: the LOG button rendered as a red stub reading ".OC".

    ``_full_width`` is measured once at construction, and the waveform's four controls
    are hidden at that moment because they only appear for a recording. So with a
    recording open the row was four buttons wider than the shedding believed, it shed
    too little, and the overflow landed on the last widget in the row -- which is the
    LOG button, the primary action of the whole application.

    It only showed in audio mode, which is why it survived: every check until then had
    been made on a video.
    """
    # 850 and 800 are where the miscounted budget actually bit: wider than that the
    # row had slack enough to absorb four extra buttons, which is why every earlier
    # check missed it.
    for width in (1200, 900, 850, 800):
        bar = transport(width)
        bar.set_media_loaded(True)
        bar.set_playback_mode(True)
        # Switching mode is what has to trigger the refit -- there is no resize after
        # it, and on a window nobody resizes there never will be. Letting Qt settle
        # here is what makes a missing refit visible rather than merely latent.
        bar.set_audio_mode(True)
        QApplication.instance().processEvents()
        bar.layout().activate()

        log = bar.log_button
        assert log.isVisible()
        assert log.geometry().right() <= bar.width(), (
            f"the LOG button runs off the end of a {width}px bar with a recording open"
        )
        assert log.width() >= log.sizeHint().width(), (
            f"the LOG button is squeezed below its own label at {width}px"
        )


def test_the_log_button_keeps_its_width_when_nothing_is_left_to_shed(transport) -> None:
    """Below about 700px every optional control has gone and the row still does not
    fit. Qt then squeezes what remains, and the last widget in the row is the LOG
    button -- so the primary action was cut down to "LO" rather than anything
    expendable giving way."""
    bar = transport(620)
    bar.set_media_loaded(True)
    bar.set_playback_mode(True)
    bar.set_audio_mode(True)
    bar.layout().activate()

    log = bar.log_button
    assert log.isVisible()
    assert log.width() >= log.sizeHint().width(), (
        "the LOG button was squeezed below its own label once shedding ran out"
    )


def test_a_recording_costs_more_room_than_a_video(transport) -> None:
    """The measurement the bug came from, asserted directly.

    If the audio controls cost nothing, the budget is the video row's and audio mode
    overflows by exactly their width.
    """
    bar = transport(1900)
    assert bar._audio_extra > 0, (
        "the waveform controls are being costed at nothing, so audio mode will overflow"
    )


def test_the_fit_never_restores_a_control_the_mode_hides(transport) -> None:
    """Shedding and mode are two different reasons for a control to be hidden.

    The fit restores whatever it shed. If it restores without asking the mode, controls
    belonging to a timeline come back over a still image -- which has none. The first
    version of the wider fix did exactly this, and the suite caught it rather than the
    eye.

    That the fit runs in this mode at all is guarded by
    ``test_image_mode_restores_controls_the_video_row_had_shed``; an assertion about
    the LOG button here would not discriminate, because a still image's row is short
    enough that it never overflows.
    """
    for width in (1900, 1100, 850, 800):
        bar = transport(width)
        bar.set_media_loaded(True)
        bar.set_playback_mode(False)  # a still image: no timeline at all
        bar.layout().activate()

        for name in ("speed_label", "speed_combo", "volume_slider",
                     "previous_mark_button", "next_mark_button"):
            assert not getattr(bar, name).isVisible(), (
                f"{name} came back over a still image at {width}px"
            )



def test_the_waveform_controls_survive_a_narrow_bar(transport) -> None:
    """They are the work in audio mode, not a convenience, so they are not shed.

    The cost of that choice is that a very narrow window clips instead; the trade is
    deliberate and belongs in a test rather than in somebody's memory.
    """
    # Narrow enough that everything sheddable has already gone, so if these were on
    # the list they would be gone too.
    for width in (1300, 850, 800):
        bar = transport(width)
        bar.set_media_loaded(True)
        bar.set_playback_mode(True)
        bar.set_audio_mode(True)
        QApplication.instance().processEvents()
        bar.layout().activate()

        assert not bar.speed_label.isVisible() or width > 1000, (
            "this width is too generous to prove anything -- nothing was shed"
        )
        for name in ("gain_up_button", "gain_down_button", "next_sound_button",
                     "waveform_fit_button"):
            assert getattr(bar, name).isVisible(), f"{name} was shed from a {width}px bar"
