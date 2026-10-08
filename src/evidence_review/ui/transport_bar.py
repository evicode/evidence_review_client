"""The control strip under the player, including the LOG button."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..util import format_timecode
from . import theme
from .icons import pencil_icon, speaker_icon
from .marker_bar import MarkerBar
from .widgets import SeekSlider

SPEEDS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)

#: Chosen from glyphs the default Windows UI font actually contains. The
#: obvious speaker and camera emoji are not in it, so Qt fell back to colour
#: emoji that clashed with the monochrome line icons beside them.
#: Above 100 is amplification, which matters when the thing being reviewed is a
#: faint sound: it is useful, but the reviewer should know they have left
#: unity gain behind.
MAX_VOLUME = 130

#: Size of the drawn speaker on the mute button, matching the glyph buttons beside it.
VOLUME_ICON_SIZE = 15
#: A diamond reads as "marker"; the frame-step buttons use bar-and-triangle,
#: and the two pairs were indistinguishable at a glance before this.
PREV_MARK_GLYPH = "‹◆"  # noqa: RUF001 - angle quotes are the intended shape
NEXT_MARK_GLYPH = "◆›"  # noqa: RUF001 - angle quotes are the intended shape


def _tool_button(glyph: str, tooltip: str, *, checkable: bool = False) -> QToolButton:
    button = QToolButton()
    button.setText(glyph)
    button.setToolTip(tooltip)
    button.setCheckable(checkable)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return button


class TransportBar(QWidget):
    """Playback controls. Emits intent; the main window decides what to do."""

    play_pause_requested = Signal()
    stop_requested = Signal()
    seek_absolute = Signal(float)
    seek_relative = Signal(float)
    scrub_preview = Signal(float)
    frame_step_requested = Signal(bool)  # backwards?
    previous_requested = Signal()
    next_requested = Signal()
    speed_changed = Signal(float)
    volume_changed = Signal(int)
    mute_toggled = Signal(bool)
    snapshot_requested = Signal()
    fullscreen_toggled = Signal()
    log_requested = Signal()
    #: Draw over the picture at the playhead.
    annotate_requested = Signal()
    mark_toggled = Signal()
    mark_activated = Signal(int)
    repeat_toggled = Signal()
    previous_mark_requested = Signal()
    next_mark_requested = Signal()
    #: Still-image controls. The backend has had zoom and rotate all along; until
    #: now nothing in the window could reach them, so a reviewer who scrolled into
    #: a photograph had no way back out and could not straighten a sideways one.
    zoom_requested = Signal(float)
    #: Display gain for the waveform. Never touches the audio.
    gain_requested = Signal(float)
    waveform_fit_requested = Signal()
    next_sound_requested = Signal()  # multiplier
    fit_requested = Signal()
    rotate_requested = Signal(int)  # degrees

    def __init__(self, parent: QWidget | None = None, *, jump_step: float = 10.0) -> None:
        super().__init__(parent)
        self._jump_step = jump_step
        self._duration = 0.0
        self._position = 0.0
        self._playback_widgets: list[QWidget] = []

        # Without an explicit minimum the row's own layout minimum (~1250px) stops
        # the bar shrinking at all: instead of resizing it simply overflows and
        # clips, which is how the LOG button used to disappear. Allowing it to go
        # narrow lets _fit_controls see the real width and shed controls.
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 8)
        outer.setSpacing(6)

        # -- tagged events -------------------------------------------------- #
        # Sits directly above the seek bar so the two read as one timeline.
        self.marker_bar = MarkerBar()
        self.marker_bar.mark_activated.connect(self.mark_activated)
        outer.addWidget(self.marker_bar)
        self._playback_widgets.append(self.marker_bar)

        # -- seek row ------------------------------------------------------- #
        self.seek_slider = SeekSlider()
        self.seek_slider.scrubbing.connect(self.scrub_preview)
        self.seek_slider.seek_requested.connect(self.seek_absolute)
        outer.addWidget(self.seek_slider)
        self._playback_widgets.append(self.seek_slider)

        # -- control row ---------------------------------------------------- #
        row = QHBoxLayout()
        row.setSpacing(4)
        outer.addLayout(row)

        self.previous_button = _tool_button("⏮", "Previous file (PgUp)")
        self.previous_button.clicked.connect(self.previous_requested)
        row.addWidget(self.previous_button)

        self.back_jump_button = _tool_button("⏪", f"Back {jump_step:g}s")
        self.back_jump_button.clicked.connect(lambda: self.seek_relative.emit(-self._jump_step))
        row.addWidget(self.back_jump_button)
        self._playback_widgets.append(self.back_jump_button)

        self.frame_back_button = _tool_button("◀❘", "Previous frame (,)")
        self.frame_back_button.clicked.connect(lambda: self.frame_step_requested.emit(True))
        row.addWidget(self.frame_back_button)
        self._playback_widgets.append(self.frame_back_button)

        self.play_button = _tool_button("▶", "Play / pause (K)")
        self.play_button.setStyleSheet("font-size: 17px; padding: 3px 11px;")
        self.play_button.clicked.connect(self.play_pause_requested)
        row.addWidget(self.play_button)
        self._playback_widgets.append(self.play_button)

        self.frame_forward_button = _tool_button("❘▶", "Next frame (.)")
        self.frame_forward_button.clicked.connect(lambda: self.frame_step_requested.emit(False))
        row.addWidget(self.frame_forward_button)
        self._playback_widgets.append(self.frame_forward_button)

        self.forward_jump_button = _tool_button("⏩", f"Forward {jump_step:g}s")
        self.forward_jump_button.clicked.connect(lambda: self.seek_relative.emit(self._jump_step))
        row.addWidget(self.forward_jump_button)
        self._playback_widgets.append(self.forward_jump_button)

        self.next_button = _tool_button("⏭", "Next file (PgDn)")
        self.next_button.clicked.connect(self.next_requested)
        row.addWidget(self.next_button)

        self.stop_button = _tool_button("⏹", "Stop and return to the start")
        self.stop_button.clicked.connect(self.stop_requested)
        row.addWidget(self.stop_button)
        self._playback_widgets.append(self.stop_button)

        # -- still-image controls, shown in place of the transport ----------- #
        self._audio_mode = False
        self._image_widgets: list[QWidget] = []
        self.zoom_out_button = _tool_button("−", "Zoom out")  # noqa: RUF001 - a real minus pairs with the +
        self.zoom_out_button.clicked.connect(lambda: self.zoom_requested.emit(1 / 1.25))
        row.addWidget(self.zoom_out_button)
        self._image_widgets.append(self.zoom_out_button)

        self.zoom_in_button = _tool_button("+", "Zoom in")
        self.zoom_in_button.clicked.connect(lambda: self.zoom_requested.emit(1.25))
        row.addWidget(self.zoom_in_button)
        self._image_widgets.append(self.zoom_in_button)

        self.fit_button = _tool_button("▢", "Fit the image to the window")
        self.fit_button.clicked.connect(self.fit_requested)
        row.addWidget(self.fit_button)
        self._image_widgets.append(self.fit_button)

        self.rotate_button = _tool_button("↻", "Rotate 90° clockwise")
        self.rotate_button.clicked.connect(lambda: self.rotate_requested.emit(90))
        row.addWidget(self.rotate_button)
        self._image_widgets.append(self.rotate_button)

        # -- video: drawing over the picture -------------------------------- #
        # Annotation used to be reachable only by logging an event and then finding it
        # in the Session Log's right-click menu, so nothing while you were watching
        # said the feature existed at all.
        self._video_widgets: list[QWidget] = []

        # -- audio: the waveform's own controls ----------------------------- #
        self._audio_widgets: list[QWidget] = []

        self.gain_down_button = _tool_button("▁", "Show the waveform smaller")
        self.gain_down_button.clicked.connect(lambda: self.gain_requested.emit(1 / 2.0))
        row.addWidget(self.gain_down_button)
        self._audio_widgets.append(self.gain_down_button)

        self.gain_up_button = _tool_button("▇", "Amplify the waveform")
        self.gain_up_button.setToolTip(
            "Amplify the waveform so quiet parts are visible.\n"
            "This changes the picture only — the audio is untouched."
        )
        self.gain_up_button.clicked.connect(lambda: self.gain_requested.emit(2.0))
        row.addWidget(self.gain_up_button)
        self._audio_widgets.append(self.gain_up_button)

        self.next_sound_button = _tool_button("⇥", "Jump to the next sound")
        self.next_sound_button.setToolTip(
            "Jump to the next moment that stands out above the noise floor.\n"
            "On a long quiet recording this is where to start."
        )
        self.next_sound_button.clicked.connect(self.next_sound_requested)
        row.addWidget(self.next_sound_button)
        self._audio_widgets.append(self.next_sound_button)

        self.waveform_fit_button = _tool_button("⇔", "Show the whole recording")
        self.waveform_fit_button.clicked.connect(self.waveform_fit_requested)
        row.addWidget(self.waveform_fit_button)
        self._audio_widgets.append(self.waveform_fit_button)

        # Playback is the default mode, so these start out of the way. The video
        # controls stay, because video is what playback mode means by default.
        for widget in (*self._image_widgets, *self._audio_widgets):
            widget.setVisible(False)

        row.addSpacing(8)
        self.time_label = QLabel("--:--:-- / --:--:--")
        self.time_label.setObjectName("timeLabel")
        # Wide enough for "00:00:00.000 / 00:00:00" at the usual size, but allowed to
        # give ground: on a narrow window an elided timecode is a far smaller loss than
        # a clipped LOG button, and this is the only widget left with room to give.
        self.time_label.setMinimumWidth(120)
        row.addWidget(self.time_label)

        row.addStretch(1)

        # -- speed ---------------------------------------------------------- #
        self.speed_label = QLabel("Speed")
        self.speed_label.setObjectName("hintLabel")
        row.addWidget(self.speed_label)
        self._playback_widgets.append(self.speed_label)

        self.speed_combo = QComboBox()
        for speed in SPEEDS:
            self.speed_combo.addItem(f"{speed:g}×", speed)  # noqa: RUF001 - a real multiplication sign reads better here
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setMaximumWidth(80)
        self.speed_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_combo.currentIndexChanged.connect(
            lambda index: self.speed_changed.emit(float(self.speed_combo.itemData(index)))
        )
        row.addWidget(self.speed_combo)
        self._playback_widgets.append(self.speed_combo)

        row.addSpacing(8)

        # -- volume --------------------------------------------------------- #
        # Labelled, like Speed beside it. Reported from a real session as "I see no
        # volume control": it was a music note and a bare slider, which reads as a
        # decoration next to a labelled one.
        self.volume_label = QLabel("Volume")
        self.volume_label.setObjectName("hintLabel")
        row.addWidget(self.volume_label)
        self._playback_widgets.append(self.volume_label)

        self.mute_button = _tool_button("", "Mute (M)", checkable=True)
        self.mute_button.setIconSize(QSize(VOLUME_ICON_SIZE, VOLUME_ICON_SIZE))
        self.mute_button.toggled.connect(self._on_mute_toggled)
        row.addWidget(self.mute_button)
        self._playback_widgets.append(self.mute_button)
        self._refresh_mute_icon(False)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, MAX_VOLUME)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.valueChanged.connect(self.volume_changed)
        self.volume_slider.valueChanged.connect(self._refresh_volume_tooltip)
        row.addWidget(self.volume_slider)
        self._playback_widgets.append(self.volume_slider)
        self._refresh_volume_tooltip(self.volume_slider.value())

        row.addSpacing(8)

        self.snapshot_button = _tool_button("⎙", "Save a snapshot of this frame (S)")
        self.snapshot_button.clicked.connect(self.snapshot_requested)
        row.addWidget(self.snapshot_button)

        self.fullscreen_button = _tool_button("⛶", "Fullscreen (F)")
        self.fullscreen_button.clicked.connect(self.fullscreen_toggled)
        row.addWidget(self.fullscreen_button)

        # Space is the fast path, but it is not discoverable, so mirror it here.
        self.mark_button = _tool_button("〚", "Mark the start of an event (Space)")
        self.mark_button.clicked.connect(self.mark_toggled)
        row.addWidget(self.mark_button)
        self._playback_widgets.append(self.mark_button)

        self.previous_mark_button = _tool_button(PREV_MARK_GLYPH, "Previous tagged event (P)")
        self.previous_mark_button.clicked.connect(self.previous_mark_requested)
        row.addWidget(self.previous_mark_button)
        self._playback_widgets.append(self.previous_mark_button)

        self.next_mark_button = _tool_button(NEXT_MARK_GLYPH, "Next tagged event (N)")
        self.next_mark_button.clicked.connect(self.next_mark_requested)
        row.addWidget(self.next_mark_button)
        self._playback_widgets.append(self.next_mark_button)

        self.repeat_button = _tool_button(
            "↺", "Repeat the current tagged event (R)", checkable=True
        )
        self.repeat_button.clicked.connect(self.repeat_toggled)
        row.addWidget(self.repeat_button)
        self._playback_widgets.append(self.repeat_button)

        # Beside LOG, because the two belong together: record that something happened
        # here, and draw on what happened here. Buried among the playback controls it
        # read as one more seek button.
        self.annotate_button = _tool_button("", "Annotate this moment (A)")
        self.annotate_button.setIcon(pencil_icon(theme.TEXT, VOLUME_ICON_SIZE))
        self.annotate_button.setIconSize(QSize(VOLUME_ICON_SIZE, VOLUME_ICON_SIZE))
        self.annotate_button.setToolTip(
            "Draw text, arrows and circles over this moment.\n"
            "Marks belong to a logged event, so one is started if there is none here yet."
        )
        self.annotate_button.clicked.connect(self.annotate_requested)
        row.addWidget(self.annotate_button)
        self._video_widgets.append(self.annotate_button)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedWidth(1)
        separator.setObjectName("toolSeparator")
        row.addSpacing(6)
        row.addWidget(separator)
        row.addSpacing(6)

        # -- the reason this application exists ----------------------------- #
        self.log_button = QPushButton("LOG")
        self.log_button.setObjectName("logButton")
        self.log_button.setToolTip("Pause and record an observation (Ctrl+L)")
        self.log_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # A floor, not a hint. Once everything sheddable has gone and the row still
        # does not fit, Qt squeezes whatever is left -- and the last widget in the row
        # is this one, so the primary action was the thing that got cut down to "LO".
        self.log_button.setMinimumWidth(self.log_button.sizeHint().width())
        self.log_button.clicked.connect(self.log_requested)
        row.addWidget(self.log_button)

        # Least important first: these are shed, in this order, when the bar is
        # too narrow to show everything. Each has a keyboard equivalent, so
        # nothing becomes unreachable -- only less discoverable.
        self._optional_widgets = [
            self.speed_label,
            self.volume_label,
            self.speed_combo,
            self.volume_slider,
            # Both have menu entries and keys, and neither is part of working through
            # a recording.
            self.snapshot_button,
            self.fullscreen_button,
            self.stop_button,
            self.previous_button,
            self.next_button,
            self.previous_mark_button,
            self.next_mark_button,
            # Near the end on purpose. This is the control a reviewer could not find at
            # all when it existed only in a context menu, so it gives way only once
            # everything else already has.
            self.annotate_button,
            # Truly last. Below about 900px even the irreducible controls do not fit,
            # and at that point the position is still readable from the seek bar while
            # a clipped LOG button is not usable at all.
            self.time_label,
        ]
        self._playback_mode = True
        #: action id -> " (Key)" suffix, filled in by the shortcut manager.
        self._hints: dict[str, str] = {}

        # Measure the cost of each optional control once, while everything is
        # visible. Re-measuring as controls are hidden makes the layout chase its
        # own size hint; a fixed cost table gives the same answer every time.
        self.layout().activate()
        self._full_width = self.sizeHint().width()
        self._widget_cost = {
            widget: widget.sizeHint().width() + row.spacing() for widget in self._optional_widgets
        }
        # The audio controls are hidden while the measurement above is taken, because
        # they only appear for a recording -- so the figure is the *video* row's width.
        # Used unadjusted it made the bar four buttons wider than it was thought to be
        # whenever a recording was open, which is exactly where the shedding mattered
        # most: it shed too little, and the overflow fell on the last widget in the row,
        # which is the LOG button.
        self._audio_extra = sum(
            widget.sizeHint().width() + row.spacing() for widget in self._audio_widgets
        )
        # The measurement above is taken in video mode, so the video-only controls are
        # already counted in it; every other mode has to give their width back.
        self._video_extra = sum(
            widget.sizeHint().width() + row.spacing() for widget in self._video_widgets
        )
        # A still image shows its own controls and none of the timeline's, so its row
        # is a different width again.
        self._image_extra = sum(
            widget.sizeHint().width() + row.spacing() for widget in self._image_widgets
        )
        self._playback_only_width = sum(
            widget.sizeHint().width() + row.spacing()
            for widget in self._playback_widgets
        )

        self.set_media_loaded(False)

    # -- state from the player ---------------------------------------------- #

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self.seek_slider.set_duration(self._duration)
        self.marker_bar.set_duration(self._duration)
        self._refresh_time_label()

    def set_position(self, seconds: float) -> None:
        self._position = max(0.0, seconds)
        self.seek_slider.set_position(self._position)
        self.marker_bar.set_position(self._position)
        self._refresh_time_label()

    def apply_shortcut_hints(self, hints: dict[str, str]) -> None:
        """Rewrite tooltips so they name the keys currently bound.

        A tooltip that still says "(Space)" after the user rebound marking is a
        lie, and the transport bar is where most people look for the key.
        """
        self._hints = dict(hints)
        self.play_button.setToolTip(f"Play / pause{self._hint('play_pause')}")
        self.previous_button.setToolTip(f"Previous file{self._hint('previous_file')}")
        self.next_button.setToolTip(f"Next file{self._hint('next_file')}")
        self.frame_back_button.setToolTip(f"Previous frame{self._hint('frame_back')}")
        self.frame_forward_button.setToolTip(f"Next frame{self._hint('frame_forward')}")
        self.mute_button.setToolTip(f"Mute{self._hint('toggle_mute')}")
        self.snapshot_button.setToolTip(
            f"Save a snapshot of this frame{self._hint('save_snapshot')}"
        )
        self.fullscreen_button.setToolTip(f"Fullscreen{self._hint('toggle_fullscreen')}")
        self.previous_mark_button.setToolTip(f"Previous tagged event{self._hint('previous_mark')}")
        self.next_mark_button.setToolTip(f"Next tagged event{self._hint('next_mark')}")
        self.repeat_button.setToolTip(
            f"Repeat the current tagged event{self._hint('toggle_repeat')}"
        )
        self.log_button.setToolTip(
            f"Pause and record an observation{self._hint('log_at_playhead')}"
        )
        self.set_marking(self.mark_button.property("marking") or False)

    def _hint(self, action_id: str) -> str:
        return self._hints.get(action_id, "")

    def set_marking(self, marking: bool) -> None:
        """Reflect whether a mark is currently open."""
        self.mark_button.setProperty("marking", marking)
        self.mark_button.setText("〛" if marking else "〚")
        mark_key = self._hint("mark_toggle") or " (Space)"
        log_key = (self._hint("log_event") or " (Enter)").strip(" ()")
        self.mark_button.setToolTip(
            f"Close the event here{mark_key}, then {log_key} to log it"
            if marking
            else f"Mark the start of an event{mark_key}"
        )
        self.mark_button.setStyleSheet("color:#e5484d; font-weight:700;" if marking else "")

    def set_repeating(self, repeating: bool) -> None:
        self.repeat_button.blockSignals(True)
        self.repeat_button.setChecked(repeating)
        self.repeat_button.blockSignals(False)

    def set_preview_position(self, seconds: float) -> None:
        """Show a scrub target without claiming the player has moved there yet."""
        self._position = max(0.0, seconds)
        self._refresh_time_label()

    def set_paused(self, paused: bool) -> None:
        self.play_button.setText("▶" if paused else "⏸")
        # The key comes from the registry, not from here. Hardcoding "(K)" meant
        # the first play or pause overwrote whatever apply_shortcut_hints had
        # written, so the button advertised K again however it had been rebound.
        self.play_button.setToolTip(f"{'Play' if paused else 'Pause'}{self._hint('play_pause')}")

    def set_volume(self, volume: int) -> None:
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(int(volume))
        self.volume_slider.blockSignals(False)
        self._refresh_volume_tooltip(self.volume_slider.value())

    def _refresh_volume_tooltip(self, value: int) -> None:
        """Say the number, and say when it is above unity.

        The slider runs past 100 and the tooltip was the bare word "Volume", so
        nothing told the reviewer the current level, where 100% sat, or that the
        top of the track is amplification - which matters when the thing being
        reviewed is a faint sound.
        """
        if value > 100:
            self.volume_slider.setToolTip(f"Volume {value}% — amplified above 100%")
        else:
            self.volume_slider.setToolTip(f"Volume {value}%")

    def set_muted(self, muted: bool) -> None:
        self.mute_button.blockSignals(True)
        self.mute_button.setChecked(muted)
        self._refresh_mute_icon(muted)
        self.mute_button.blockSignals(False)

    def set_speed(self, speed: float) -> None:
        index = self.speed_combo.findData(speed)
        if index >= 0:
            self.speed_combo.blockSignals(True)
            self.speed_combo.setCurrentIndex(index)
            self.speed_combo.blockSignals(False)

    def set_jump_step(self, seconds: float) -> None:
        self._jump_step = seconds
        self.back_jump_button.setToolTip(f"Back {seconds:g}s")
        self.forward_jump_button.setToolTip(f"Forward {seconds:g}s")

    # -- mode --------------------------------------------------------------- #

    def set_media_loaded(self, loaded: bool) -> None:
        self.log_button.setEnabled(loaded)
        self.annotate_button.setEnabled(loaded)
        self.snapshot_button.setEnabled(loaded)
        self.fullscreen_button.setEnabled(loaded)
        if not loaded:
            self.time_label.setText("--:--:-- / --:--:--")

    def set_audio_mode(self, is_audio: bool) -> None:
        """Show the waveform's own controls, which only make sense for audio.

        Separate from set_playback_mode, which answers a different question -- whether
        there is a timeline at all. A recording has one; what it does not have is a
        picture, and that is what these controls are for.
        """
        self._audio_mode = is_audio
        for widget in self._audio_widgets:
            widget.setVisible(is_audio and self._playback_mode)
        for widget in self._video_widgets:
            widget.setVisible(not is_audio and self._playback_mode)
        # Four controls just appeared or left, so what fits has changed. Without this
        # the row keeps the video layout's answer until the next resize, which on a
        # window nobody resizes is for ever.
        self._fit_controls()

    def set_playback_mode(self, playable: bool) -> None:
        """Hide transport controls for still images, which have no timeline."""
        self._playback_mode = playable
        for widget in self._playback_widgets:
            widget.setVisible(playable)
        for widget in self._image_widgets:
            widget.setVisible(not playable)
        for widget in self._audio_widgets:
            widget.setVisible(playable and self._audio_mode)
        for widget in self._video_widgets:
            widget.setVisible(playable and not self._audio_mode)
        if not playable:
            self.time_label.setText("Still image")
        # Every mode now, not just playback. It used to skip still images entirely, so
        # whatever had been shed to fit a video row stayed hidden in the mode with six
        # fewer controls and room to spare -- and nothing could be shed there when the
        # row genuinely did not fit. _mode_allows keeps each mode's own controls
        # straight, which is what made the special case necessary before.
        self._fit_controls()

    # -- responsive layout --------------------------------------------------- #

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_controls()

    def _fit_controls(self) -> None:
        """Drop optional controls until the row fits the available width.

        The full control set needs about 1250px, which a narrow window or a wide
        playlist dock can easily deny it. Without this the row is silently clipped
        and the LOG button -- the one control that matters -- can fall off the end.
        """
        available = self.width()
        if available <= 0:
            return

        # Work out the smallest set to drop, cheapest-to-lose first, then apply
        # it in one pass.
        needed = self._full_width
        if self._playback_mode and self._audio_mode:
            needed += self._audio_extra - self._video_extra
        elif not self._playback_mode:
            needed += self._image_extra - self._playback_only_width - self._video_extra
        dropped: set[int] = set()
        for widget in self._optional_widgets:
            if needed <= available:
                break
            if not self._mode_allows(widget):
                # Already hidden by the mode, so dropping it would free nothing and
                # would stop the loop short of what actually has to go.
                continue
            dropped.add(id(widget))
            needed -= self._widget_cost.get(widget, 0)

        for widget in self._optional_widgets:
            widget.setVisible(self._mode_allows(widget) and id(widget) not in dropped)

    def _mode_allows(self, widget: QWidget) -> bool:
        """Whether the current mode would show this control at all.

        Shedding and mode are two different reasons for a control to be hidden, and
        conflating them is how the waveform's buttons would end up drawn over a video:
        restoring one must never override the other.
        """
        if widget in self._audio_widgets:
            return self._playback_mode and self._audio_mode
        if widget in self._video_widgets:
            return self._playback_mode and not self._audio_mode
        if widget in self._image_widgets:
            return not self._playback_mode
        if widget in self._playback_widgets:
            return self._playback_mode
        # Belongs to no mode, so every mode shows it -- the timecode, which reads
        # "Still image" for a photograph. Treating "in no list" as "playback only"
        # would blank it in the one mode that has no seek bar to fall back on.
        return True

    # -- internals ---------------------------------------------------------- #

    def _refresh_mute_icon(self, muted: bool) -> None:
        colour = theme.TEXT_MUTED if muted else theme.TEXT
        self.mute_button.setIcon(speaker_icon(colour, VOLUME_ICON_SIZE, muted=muted))

    def _on_mute_toggled(self, muted: bool) -> None:
        self._refresh_mute_icon(muted)
        self.mute_toggled.emit(muted)

    def _refresh_time_label(self) -> None:
        if self._duration <= 0 and self._position <= 0:
            return
        self.time_label.setText(
            f"{format_timecode(self._position)} / {format_timecode(self._duration, millis=False)}"
        )
