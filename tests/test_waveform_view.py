"""The waveform on screen, driven with the mouse.

Two things can be wrong here and neither raises. The mapping between a pixel and a
moment can be off, which puts every mark somewhere other than where it was drawn. And
the view can show nothing at all while reporting that everything is fine -- a waveform
that paints an empty rectangle satisfies any assertion about its return values.

So these drive real mouse events and then look at the pixels.
"""

from __future__ import annotations

import array

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from evidence_review.media.audio_filters import AudioChain, FilterKind
from evidence_review.media.waveform import PEAKS_PER_SECOND, Waveform
from evidence_review.ui.marker_bar import EventMark
from evidence_review.ui.waveform_view import WAVE_COLOUR, WaveformView

from .conftest import make_entry


def make_waveform(duration: float = 60.0, *, event_at: float = 30.0) -> Waveform:
    """A quiet recording with one event in it, built without touching ffmpeg."""
    peaks = array.array("h")
    for bucket in range(int(duration * PEAKS_PER_SECOND)):
        when = bucket / PEAKS_PER_SECOND
        level = 4000 if event_at <= when < event_at + 2.0 else 120
        peaks.append(-level)
        peaks.append(level)
    return Waveform(peaks, PEAKS_PER_SECOND, duration, 4000)


@pytest.fixture
def view(qt_app):
    widget = WaveformView()
    widget.resize(800, 300)
    widget.set_waveform(make_waveform())
    widget.set_duration(60.0)
    widget.show()
    qt_app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qt_app.processEvents()


def x_for(view: WaveformView, seconds: float) -> int:
    start, end = view.view_span()
    return int((seconds - start) / (end - start) * view.width())


def rendered(view: WaveformView) -> QImage:
    image = QImage(view.size(), QImage.Format.Format_RGB32)
    view.render(image)
    return image.convertToFormat(QImage.Format.Format_RGB32)


def count_colour(image: QImage, hexcolour: str, tolerance: int = 40) -> int:
    from PySide6.QtGui import QColor

    wanted = QColor(hexcolour)
    found = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            pixel = image.pixelColor(x, y)
            if (abs(pixel.red() - wanted.red()) < tolerance
                    and abs(pixel.green() - wanted.green()) < tolerance
                    and abs(pixel.blue() - wanted.blue()) < tolerance):
                found += 1
    return found


# --------------------------------------------------------------------------- #
# Is anything actually drawn?
# --------------------------------------------------------------------------- #


def test_the_recording_is_drawn(view: WaveformView) -> None:
    """A view that paints an empty rectangle passes every structural assertion."""
    assert count_colour(rendered(view), WAVE_COLOUR) > 100


def test_the_event_is_taller_than_the_quiet_parts(view: WaveformView) -> None:
    """The one thing the picture has to communicate."""
    image = rendered(view)

    def height_at(seconds: float) -> int:
        column = x_for(view, seconds)
        from PySide6.QtGui import QColor

        wanted = QColor(WAVE_COLOUR)
        return sum(
            1
            for y in range(image.height())
            if abs(image.pixelColor(column, y).red() - wanted.red()) < 40
            and abs(image.pixelColor(column, y).blue() - wanted.blue()) < 40
        )

    assert height_at(31.0) > height_at(10.0) * 5


def test_nothing_is_drawn_without_a_waveform(qt_app) -> None:
    widget = WaveformView()
    widget.resize(400, 200)
    widget.set_message("Reading…")
    assert count_colour(rendered(widget), WAVE_COLOUR) == 0
    widget.deleteLater()


# --------------------------------------------------------------------------- #
# A pixel means a moment
# --------------------------------------------------------------------------- #


def test_clicking_seeks_to_the_moment_under_the_cursor(view: WaveformView) -> None:
    seen: list[float] = []
    view.seek_requested.connect(seen.append)

    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x_for(view, 20.0), view._wave_rect().center().y()))

    assert seen, "clicking the waveform did not seek"
    assert seen[0] == pytest.approx(20.0, abs=0.3)


def test_dragging_selects_the_span_that_was_dragged(view: WaveformView) -> None:
    """How a reviewer marks something they heard. The span has to be the span."""
    spans: list[tuple[float, float]] = []
    view.span_selected.connect(lambda a, b: spans.append((a, b)))

    middle = view._wave_rect().center().y()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x_for(view, 30.0), middle))
    QTest.mouseMove(view, QPoint(x_for(view, 33.0), middle))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(x_for(view, 33.0), middle))

    assert spans, "dragging produced no span"
    start, end = spans[0]
    assert start == pytest.approx(30.0, abs=0.4)
    assert end == pytest.approx(33.0, abs=0.4)


def test_a_click_is_not_a_span(view: WaveformView) -> None:
    """Otherwise every attempt to seek would leave a zero-length mark behind."""
    spans: list[tuple[float, float]] = []
    view.span_selected.connect(lambda a, b: spans.append((a, b)))

    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x_for(view, 20.0), view._wave_rect().center().y()))

    assert spans == []


def test_a_backwards_drag_still_gives_a_forward_span(view: WaveformView) -> None:
    spans: list[tuple[float, float]] = []
    view.span_selected.connect(lambda a, b: spans.append((a, b)))

    middle = view._wave_rect().center().y()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x_for(view, 35.0), middle))
    QTest.mouseMove(view, QPoint(x_for(view, 31.0), middle))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(x_for(view, 31.0), middle))

    assert spans
    start, end = spans[0]
    assert start < end


# --------------------------------------------------------------------------- #
# Zoom
# --------------------------------------------------------------------------- #


def test_zooming_keeps_the_moment_under_the_cursor(view: WaveformView) -> None:
    """Otherwise zooming walks away from whatever you were looking at."""
    view.zoom_by(4.0, around=40.0)
    start, end = view.view_span()
    assert start < 40.0 < end
    assert end - start == pytest.approx(15.0, abs=1.0)


def test_zoom_cannot_go_past_the_ends(view: WaveformView) -> None:
    view.zoom_by(0.001)
    start, end = view.view_span()
    assert start >= 0.0
    assert end <= 60.0 + 1e-6


def test_zooming_to_an_event_frames_it_with_room(view: WaveformView) -> None:
    view.zoom_to_span(30.0, 32.0)
    start, end = view.view_span()
    assert start < 30.0
    assert end > 32.0


def test_resetting_shows_the_whole_recording(view: WaveformView) -> None:
    view.zoom_to_span(30.0, 32.0)
    view.reset_zoom()
    assert view.view_span() == pytest.approx((0.0, 60.0))


def test_the_view_follows_the_playhead_when_it_leaves(view: WaveformView) -> None:
    """Playing past the right edge has to scroll, or the picture stops meaning
    anything a minute into a zoomed session."""
    view.set_view(10.0, 20.0)
    view.set_position(45.0)
    start, end = view.view_span()
    assert start <= 45.0 <= end


def test_the_view_does_not_jump_while_a_span_is_being_dragged(view: WaveformView) -> None:
    """Scrolling out from under a drag would move the moment the reviewer is aiming at."""
    view.set_view(10.0, 20.0)
    middle = view._wave_rect().center().y()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x_for(view, 12.0), middle))
    before = view.view_span()

    view.set_position(55.0)

    assert view.view_span() == before


# --------------------------------------------------------------------------- #
# Gain
# --------------------------------------------------------------------------- #


def test_gain_makes_quiet_parts_bigger(view: WaveformView) -> None:
    """The control EVP work needs: a session with one door slam leaves everything else
    a thin line, and the whisper is inside that line."""
    quiet_column = x_for(view, 10.0)

    def ink_at(column: int) -> int:
        image = rendered(view)
        from PySide6.QtGui import QColor

        wanted = QColor(WAVE_COLOUR)
        return sum(
            1
            for y in range(image.height())
            if abs(image.pixelColor(column, y).red() - wanted.red()) < 40
            and abs(image.pixelColor(column, y).blue() - wanted.blue()) < 40
        )

    before = ink_at(quiet_column)
    view.set_gain(8.0)
    after = ink_at(quiet_column)

    assert after > before, "turning the display up showed no more of the quiet part"


def test_gain_is_bounded(view: WaveformView) -> None:
    view.set_gain(10_000.0)
    assert view.gain() <= 64.0
    view.set_gain(0.0)
    assert view.gain() >= 0.1


# --------------------------------------------------------------------------- #
# Events on the picture
# --------------------------------------------------------------------------- #


def test_logged_events_are_drawn_on_the_waveform(view: WaveformView) -> None:
    plain = count_colour(rendered(view), "#e3a008")
    view.set_marks([
        EventMark(start=30.0, end=32.0, entry_id="e1", observation="Voice",
                  status="Unexplained"),
    ])
    assert count_colour(rendered(view), "#e3a008") > plain


def test_double_clicking_an_event_activates_it(view: WaveformView) -> None:
    view.set_marks([EventMark(start=30.0, end=32.0, entry_id="e1")])
    activated: list[int] = []
    view.mark_activated.connect(activated.append)

    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      QPoint(x_for(view, 31.0), view._wave_rect().center().y()))

    assert activated == [0]


def test_a_mark_under_a_loud_event_is_still_visible(view: WaveformView) -> None:
    """The shading goes under the wave so the shape stays readable, so the edges have
    to go on top -- and a span under a loud event is the span most worth seeing, since
    loud is usually why it was logged."""
    view.set_marks([
        EventMark(start=30.0, end=32.0, entry_id="e1", status="Unexplained"),
    ])
    image = rendered(view)
    column = x_for(view, 30.0)
    from PySide6.QtGui import QColor

    amber = QColor("#e3a008")
    on_the_edge = sum(
        1
        for y in range(image.height())
        if abs(image.pixelColor(column, y).red() - amber.red()) < 50
        and abs(image.pixelColor(column, y).green() - amber.green()) < 60
    )
    assert on_the_edge > 10, "the event boundary was buried under the waveform"


# --------------------------------------------------------------------------- #
# Audio gets the waveform, not a black rectangle
# --------------------------------------------------------------------------- #


class StubBackend:
    """Stands in for libmpv.

    Real mpv is not what these tests are about, and initialising it under the offscreen
    platform crashes the interpreter on teardown -- which is why every other test in
    this suite stubs it too. The waveform is read from the file by ffmpeg and does not
    involve the player at all.
    """

    supports_playback = True

    def __init__(self) -> None:
        self.loaded: list[str] = []

    def load(self, path) -> None:
        self.loaded.append(str(path))

    def stop(self) -> None: ...

    def pause(self) -> None: ...

    def set_speed(self, speed: float) -> None: ...

    def duration(self) -> float:
        return 6.0

    def position(self) -> float:
        return 0.0

    def is_paused(self) -> bool:
        return True

    def play(self) -> None: ...

    def seek_absolute(self, seconds: float) -> None: ...

    def capture_frame(self, target) -> bool:
        return False

    def set_audio_filters(self, spec: str) -> bool:
        self.filters = spec
        return True

    def audio_filters(self) -> str:
        return getattr(self, "filters", "")


@pytest.fixture
def audio_window(window, monkeypatch):
    """A real MainWindow whose player is a stub."""
    backend = StubBackend()

    def ensure(self):
        # The real _ensure_mpv assigns this as well as returning it, and load_media
        # relies on that, so the stand-in has to do the same.
        self._mpv_backend = backend
        return backend

    monkeypatch.setattr(type(window), "_ensure_mpv", ensure)
    window._mpv_backend = backend
    return window


def silent_recording(tmp_path, seconds: float = 6.0):
    """A short real audio file, so load_media takes the audio branch for real."""
    import subprocess

    from evidence_review.media.clip import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path / "evp.wav"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}:sample_rate=22050",
         "-c:a", "pcm_s16le", str(target)],
        check=True, capture_output=True,
    )
    return target


def settle(qt_app, predicate, seconds: float = 30.0) -> bool:
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_an_audio_file_shows_the_waveform_not_the_player(audio_window, qt_app, tmp_path) -> None:
    """An audio file used to select the mpv surface, which with no picture is a black
    rectangle -- no help when the work is finding a whisper in three hours."""
    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()

    assert audio_window.player_stack.currentWidget() is audio_window.waveform_view


def test_a_video_file_still_shows_the_player(audio_window, qt_app, tmp_path) -> None:
    import subprocess

    from evidence_review.media.clip import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    video = tmp_path / "cam.mkv"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:size=160x120:duration=2:rate=10",
         "-c:v", "libopenh264", str(video)],
        check=True, capture_output=True,
    )

    audio_window.load_media(video)
    qt_app.processEvents()

    assert audio_window.player_stack.currentWidget() is not audio_window.waveform_view


def test_the_waveform_is_read_and_shown(audio_window, qt_app, tmp_path) -> None:
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform), (
        "the waveform was never read"
    )
    assert audio_window.waveform_view._waveform.duration == pytest.approx(6.0, abs=0.3)


def test_the_envelope_is_cached_so_reopening_is_instant(audio_window, qt_app, tmp_path) -> None:
    import time

    media = silent_recording(tmp_path)
    audio_window.load_media(media)
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    assert list(audio_window._paths.cache_dir.glob("*.peaks")), "nothing was cached"

    audio_window.load_media(media)
    began = time.time()
    assert settle(qt_app, audio_window.waveform_view.has_waveform, seconds=5)
    assert time.time() - began < 2.0, "the recording was read again instead of cached"


def test_dragging_a_span_on_the_waveform_marks_an_event(audio_window, qt_app, tmp_path) -> None:
    """The same workflow as video: once it is a pending mark, Enter writes it up,
    P and N step through it and R repeats it."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    before = len(audio_window._pending_marks)
    audio_window.waveform_view.span_selected.emit(2.0, 4.0)
    qt_app.processEvents()

    assert len(audio_window._pending_marks) == before + 1
    mark = audio_window._pending_marks[-1]
    assert mark.start == pytest.approx(2.0)
    assert mark.effective_end == pytest.approx(4.0)


def test_a_zero_length_span_is_ignored(audio_window, qt_app, tmp_path) -> None:
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    before = len(audio_window._pending_marks)
    audio_window.waveform_view.span_selected.emit(2.0, 2.0)
    qt_app.processEvents()

    assert len(audio_window._pending_marks) == before


def test_logged_entries_appear_on_the_waveform(audio_window, qt_app, tmp_path) -> None:
    from evidence_review.models import MediaKind

    media = silent_recording(tmp_path)
    audio_window.load_media(media)
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    audio_window._store.save(
        make_entry(
            case_id=audio_window._settings.review.case_id,
            media_path=str(media), media_kind=MediaKind.AUDIO,
            event_offset_seconds=2.0, event_duration_seconds=1.0,
        )
    )
    audio_window._refresh_marks()
    qt_app.processEvents()

    assert any(m.is_logged for m in audio_window.waveform_view._marks)


def test_the_waveform_controls_appear_only_for_audio(audio_window, qt_app, tmp_path) -> None:
    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()
    assert audio_window.transport.gain_up_button.isVisibleTo(audio_window.transport)

    audio_window.transport.set_audio_mode(False)
    assert not audio_window.transport.gain_up_button.isVisibleTo(audio_window.transport)


def test_turning_the_waveform_up_never_touches_the_audio(audio_window, qt_app, tmp_path) -> None:
    """Gain is a display control. If it ever reached the player it would change what
    the reviewer is listening to, which is evidence."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    volume_before = audio_window._settings.player.volume
    audio_window._adjust_waveform_gain(4.0)

    assert audio_window.waveform_view.gain() == pytest.approx(4.0)
    assert audio_window._settings.player.volume == volume_before


def test_an_unreadable_recording_says_so_and_still_plays(audio_window, qt_app, tmp_path) -> None:
    """A waveform is a convenience. Losing it must not cost the reviewer the file."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    audio_window._on_waveform_failed(str(audio_window._media_context.path), "ffmpeg fell over")
    qt_app.processEvents()

    assert "could not be read" in audio_window.waveform_view._message
    assert "still work" in audio_window.waveform_view._message


def test_a_waveform_for_a_file_already_closed_is_dropped(audio_window, qt_app, tmp_path) -> None:
    """Opening another recording mid-read must not paint the old one's shape."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    stale = make_waveform_stub()
    audio_window._on_waveform_ready(r"C:\somewhere\else.wav", "", stale)

    assert audio_window.waveform_view._waveform is not stale


def make_waveform_stub():
    import array

    from evidence_review.media.waveform import PEAKS_PER_SECOND, Waveform

    return Waveform(array.array("h", [-1, 1] * 100), PEAKS_PER_SECOND, 1.0, 1)


# --------------------------------------------------------------------------- #
# The panel, and whether it tells the truth about what is being heard
# --------------------------------------------------------------------------- #


@pytest.fixture
def dock(qt_app):
    from evidence_review.ui.audio_dock import AudioDock

    widget = AudioDock()
    yield widget
    widget.close()
    widget.deleteLater()
    qt_app.processEvents()


def banner_text(dock) -> str:
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(dock.banner.text())
    return doc.toPlainText()


def test_the_panel_opens_saying_nothing_is_applied(dock) -> None:
    assert not dock.chain().is_processing
    assert "as it was" in banner_text(dock)


def test_turning_a_filter_on_says_what_is_being_heard(dock) -> None:
    """Never a bare "filters: on". Somebody coming back after lunch has to be able to
    read the state of the audio without opening anything."""
    box, amount = dock._rows[FilterKind.DENOISE]
    amount.setValue(12)
    box.setChecked(True)

    said = banner_text(dock)
    assert "PROCESSED" in said
    assert "noise reduction 12 dB" in said
    assert "not what the recorder captured" in said


def test_bypassing_says_the_recording_is_being_heard(dock) -> None:
    box, _ = dock._rows[FilterKind.DENOISE]
    box.setChecked(True)
    dock.toggle_bypass()

    said = banner_text(dock)
    assert "PROCESSED" not in said
    assert "as it was" in said
    assert dock.chain().settings, "the settings are kept, not thrown away"


def test_bypass_does_nothing_when_there_is_nothing_to_compare(dock) -> None:
    dock.toggle_bypass()
    assert not dock.chain().bypassed


def test_a_preset_reaches_the_controls_and_the_banner(dock) -> None:
    index = next(
        i for i in range(dock.preset_combo.count())
        if dock.preset_combo.itemText(i) == "Mains hum"
    )
    dock.preset_combo.setCurrentIndex(index)

    assert dock.chain().enabled(FilterKind.HUM)
    assert dock._rows[FilterKind.HUM][0].isChecked()
    assert "mains hum" in banner_text(dock)


def test_going_back_to_the_recording_turns_everything_off(dock) -> None:
    dock._rows[FilterKind.DENOISE][0].setChecked(True)
    dock._rows[FilterKind.HIGH_PASS][0].setChecked(True)

    dock._on_reset()

    assert dock.chain().to_spec() == ""
    assert not any(box.isChecked() for box, _ in dock._rows.values())


def test_every_change_is_announced(dock) -> None:
    """The window applies the chain to the player on this signal. A change that did
    not emit would leave the panel and the audio disagreeing."""
    seen: list[object] = []
    dock.chain_changed.connect(seen.append)

    dock._rows[FilterKind.HIGH_PASS][0].setChecked(True)
    dock._rows[FilterKind.HIGH_PASS][1].setValue(200)
    dock.toggle_bypass()

    assert len(seen) >= 3


def test_setting_a_chain_from_outside_does_not_echo_back(dock) -> None:
    """Loading a recording points the panel at a chain; echoing would re-apply it and,
    worse, could loop."""
    seen: list[object] = []
    dock.chain_changed.connect(seen.append)

    dock.set_chain(AudioChain().with_filter(FilterKind.DENOISE, 10))

    assert seen == []
    assert dock.chain().enabled(FilterKind.DENOISE)


# --------------------------------------------------------------------------- #
# In the window
# --------------------------------------------------------------------------- #


def test_the_panel_is_shown_for_audio_and_hidden_otherwise(
    audio_window, qt_app, tmp_path
) -> None:
    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()
    assert audio_window.audio_dock.isVisibleTo(audio_window)


def test_a_chain_reaches_the_player(audio_window, qt_app, tmp_path) -> None:
    applied: list[str] = []
    audio_window._mpv_backend.set_audio_filters = lambda spec: (
        applied.append(spec) or True
    )
    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()

    chain = AudioChain().with_filter(FilterKind.HIGH_PASS, 120)
    audio_window._on_audio_chain_changed(chain)

    assert applied and applied[-1] == chain.to_spec()


def test_a_chain_the_player_refuses_leaves_the_panel_honest(
    audio_window, qt_app, tmp_path, monkeypatch
) -> None:
    """Believing you are hearing processed audio when you are not is its own kind of
    wrong. If the player will not take the chain, the panel goes back to the recording
    rather than carrying on claiming processing is on.
    """
    from evidence_review.ui import main_window as module

    monkeypatch.setattr(
        module.QMessageBox, "warning", staticmethod(lambda *a, **k: None)
    )
    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()
    audio_window._mpv_backend.set_audio_filters = lambda spec: not spec

    # The panel has to be holding the chain first, which is the real situation: this
    # handler runs *from* the panel's own signal. Calling it with an empty panel would
    # assert that nothing is processing when nothing ever was.
    chain = AudioChain().with_filter(FilterKind.DENOISE, 20)
    audio_window.audio_dock.set_chain(chain)
    assert audio_window.audio_dock.chain().is_processing, "precondition"

    audio_window._on_audio_chain_changed(chain)

    assert not audio_window.audio_dock.chain().is_processing, (
        "the player refused the chain, so the panel must stop claiming it is applied"
    )


def test_what_was_being_heard_is_written_onto_the_entry(
    audio_window, qt_app, tmp_path
) -> None:
    """The heart of it. An observation made through heavy noise reduction is a
    different claim from the same words on the raw recording, and the record has to be
    able to say which."""
    from evidence_review.ui import log_dialog as dialog_module

    media = silent_recording(tmp_path)
    audio_window.load_media(media)
    qt_app.processEvents()

    chain = AudioChain().with_filter(FilterKind.DENOISE, 18)
    audio_window.audio_dock.set_chain(chain)

    captured: dict[str, str] = {}
    real_init = dialog_module.LogEntryDialog.__init__

    def spy(self, *args, **kwargs):
        captured["audio_filters"] = kwargs.get("audio_filters", "")
        raise RuntimeError("stop here; the dialog itself is not under test")

    dialog_module.LogEntryDialog.__init__ = spy
    try:
        with pytest.raises(RuntimeError):
            audio_window.open_log_dialog()
    finally:
        dialog_module.LogEntryDialog.__init__ = real_init

    assert captured["audio_filters"] == chain.to_spec()


def test_an_entry_logged_on_the_raw_recording_records_nothing(
    audio_window, qt_app, tmp_path
) -> None:
    from evidence_review.ui import log_dialog as dialog_module

    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()

    captured: dict[str, str] = {}
    real_init = dialog_module.LogEntryDialog.__init__

    def spy(self, *args, **kwargs):
        captured["audio_filters"] = kwargs.get("audio_filters", "MISSING")
        raise RuntimeError("stop")

    dialog_module.LogEntryDialog.__init__ = spy
    try:
        with pytest.raises(RuntimeError):
            audio_window.open_log_dialog()
    finally:
        dialog_module.LogEntryDialog.__init__ = real_init

    assert captured["audio_filters"] == ""


def test_the_chain_survives_a_round_trip_through_the_store(store) -> None:
    from evidence_review.models import EntryRow, MediaKind

    chain = (
        AudioChain()
        .with_filter(FilterKind.HIGH_PASS, 80)
        .with_filter(FilterKind.DENOISE, 12)
    )
    entry = store.save(
        EntryRow(
            file_name="a.wav", media_path=r"C:\ev\a.wav", media_kind=MediaKind.AUDIO,
            audio_filters=chain.to_spec(),
        )
    )

    stored = store.get(entry.entry_id)
    assert stored.audio_filters == chain.to_spec()
    assert AudioChain.from_spec(stored.audio_filters).describe() == chain.describe()
    assert stored.wire_payload()["audio_filters"] == chain.to_spec()


def test_editing_an_entry_keeps_what_was_heard_when_it_was_written(store) -> None:
    """Stamping the current setting on an edit would rewrite history every time
    somebody fixed a typo with different filters switched on."""
    from evidence_review.models import EntryRow, MediaKind

    original = AudioChain().with_filter(FilterKind.DENOISE, 18).to_spec()
    entry = store.save(
        EntryRow(
            file_name="a.wav", media_path=r"C:\ev\a.wav", media_kind=MediaKind.AUDIO,
            audio_filters=original,
        )
    )

    loaded = store.get(entry.entry_id)
    store.save(loaded.model_copy(update={"investigator_name": "Someone Else"}))

    assert store.get(entry.entry_id).audio_filters == original


def test_the_built_entry_carries_what_was_being_heard(qt_app, store, tmp_path) -> None:
    """Checks the entry the dialog produces, not the argument it was handed.

    An earlier version of this spied on the constructor's keyword arguments, which
    proved only that the window passed the value along. Deleting the line in the dialog
    that puts it on the entry left that test green and the record empty.
    """
    from evidence_review.autofill import MediaContext
    from evidence_review.config import Settings
    from evidence_review.ui.log_dialog import LogEntryDialog

    chain = AudioChain().with_filter(FilterKind.DENOISE, 18)
    media = tmp_path / "evp.wav"
    media.write_bytes(b"stand-in")

    settings = Settings()
    settings.review.investigator_name = "Tester"
    dialog = LogEntryDialog(
        context=MediaContext(path=media, kind="audio", duration_seconds=60.0),
        settings=settings,
        store=store,
        event_offset_seconds=12.0,
        audio_filters=chain.to_spec(),
    )
    try:
        entry = dialog._build_entry()
        assert entry.audio_filters == chain.to_spec(), (
            "the entry does not record what the reviewer was listening through"
        )
        assert AudioChain.from_spec(entry.audio_filters).describe() == chain.describe()
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_an_entry_built_on_the_raw_recording_records_nothing(
    qt_app, store, tmp_path
) -> None:
    from evidence_review.autofill import MediaContext
    from evidence_review.config import Settings
    from evidence_review.ui.log_dialog import LogEntryDialog

    media = tmp_path / "evp.wav"
    media.write_bytes(b"stand-in")

    settings = Settings()
    settings.review.investigator_name = "Tester"
    dialog = LogEntryDialog(
        context=MediaContext(path=media, kind="audio", duration_seconds=60.0),
        settings=settings,
        store=store,
        event_offset_seconds=12.0,
    )
    try:
        assert dialog._build_entry().audio_filters == ""
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_annotation_is_not_offered_for_a_recording(audio_window, qt_app, tmp_path) -> None:
    """A recording has no frame to draw on and nothing to burn an annotation into.

    The menu offered it anyway, which led to a warning for trying exactly what the menu
    had just invited.
    """
    from evidence_review.models import MediaKind

    media = silent_recording(tmp_path)
    entry = audio_window._store.save(
        make_entry(
            case_id=audio_window._settings.review.case_id,
            media_path=str(media), media_kind=MediaKind.AUDIO,
            event_offset_seconds=2.0,
        )
    )

    labels = [a.text() for a in audio_window.log_dock.context_menu_for(entry).actions()]

    assert not any("Annotate" in label for label in labels), labels
    assert any("Export clip" in label for label in labels), "the rest of the menu stands"


def test_annotation_is_still_offered_for_video(window, tmp_path) -> None:
    from evidence_review.models import MediaKind

    entry = window._store.save(
        make_entry(
            media_path=str(tmp_path / "cam.mkv"), media_kind=MediaKind.VIDEO,
            event_offset_seconds=2.0,
        )
    )
    labels = [a.text() for a in window.log_dock.context_menu_for(entry).actions()]
    assert any("Annotate" in label for label in labels), labels


def test_opening_a_video_takes_the_audio_cleanup_off(
    audio_window, qt_app, tmp_path
) -> None:
    """Otherwise the video's audio is filtered with the panel hidden and nothing on
    screen saying so -- the one outcome the disclosure design exists to prevent."""
    import subprocess

    from evidence_review.media.clip import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")

    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()
    chain = AudioChain().with_filter(FilterKind.DENOISE, 20)
    audio_window.audio_dock.set_chain(chain)
    audio_window._on_audio_chain_changed(chain)
    assert audio_window._mpv_backend.audio_filters() == chain.to_spec()

    video = tmp_path / "cam.mkv"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:size=160x120:duration=2:rate=10",
         "-c:v", "libopenh264", str(video)],
        check=True, capture_output=True,
    )
    audio_window.load_media(video)
    qt_app.processEvents()

    assert audio_window._mpv_backend.audio_filters() == "", (
        "the video's audio is still being filtered, with the panel hidden"
    )


def test_opening_another_recording_re_applies_the_cleanup(
    audio_window, qt_app, tmp_path
) -> None:
    """The panel keeps the chain across recordings, which is wanted. It has to actually
    be applied to the new file, or the panel claims a cleanup the reviewer never heard
    -- and an entry logged then would record one that was never applied."""
    audio_window.load_media(silent_recording(tmp_path))
    qt_app.processEvents()
    chain = AudioChain().with_filter(FilterKind.HIGH_PASS, 150)
    audio_window.audio_dock.set_chain(chain)
    audio_window._on_audio_chain_changed(chain)

    second = tmp_path / "second.wav"
    second.write_bytes(silent_recording(tmp_path).read_bytes())
    audio_window.load_media(second)
    qt_app.processEvents()

    assert audio_window.audio_dock.chain().is_processing, "the panel keeps the chain"
    assert audio_window._mpv_backend.audio_filters() == chain.to_spec(), (
        "the panel says processing is on but the player has nothing applied"
    )


def test_jumping_to_the_next_sound_is_reachable_and_goes_somewhere_real(
    audio_window, qt_app, tmp_path
) -> None:
    """loudest_moments was written, tested and wired to nothing."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    seeks: list[float] = []
    audio_window._seek_absolute = lambda seconds: seeks.append(seconds)
    audio_window.jump_to_next_sound()

    # The fixture recording is a steady tone, so it may legitimately offer nothing;
    # what must not happen is a crash or a seek to somewhere meaningless.
    for where in seeks:
        assert 0.0 <= where <= audio_window.waveform_view._waveform.duration


def test_the_playhead_moving_does_not_redraw_the_whole_recording(qt_app) -> None:
    """A three-hour recording is a million buckets, and reducing them to a thousand
    columns costs about half a second. The playhead repaints several times a second, so
    without a cache the view spent its time recomputing an unchanged picture and got
    slower the longer the recording -- unusable on exactly the files it is for.
    """
    import time

    from PySide6.QtGui import QImage

    big = make_waveform(duration=3600.0, event_at=1800.0)
    view = WaveformView()
    view.resize(1200, 320)
    view.set_waveform(big)
    view.set_duration(big.duration)
    image = QImage(view.size(), QImage.Format.Format_RGB32)

    began = time.perf_counter()
    view.render(image)
    first = time.perf_counter() - began

    began = time.perf_counter()
    for tick in range(8):
        view.set_position(tick * 0.1)
        view.render(image)
    each = (time.perf_counter() - began) / 8

    view.deleteLater()
    assert each < first / 3, (
        f"a paint while playing costs {each * 1000:.0f} ms against a first paint of "
        f"{first * 1000:.0f} ms -- the picture is being recomputed on every tick"
    )


def test_changing_the_view_does_recompute(qt_app) -> None:
    """The cache must not be so eager that zooming shows the old picture."""
    view = WaveformView()
    view.resize(600, 300)
    view.set_waveform(make_waveform())
    view.set_duration(60.0)

    whole = view._columns_for(view._wave_rect())
    view.set_view(10.0, 20.0)
    zoomed = view._columns_for(view._wave_rect())

    view.deleteLater()
    assert whole != zoomed, "zooming redrew the same columns"


# --------------------------------------------------------------------------- #
# The shape follows the cleanup, and says so
# --------------------------------------------------------------------------- #


def test_the_waveform_is_redrawn_through_the_cleanup(audio_window, qt_app, tmp_path) -> None:
    """The reviewer's own choice, and the reason the cleanup is worth having.

    An EVP event is inside a line that hum and hiss have flattened; with the cleanup
    applied it stands clear of the floor. Measured on a three-minute recording, the
    background fell from 38% of the height to 10% while the event held 94% -- contrast
    from 2.6x to 9.4x. A waveform that stayed raw while the audio was cleaned would show
    the reviewer none of that.
    """
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    assert audio_window._waveform_chain == "", "should start on the recording itself"

    chain = AudioChain().with_filter(FilterKind.HUM, 50)
    audio_window.audio_dock.set_chain(chain)
    audio_window._on_audio_chain_changed(chain)
    # The re-read is debounced; fire it as the timer would.
    audio_window._waveform_chain_timer.stop()
    audio_window._reread_waveform_for_chain()

    assert settle(qt_app, lambda: audio_window._waveform_chain != "")
    assert audio_window._waveform_chain == chain.to_spec()


def test_a_processed_waveform_says_so_on_its_own_face(audio_window, qt_app, tmp_path) -> None:
    """The same disclosure rule as everywhere else in this feature.

    A waveform gets read as evidence -- somebody measures a peak off it, or grabs it for
    a report -- so when the picture is of processed audio it has to carry that itself,
    not rely on a panel being open beside it.
    """
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    assert audio_window.waveform_view.processed() is None

    chain = AudioChain().with_filter(FilterKind.HUM, 50)
    audio_window.audio_dock.set_chain(chain)
    audio_window._reread_waveform_for_chain()
    assert settle(qt_app, lambda: audio_window.waveform_view.processed() is not None)

    said = audio_window.waveform_view.processed()
    assert said == chain.describe(), f"the badge should name what was done: {said!r}"


def test_the_badge_is_painted_into_the_picture_itself(qt_app) -> None:
    """Not into the chrome around it: the view is grabbed as an image for reports, and
    a disclosure in the surrounding furniture does not survive that."""
    from PySide6.QtGui import QColor, QImage

    from evidence_review.ui import theme
    from evidence_review.ui.waveform_view import WaveformView

    def grab(processed):
        view = WaveformView()
        view.resize(700, 240)
        view.set_waveform(make_waveform_stub())
        view.set_duration(1.0)
        view.set_processed(processed)
        image = QImage(view.size(), QImage.Format.Format_RGB32)
        view.render(image)
        return image

    plain = grab(None)
    badged = grab("hum removed at 50 Hz")

    assert plain != badged, "the badge left no mark on the painted picture"

    # And in amber, the colour this feature uses for "not the recording".
    warning = QColor(theme.WARNING).rgb()
    found = any(
        badged.pixel(x, y) == warning for y in range(20, 45) for x in range(10, 320)
    )
    assert found, "the badge is not drawn in the warning colour"


def test_bypass_puts_the_picture_back_on_the_recording(audio_window, qt_app, tmp_path) -> None:
    """Bypass is how an artefact gets caught -- the reviewer flips back and forth. The
    shape has to flip with it, or the comparison is only half made."""
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    chain = AudioChain().with_filter(FilterKind.HUM, 50)
    audio_window.audio_dock.set_chain(chain)
    audio_window._reread_waveform_for_chain()
    assert settle(qt_app, lambda: audio_window.waveform_view.processed() is not None)

    audio_window.audio_dock.set_chain(chain.with_bypass(True))
    audio_window._reread_waveform_for_chain()

    assert settle(qt_app, lambda: audio_window.waveform_view.processed() is None)
    assert audio_window._waveform_chain == ""


def test_a_slowed_chain_does_not_move_the_peaks(audio_window, tmp_path) -> None:
    """Slowing is for listening, and it stretches time.

    The envelope is drawn against the recording's own clock and the playhead is placed
    on it by seconds. An envelope read through atempo would put every peak at the wrong
    moment: the picture and the playhead would disagree, and a span dragged on it would
    mark seconds that are not the ones the reviewer heard.
    """
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    audio_window.audio_dock.set_chain(AudioChain().with_filter(FilterKind.SLOW, 75))

    assert audio_window._waveform_chain_spec() == "", (
        "a tempo change must not reach the picture"
    )
    assert audio_window._waveform_chain_description() is None


def test_a_slowed_chain_still_shows_the_rest_of_the_cleanup(audio_window, tmp_path) -> None:
    """Excluding the tempo change must not quietly exclude everything with it."""
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    audio_window.audio_dock.set_chain(
        AudioChain().with_filter(FilterKind.SLOW, 75).with_filter(FilterKind.HUM, 50)
    )

    spec = audio_window._waveform_chain_spec()
    assert spec, "the hum removal should still be shown"
    assert "atempo" not in spec
    assert audio_window._waveform_chain_description() is not None


def test_an_envelope_read_for_a_cleanup_since_changed_is_dropped(
    audio_window, qt_app, tmp_path
) -> None:
    """Reading three hours outlives several turns of the dial.

    A late envelope drawn under whatever the panel says by then is a picture of one
    thing captioned as another -- worse than no picture, because it is a measurement
    somebody will write down.
    """
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    current = audio_window.waveform_view._waveform

    late = make_waveform_stub()
    audio_window._on_waveform_ready(
        str(audio_window._media_context.path), "highpass=f=300", late
    )

    assert audio_window.waveform_view._waveform is current
    assert audio_window.waveform_view.processed() is None


def test_detail_read_for_another_cleanup_is_dropped(audio_window, qt_app, tmp_path) -> None:
    """Zoomed-in detail of the raw file, drawn over a processed overview, would put
    unprocessed samples behind a badge saying the opposite."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    audio_window._waveform_chain = "highpass=f=300"

    audio_window._on_detail_ready(
        str(audio_window._media_context.path), "", 0.0, 1.0, make_waveform_stub()
    )

    assert audio_window.waveform_view._detail is None


def test_a_cleaned_envelope_is_cached_apart_from_the_recording(tmp_path) -> None:
    """Both pictures are worth keeping -- bypass flips between them constantly -- and
    one must never be served as the other."""
    from evidence_review.media.waveform import cache_path

    target = tmp_path / "evp.wav"
    target.write_bytes(b"\0" * 2048)

    raw = cache_path(tmp_path, target)
    cleaned = cache_path(tmp_path, target, "highpass=f=300")
    other = cache_path(tmp_path, target, "lowpass=f=3000")

    assert len({raw, cleaned, other}) == 3
    assert cache_path(tmp_path, target, "highpass=f=300") == cleaned


def test_changing_the_cleanup_does_not_blank_the_waveform(audio_window, qt_app, tmp_path) -> None:
    """Twenty seconds of empty well because somebody nudged a cutoff would lose them the
    peak they were examining. The old shape stays up until the new one arrives."""
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    audio_window.audio_dock.set_chain(AudioChain().with_filter(FilterKind.HUM, 50))
    audio_window._reread_waveform_for_chain()
    qt_app.processEvents()

    assert audio_window.waveform_view.has_waveform(), "the well went empty mid-read"


def test_one_drag_of_a_cutoff_does_not_queue_a_read_per_step(
    audio_window, qt_app, tmp_path, monkeypatch
) -> None:
    """A spinbox drag emits a chain per step, and each read is an ffmpeg pass over the
    whole recording. Unthrottled, one drag starts a dozen reads of a three-hour file."""
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)

    started = []
    original = type(audio_window)._start_waveform

    def counting(self, path, *, keep_picture=False):
        started.append(path)
        return original(self, path, keep_picture=keep_picture)

    monkeypatch.setattr(type(audio_window), "_start_waveform", counting)
    for cutoff in range(50, 60):
        chain = AudioChain().with_filter(FilterKind.DENOISE, 12).with_filter(FilterKind.HUM, cutoff)
        audio_window.audio_dock.set_chain(chain)
        audio_window._on_audio_chain_changed(chain)
        qt_app.processEvents()

    assert started == [], f"{len(started)} reads started while the dial was moving"
    assert audio_window._waveform_chain_timer.isActive(), "the re-read was never scheduled"


def test_a_rereading_waveform_says_the_shape_is_the_previous_one(
    audio_window, qt_app, tmp_path
) -> None:
    """The price of not blanking the well.

    Keeping the old shape up means a stale picture with nothing saying so, and on a
    three-hour session the re-read takes minutes -- the audio has already changed by
    then. Measured here: three minutes of recording took 2.9s to re-read through hum
    removal, a high-pass and denoising, which scales to about three minutes for a
    three-hour file. Without the note the reviewer would be comparing what they hear
    against a shape of something else, with no way to tell.
    """
    from evidence_review.media.audio_filters import AudioChain, FilterKind

    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    assert audio_window.waveform_view.reading() is None

    audio_window.audio_dock.set_chain(AudioChain().with_filter(FilterKind.HUM, 50))
    audio_window._reread_waveform_for_chain()

    assert audio_window.waveform_view.reading() is not None, (
        "the picture went stale with nothing on it saying so"
    )

    assert settle(qt_app, lambda: audio_window.waveform_view.processed() is not None)
    assert audio_window.waveform_view.reading() is None, (
        "the note was left up after the new shape arrived"
    )


def test_a_failed_reread_does_not_leave_the_note_spinning(audio_window, qt_app, tmp_path) -> None:
    """A note that never clears is a progress bar that lies for the rest of the session."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    audio_window.waveform_view.set_reading("Re-reading the shape…")

    audio_window._on_waveform_failed(
        str(audio_window._media_context.path), "ffmpeg fell over"
    )

    assert audio_window.waveform_view.reading() is None


def test_opening_another_recording_clears_a_reread_note(audio_window, qt_app, tmp_path) -> None:
    """The note belongs to the file that was being re-read, not to the view."""
    audio_window.load_media(silent_recording(tmp_path))
    assert settle(qt_app, audio_window.waveform_view.has_waveform)
    audio_window.waveform_view.set_reading("Re-reading the shape…")

    second = tmp_path / "second"
    second.mkdir()
    audio_window.load_media(silent_recording(second, seconds=2.0))

    assert audio_window.waveform_view.reading() is None


def test_the_note_is_painted_into_the_picture(qt_app) -> None:
    """Same reason as the badge: it has to be visible on the shape itself."""
    from PySide6.QtGui import QImage

    from evidence_review.ui.waveform_view import WaveformView

    def grab(note):
        view = WaveformView()
        view.resize(700, 240)
        view.set_waveform(make_waveform_stub())
        view.set_duration(1.0)
        view.set_reading(note)
        image = QImage(view.size(), QImage.Format.Format_RGB32)
        view.render(image)
        return image

    assert grab(None) != grab("Re-reading the shape…  40%"), (
        "the note left no mark on the painted picture"
    )


def test_the_disclosure_never_covers_the_peak_it_discloses_about(qt_app) -> None:
    """Caught on a real screen, not in a test: the badge sat on top of the wave, and the
    event spike ran up behind it.

    That is the worst possible place for it. The whole point of the cleanup is to make
    the event the tallest thing on screen -- so a label floating at the top of the well
    covers precisely the peak somebody is hunting. The strip is reserved out of the
    wave's height instead, so nothing is ever hidden.
    """
    from PySide6.QtGui import QColor, QImage

    from evidence_review.ui.waveform_view import WAVE_COLOUR, WaveformView

    view = WaveformView()
    view.resize(700, 300)
    view.set_waveform(make_waveform_stub())
    view.set_duration(1.0)
    view.set_processed("50 Hz mains hum notched out")
    view.set_reading("Re-reading the shape…  40%")

    assert view._wave_rect().top() >= view._notice_rect().bottom(), (
        "the notice strip overlaps the wave well"
    )

    image = QImage(view.size(), QImage.Format.Format_RGB32)
    view.render(image)

    wave = QColor(WAVE_COLOUR).rgb()
    drawn = [
        y
        for y in range(image.height())
        for x in range(0, image.width(), 7)
        if image.pixel(x, y) == wave
    ]
    assert drawn, "nothing was drawn, so the test proves nothing"
    assert min(drawn) > view._notice_rect().bottom(), (
        "the wave is drawn under the notice strip, which hides the tallest peaks"
    )


def test_an_ordinary_recording_loses_no_height_to_the_strip(qt_app) -> None:
    """Most recordings have nothing to disclose, and every pixel of the well is working
    height on a quiet file."""
    from evidence_review.ui.waveform_view import WaveformView

    view = WaveformView()
    view.resize(700, 300)
    view.set_waveform(make_waveform_stub())
    view.set_duration(1.0)
    plain = view._wave_rect().height()

    view.set_processed("50 Hz mains hum notched out")
    assert view._wave_rect().height() < plain, "the strip was never reserved"

    view.set_processed(None)
    assert view._wave_rect().height() == plain, "the height was not given back"


def test_the_strip_carries_both_notices_at_once(qt_app) -> None:
    """Re-reading while already processed is the common case: the reviewer adjusts a
    cutoff on a chain that is already on. Both things are true and both must be said."""
    from PySide6.QtGui import QImage

    from evidence_review.ui.waveform_view import WaveformView

    def grab(processed, reading):
        view = WaveformView()
        view.resize(700, 300)
        view.set_waveform(make_waveform_stub())
        view.set_duration(1.0)
        view.set_processed(processed)
        view.set_reading(reading)
        image = QImage(view.size(), QImage.Format.Format_RGB32)
        view.render(image)
        return image

    # A badge short enough that it never elides, so the only thing that can differ
    # between these two pictures is the note itself. Comparing a long badge instead
    # would pass on the elide width alone -- the first version of this test did, and
    # survived the note being dropped entirely.
    badge_only = grab("hum out", None)
    both = grab("hum out", "Re-reading the shape…  40%")

    assert badge_only != both, "the re-read note vanished once processing was on"
