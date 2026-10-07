"""The application window: player, transport, docks, and the LOG workflow."""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QByteArray, QDeadlineTimer, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QImage
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..autofill import MediaContext
from ..config import Settings, app_paths, get_api_token
from ..export import export as export_entries
from ..hashing import lookup_cached_sha256
from ..media.audio_filters import AudioChain
from ..media.base import MediaBackend
from ..media.image_backend import ImageBackend, ImageSurface
from ..media.mpv_loader import MpvNotAvailable
from ..media.waveform import DETAIL_THRESHOLD_SECONDS
from ..models import Case, EntryRow
from ..shortcuts import ACTIONS_BY_ID
from ..store import LocalStore
from ..sync import SyncWorker
from ..templates import FieldRole, LogTemplate
from ..util import (
    ALL_MEDIA_EXTENSIONS,
    classify_media,
    format_timecode,
    safe_filename,
    slugify_case_id,
    utc_now,
)
from ..version import APP_NAME, APP_VERSION
from ..workers import HashWorker, WaveformDetailWorker, WaveformWorker
from .annotation_editor import AnnotationEditor
from .annotation_overlay import AnnotationOverlay
from .audio_dock import AudioDock
from .clip_dialog import ClipExportDialog
from .entry_detail import EntryDetailDialog
from .log_dialog import LogEntryDialog
from .log_dock import LogDock
from .marker_bar import EventMark
from .playlist_dock import PlaylistDock
from .settings_dialog import SettingsDialog
from .shortcut_manager import ShortcutManager
from .template_editor import TemplateEditorDialog
from .transport_bar import TransportBar
from .waveform_view import WaveformView
from .widgets import fit_to_screen

log = logging.getLogger(__name__)

SYNC_INDICATORS = {
    "idle": ("●", "#3fb950"),
    "syncing": ("↻", "#58a6ff"),
    "offline": ("⚠", "#e3a008"),
    "error": ("✕", "#e5484d"),
    "disabled": ("○", "#8b949e"),
    # Amber, like offline: something needs attention, but nothing is broken and
    # nothing has been lost. The caption carries the difference.
    "unsubscribed": ("⚠", "#e3a008"),
}

# States that are silently costing the reviewer uploads. A grey glyph with the
# explanation buried in a tooltip is not enough warning that nothing is leaving
# the machine, so these spell it out in the status bar.
SYNC_CAPTIONS = {
    "offline": "Offline",
    "error": "Sync failed",
    "disabled": "Sync off",
    "unsubscribed": "Subscription needed",
}


class MainWindow(QMainWindow):
    """Everything the reviewer sees."""

    def __init__(self, settings: Settings, store: LocalStore) -> None:
        super().__init__()
        self._settings = settings
        self._store = store
        self._paths = app_paths()

        self._annotation_overlay = AnnotationOverlay(self)
        #: Said once per session. A lapsed subscription is worth telling somebody
        #: about; telling them every thirty seconds is harassment.
        #: The refusal reason last explained to the reviewer, or "" for none.
        #: Latched per reason rather than once per session: a failed payment that
        #: becomes an ended subscription needs a different fix and a different page.
        self._subscription_prompt_reason = ""
        self._waveform_worker: WaveformWorker | None = None
        self._detail_worker: WaveformDetailWorker | None = None
        #: The chain the drawn envelope was read through. "" is the recording itself.
        self._waveform_chain = ""
        self._mpv_backend: MediaBackend | None = None
        self._image_backend: ImageBackend | None = None
        self._backend: MediaBackend | None = None
        self._media_context: MediaContext | None = None
        self._hash_worker: HashWorker | None = None
        self._sync_worker: SyncWorker | None = None
        self._open_log_dialog: LogEntryDialog | None = None
        self._mpv_failure_reported = False
        self._was_playing_before_log = False
        self._pending_seek: float | None = None

        # Event marking. A mark is opened and closed with Space while the video
        # runs; pending marks live only for this session until they are logged.
        self._open_mark_start: float | None = None
        self._pending_marks: list[EventMark] = []
        self._marks: list[EventMark] = []
        self._active_mark: int | None = None
        self._repeating = False
        #: When a mark was last reported, so the end of the file cannot steal the
        #: answer to a question the reviewer has only just asked.
        self._mark_reported_at = 0.0

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        # 1100x720 was a minimum larger than some real screens: a 1366x768 laptop
        # at 125% scaling has 1092x614 to give, so the window could not be shrunk
        # to fit and its edges stayed off the desktop. The comfortable size is a
        # preference, applied below through fit_to_screen; this is the floor.
        self.setMinimumSize(800, 560)
        self.setAcceptDrops(True)

        self.shortcuts = ShortcutManager(self)

        self._build_central()
        self._build_docks()
        self._build_menus()
        self._build_status_bar()
        self.apply_shortcuts()

        self._restore_geometry()
        self._ensure_case_exists()
        self.refresh_log()
        self.start_sync()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.player_stack = QStackedWidget()

        # Filled in by apply_shortcuts, which knows the keys actually bound. The
        # empty state is the first thing a new reviewer reads, so it is the worst
        # possible place to name a key that has been rebound.
        self.placeholder = QLabel("Open a file or a folder to begin.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setObjectName("emptyState")
        self.player_stack.addWidget(self.placeholder)  # index 0

        # The mpv surface is created eagerly so it has a native handle ready,
        # but libmpv itself is only initialised on the first playable file.
        from ..media.mpv_backend import MpvSurface

        self.mpv_surface = MpvSurface()
        self.player_stack.addWidget(self.mpv_surface)  # index 1

        # What an audio file shows instead of the mpv surface, which for a recording
        # with no picture is a black rectangle.
        self.waveform_view = WaveformView()
        self.waveform_view.seek_requested.connect(self._seek_absolute)
        self.waveform_view.span_selected.connect(self._on_waveform_span)
        self.waveform_view.mark_activated.connect(self._activate_mark)
        self.waveform_view.view_changed.connect(self._on_waveform_view_changed)
        self.player_stack.addWidget(self.waveform_view)  # index 2

        self.image_surface = ImageSurface()
        self.player_stack.addWidget(self.image_surface)  # index 3

        layout.addWidget(self.player_stack, 1)

        self.transport = TransportBar(jump_step=self._settings.player.jump_step_seconds)
        layout.addWidget(self.transport)
        self._wire_transport()

        self.setCentralWidget(central)

    def _wire_transport(self) -> None:
        transport = self.transport
        transport.play_pause_requested.connect(self._toggle_pause)
        transport.stop_requested.connect(self._stop)
        transport.seek_absolute.connect(self._seek_absolute)
        transport.seek_relative.connect(self._seek_relative)
        transport.scrub_preview.connect(transport.set_preview_position)
        transport.frame_step_requested.connect(self._frame_step)
        transport.previous_requested.connect(lambda: self._step_file(-1))
        transport.next_requested.connect(lambda: self._step_file(1))
        transport.speed_changed.connect(self._set_speed)
        transport.volume_changed.connect(self._set_volume)
        transport.mute_toggled.connect(self._set_muted)
        transport.snapshot_requested.connect(self._save_snapshot)
        transport.fullscreen_toggled.connect(self._toggle_fullscreen)
        transport.log_requested.connect(self.open_log_dialog)
        transport.annotate_requested.connect(self.annotate_here)
        transport.mark_toggled.connect(self.toggle_mark)
        transport.mark_activated.connect(self._activate_mark)
        transport.repeat_toggled.connect(self.toggle_repeat)
        transport.gain_requested.connect(self._adjust_waveform_gain)
        transport.waveform_fit_requested.connect(self.waveform_view.reset_zoom)
        transport.next_sound_requested.connect(self.jump_to_next_sound)
        transport.previous_mark_requested.connect(lambda: self.cycle_mark(-1))
        transport.next_mark_requested.connect(lambda: self.cycle_mark(1))
        transport.zoom_requested.connect(self._zoom_image)
        transport.fit_requested.connect(self._fit_image)
        transport.rotate_requested.connect(self._rotate_image)

        transport.set_volume(self._settings.player.volume)
        transport.set_muted(self._settings.player.muted)
        transport.set_speed(self._settings.player.speed)

    def _build_docks(self) -> None:
        self.playlist_dock = PlaylistDock(self)
        self.playlist_dock.file_activated.connect(self.load_media)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.playlist_dock)
        self.playlist_dock.setVisible(self._settings.ui.playlist_dock_visible)

        self.log_dock = LogDock(self)
        self.log_dock.entry_activated.connect(self._goto_entry)
        self.log_dock.view_requested.connect(self._view_entry)
        self.log_dock.edit_requested.connect(self._edit_entry)
        self.log_dock.delete_requested.connect(self._delete_entry)
        self.log_dock.all_files_changed.connect(lambda _on: self.refresh_log())
        self.log_dock.table.selectionModel().selectionChanged.connect(
            lambda *_: self._refresh_entry_actions()
        )
        self.log_dock.restore_requested.connect(self._restore_entry)
        self.log_dock.clip_requested.connect(self._export_clip)
        self.log_dock.annotate_requested.connect(self._annotate_entry)

        self.audio_dock = AudioDock(self)
        self.audio_dock.chain_changed.connect(self._on_audio_chain_changed)
        # Dragging a cutoff spinbox emits a chain per step. Re-reading the envelope is
        # an ffmpeg pass over the whole recording, so one drag would otherwise queue a
        # dozen reads of a three-hour file and cancel eleven of them.
        self._waveform_chain_timer = QTimer(self)
        self._waveform_chain_timer.setSingleShot(True)
        self._waveform_chain_timer.setInterval(500)
        self._waveform_chain_timer.timeout.connect(self._reread_waveform_for_chain)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.audio_dock)
        # Only a recording has anything to clean up, so it stays out of the way until
        # one is open.
        self.audio_dock.hide()
        self.log_dock.show_deleted_changed.connect(lambda _on: self.refresh_log())
        self.log_dock.filter_changed.connect(lambda _text: self.refresh_log())
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.setVisible(self._settings.ui.log_dock_visible)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._add_action(file_menu, "", "open_file", self._open_file_dialog, ellipsis=True)
        self._add_action(file_menu, "", "open_folder", self._open_folder_dialog, ellipsis=True)
        file_menu.addSeparator()
        self._add_action(file_menu, "", "export_log", self._export_log, ellipsis=True)
        file_menu.addSeparator()
        self._add_action(file_menu, "", "edit_templates", self._edit_templates, ellipsis=True)
        self._add_action(file_menu, "", "open_settings", self._open_settings, ellipsis=True)
        file_menu.addSeparator()
        self._add_action(file_menu, "", "quit", self.close)

        review_menu = self.menuBar().addMenu("&Review")
        self._add_action(review_menu, "", "log_at_playhead", self.open_log_dialog, ellipsis=True)
        self._add_action(review_menu, "", "save_snapshot", self._save_snapshot, ellipsis=True)
        review_menu.addSeparator()
        # Editing and deleting used to live only in the Log panel's context menu,
        # so closing that panel took every way of changing a saved entry with it.
        # These three act on the entry selected in the Log panel, so they are greyed
        # out when there is none. They used to stay enabled and answer a click with a
        # status-bar line nobody saw, which reads as the command being broken -- most
        # of all for Delete, where the expected response is a confirmation box.
        self._entry_actions = [
            self._add_action(
                review_menu, "", "edit_entry", self._edit_selected_entry, ellipsis=True
            ),
        ]
        # Not in that list, and deliberately. It is the command the reporter could not
        # find, and greying it out until a row is selected is most of why: annotate_here
        # works from the playhead, so it only needs a video open.
        self._annotate_action = self._add_action(
            review_menu, "", "annotate_entry", self.annotate_here, ellipsis=True
        )
        self._add_action(review_menu, "", "toggle_audio_processing", self.toggle_audio_processing)
        # No ellipsis: a confirmation is not the command asking for more input.
        self._entry_actions.append(
            self._add_action(review_menu, "", "delete_entry", self._delete_selected_entry)
        )
        for action in self._entry_actions:
            action.setEnabled(False)
        review_menu.addSeparator()
        self._add_action(review_menu, "", "sync_now", self._sync_now)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.playlist_dock.toggleViewAction())
        view_menu.addAction(self.log_dock.toggleViewAction())
        view_menu.addSeparator()

        # Not _add_action: that drops the checked flag on its way to the slot,
        # which is exactly the thing a checkable action exists to carry.
        self.annotations_action = QAction(ACTIONS_BY_ID["toggle_annotations"].label, self)
        self.annotations_action.setCheckable(True)
        self.annotations_action.setChecked(True)
        self.annotations_action.setToolTip(
            "Show the marks drawn over this video. This is what you see on screen; "
            "whether an export contains them is chosen when you export."
        )
        self.annotations_action.toggled.connect(self._set_annotations_visible)
        view_menu.addAction(self.annotations_action)
        self.shortcuts.register_menu_action("toggle_annotations", self.annotations_action)

        view_menu.addSeparator()
        self._add_action(view_menu, "", "toggle_fullscreen", self._toggle_fullscreen)

        help_menu = self.menuBar().addMenu("&Help")
        self._add_action(help_menu, "", "show_shortcuts", self._show_shortcuts)
        self._add_action(help_menu, f"About {APP_NAME}", None, self._show_about)

    def _add_action(
        self, menu, text: str, action_id: str | None, slot, *, ellipsis: bool = False
    ) -> QAction:
        """Add a menu entry. With an action id, the manager owns its shortcut.

        The registry label *is* the menu text for anything with an id, so the two
        cannot drift: they used to disagree for almost every command ("Log
        Observation…" in the menu, "Log at the playhead" in the Shortcuts tab),
        and the editor's search only ever saw the registry side. A trailing "…"
        is the menu's own convention and means the command asks for something
        more before it acts.
        """
        if action_id is not None:
            text = ACTIONS_BY_ID[action_id].label
        if ellipsis:
            text += "…"
        action = QAction(text, self)
        # triggered() carries the checked flag, and PySide hands it to any slot
        # willing to take a positional argument. open_log_dialog(mark=None) is,
        # so a menu click used to arrive as mark=False and crash on False.start.
        # No menu slot wants that flag, so drop it for all of them.
        action.triggered.connect(lambda _checked=False, _slot=slot: _slot())
        menu.addAction(action)
        if action_id is not None:
            self.shortcuts.register_menu_action(action_id, action)
        return action

    def _build_status_bar(self) -> None:
        bar = self.statusBar()

        self.sync_label = QLabel()
        self.sync_label.setToolTip("Sync state")
        bar.addPermanentWidget(self.sync_label)

        # A button, not a label: with several cases in play, switching has to be
        # reachable from the window rather than through a Settings round-trip.
        self.case_button = QToolButton()
        self.case_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.case_button.setAutoRaise(True)
        self.case_button.setToolTip("The case new entries are filed under. Click to switch.")
        self.case_menu = QMenu(self.case_button)
        self.case_menu.aboutToShow.connect(self._rebuild_case_menu)
        self.case_button.setMenu(self.case_menu)
        bar.addPermanentWidget(self.case_button)

        self.investigator_label = QLabel()
        bar.addPermanentWidget(self.investigator_label)

        self._refresh_status_labels()
        self._set_sync_indicator("disabled", 0, "Remote sync is off")

    def _action_handlers(self) -> dict:
        """Every rebindable action that is not a menu entry.

        Menu entries carry their own shortcut so the key appears beside the item;
        everything here gets a QShortcut from the manager instead.
        """
        player = self._settings.player
        return {
            "mark_toggle": self.toggle_mark,
            "log_event": self.log_current_event,
            "cancel_mark": self._on_escape,
            "previous_mark": lambda: self.cycle_mark(-1),
            "next_mark": lambda: self.cycle_mark(1),
            "toggle_repeat": self.toggle_repeat,
            "play_pause": self._toggle_pause,
            "seek_back": lambda: self._seek_relative(-player.seek_step_seconds),
            "seek_forward": lambda: self._seek_relative(player.seek_step_seconds),
            "seek_back_fine": lambda: self._seek_relative(-player.fine_seek_step_seconds),
            "seek_forward_fine": lambda: self._seek_relative(player.fine_seek_step_seconds),
            "frame_back": lambda: self._frame_step(True),
            "frame_forward": lambda: self._frame_step(False),
            "volume_up": lambda: self._nudge_volume(5),
            "volume_down": lambda: self._nudge_volume(-5),
            "toggle_mute": self._toggle_mute,
            "speed_down": lambda: self._nudge_speed(-1),
            "speed_up": lambda: self._nudge_speed(1),
            "previous_file": lambda: self._step_file(-1),
            "next_file": lambda: self._step_file(1),
        }

    def apply_shortcuts(self) -> None:
        """(Re)bind every key from settings and refresh anything that names one."""
        self.shortcuts.set_handlers(self._action_handlers())
        self.shortcuts.apply(self._settings.shortcuts.overrides)
        self.transport.apply_shortcut_hints(
            {
                action_id: self.shortcuts.hint(action_id)
                for action_id in (
                    "play_pause",
                    "previous_file",
                    "next_file",
                    "frame_back",
                    "frame_forward",
                    "toggle_mute",
                    "save_snapshot",
                    "toggle_fullscreen",
                    "previous_mark",
                    "next_mark",
                    "toggle_repeat",
                    "log_at_playhead",
                    "mark_toggle",
                    "log_event",
                )
            }
        )
        self._refresh_placeholder()

    def _refresh_placeholder(self) -> None:
        """Name the keys that open a file, as currently bound."""
        open_file = self.shortcuts.key_for("open_file")
        open_folder = self.shortcuts.key_for("open_folder")
        lines = ["Open a file or a folder to begin."]
        hints = [
            f"{key}  {what}"
            for key, what in ((open_file, "open a file"), (open_folder, "open a folder"))
            if key
        ]
        if hints:
            lines.append("")
            lines.append("      ".join(hints))
        self.placeholder.setText("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Media loading
    # ------------------------------------------------------------------ #

    def _ensure_mpv(self) -> MediaBackend | None:
        """Create the mpv backend on first use, reporting failure once."""
        if self._mpv_backend is not None:
            return self._mpv_backend
        if self._mpv_failure_reported:
            return None
        try:
            from ..media.mpv_backend import MpvBackend

            backend = MpvBackend(
                self.mpv_surface,
                hardware_decoding=self._settings.player.hardware_decoding,
                volume=self._settings.player.volume,
                muted=self._settings.player.muted,
                parent=self,
            )
        except MpvNotAvailable as exc:
            self._mpv_failure_reported = True
            QMessageBox.critical(self, "Playback unavailable", str(exc))
            return None
        except Exception as exc:
            self._mpv_failure_reported = True
            log.exception("mpv initialisation failed")
            QMessageBox.critical(self, "Playback unavailable", str(exc))
            return None

        backend.position_changed.connect(self.transport.set_position)
        backend.position_changed.connect(self._annotation_overlay.refresh)
        backend.position_changed.connect(self.waveform_view.set_position)
        backend.duration_changed.connect(self.waveform_view.set_duration)
        backend.duration_changed.connect(self._on_duration_changed)
        backend.paused_changed.connect(self.transport.set_paused)
        backend.error.connect(self._on_backend_error)
        backend.metadata_changed.connect(self._on_metadata_changed)
        backend.ended.connect(self._on_playback_ended)
        # The saved speed reached the combo box but never the player: set_speed on
        # the transport blocks signals while it sets the value, so _set_speed never
        # ran, and the backend was never told. A reviewer timing an event trusted a
        # combo that read 2x while playback ran at 1x.
        backend.set_speed(self._settings.player.speed)
        self._mpv_backend = backend
        self._annotation_overlay.attach(backend)
        return backend

    def _ensure_image_backend(self) -> ImageBackend:
        if self._image_backend is None:
            backend = ImageBackend(self.image_surface, self)
            backend.error.connect(self._on_backend_error)
            self._image_backend = backend
        return self._image_backend

    def load_media(self, path: str | Path, *, seek_to: float | None = None) -> None:
        """Load a file into the appropriate backend and prepare autofill context.

        ``seek_to`` is applied once the backend reports a duration, which is how
        the log dock jumps straight to the moment an entry describes. Passing it
        explicitly also clears any seek left queued by a previous jump.
        """
        self._pending_seek = seek_to
        self._reset_marks()
        target = Path(path)
        if not target.is_file():
            QMessageBox.warning(self, "File not found", f"{target} could not be opened.")
            return

        kind = classify_media(target)
        if kind is None:
            QMessageBox.warning(
                self,
                "Unsupported file",
                f"{target.name} is not a supported media type.",
            )
            return

        # Stop whatever is currently playing before switching backends.
        if self._backend is not None and self._backend is not self._mpv_backend:
            self._backend.stop()
        elif self._mpv_backend is not None and kind == "image":
            self._mpv_backend.pause()

        if kind == "image":
            backend = self._ensure_image_backend()
            self.player_stack.setCurrentWidget(self.image_surface)
            self.transport.set_playback_mode(False)
            self.transport.set_audio_mode(False)
            self.audio_dock.hide()
        elif kind == "audio":
            # Same backend, same transport, different thing to look at. An audio file
            # used to select the mpv surface, which for a recording with no picture is
            # a black rectangle -- no help at all when the work is finding two seconds
            # of whisper in three hours.
            backend = self._ensure_mpv()
            if backend is None:
                return
            self.player_stack.setCurrentWidget(self.waveform_view)
            self.transport.set_playback_mode(True)
            self.transport.set_audio_mode(True)
            self.audio_dock.show()
        else:
            backend = self._ensure_mpv()
            if backend is None:
                return
            self.player_stack.setCurrentWidget(self.mpv_surface)
            self.transport.set_playback_mode(True)
            self.transport.set_audio_mode(False)
            self.audio_dock.hide()

        self._backend = backend
        backend.load(target)

        self._media_context = MediaContext(
            path=target,
            kind=kind,
            duration_seconds=None,
            sha256=lookup_cached_sha256(self._store, target),
        )

        self.transport.set_media_loaded(True)
        self._refresh_entry_actions()
        self.setWindowTitle(f"{target.name} — {APP_NAME}")
        self.statusBar().showMessage(str(target), 6000)
        self.playlist_dock.select_path(target)
        self._settings.player.last_directory = str(target.parent)

        if self._settings.review.compute_media_hash and self._media_context.sha256 is None:
            self._start_hashing(target)

        # The player is made to match the panel on every load, in both directions.
        self._sync_audio_chain_to_player(is_audio=kind == "audio")

        if kind == "audio":
            self._start_waveform(target)
        else:
            self._stop_waveform()
            self.waveform_view.set_waveform(None)
            self.waveform_view.set_processed(None)
            self.waveform_view.set_reading(None)
            self._waveform_chain = ""

        self._reload_annotations()
        self.refresh_log()

    def _reset_marks(self) -> None:
        """Marks belong to one file; opening another starts afresh."""
        self._stop_repeat()
        self._open_mark_start = None
        self._pending_marks.clear()
        self._marks.clear()
        self._active_mark = None
        self.transport.set_marking(False)
        self.transport.marker_bar.set_open_mark(None)
        self.transport.marker_bar.set_marks([])
        self.transport.marker_bar.set_active_index(None)

    def _stop_hashing(self) -> None:
        """Cancel any running hash and drop our reference to the worker.

        The worker is deleteLater'd when it finishes, so a stale reference points
        at a destroyed C++ object and merely calling isRunning() on it raises
        RuntimeError. That crashed the app on opening a second file once the
        first file's hash had completed.
        """
        worker = self._hash_worker
        self._hash_worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.cancel()
                worker.wait(2000)
        except RuntimeError:
            log.debug("Hash worker was already destroyed")

    def _on_hash_finished(self, worker: HashWorker) -> None:
        # Only clear it if a newer hash has not already taken its place.
        if self._hash_worker is worker:
            self._hash_worker = None

    def _start_hashing(self, path: Path) -> None:
        self._stop_hashing()
        worker = HashWorker(self._paths.database, path, self)
        worker.hash_ready.connect(self._on_hash_ready)
        worker.finished.connect(lambda w=worker: self._on_hash_finished(w))
        worker.finished.connect(worker.deleteLater)
        self._hash_worker = worker
        worker.start()

    def _on_hash_ready(self, media_path: str, digest: str) -> None:
        if self._media_context and str(self._media_context.path) == media_path:
            self._media_context.sha256 = digest
            if self._open_log_dialog is not None:
                self._open_log_dialog.set_media_hash(digest)

    # ------------------------------------------------------------------ #
    # Backend signal handlers
    # ------------------------------------------------------------------ #

    def _on_duration_changed(self, duration: float) -> None:
        self.transport.set_duration(duration)
        if self._media_context is not None and duration > 0:
            self._media_context.duration_seconds = duration

        # A seek queued by _goto_entry waits here until the file is genuinely ready.
        if self._pending_seek is not None and duration > 0:
            target, self._pending_seek = self._pending_seek, None
            self._seek_absolute(target)

    def _on_metadata_changed(self, metadata: dict) -> None:
        # Container tags are no longer used for autofill; kept for diagnostics.
        log.debug("Container metadata: %s", sorted(metadata) if metadata else "none")

    # -- still images ------------------------------------------------------- #

    def _zoom_image(self, factor: float) -> None:
        self.image_surface.zoom_by(factor)

    def _fit_image(self) -> None:
        self.image_surface.fit_to_window()

    def _rotate_image(self, degrees: int) -> None:
        self.image_surface.rotate_by(degrees)

    def _on_backend_error(self, message: str) -> None:
        """Report the failure, and leave it attached to the file that caused it.

        An eight-second status-bar message is no use when the question - which of
        these two hundred files could I not review? - gets asked at the end rather
        than at the time. The playlist marks the file instead, so the answer is
        still on screen an hour later.
        """
        self.statusBar().showMessage(message, 8000)
        log.warning("Playback error: %s", message)
        if self._media_context is not None:
            self.playlist_dock.mark_failed(self._media_context.path, message)

    #: How long a mark report is protected from being overwritten by the end of
    #: the file. Long enough to read, short enough that genuinely reaching the end
    #: while watching still says so.
    MARK_REPORT_GRACE_SECONDS = 2.5

    def _on_playback_ended(self) -> None:
        # Selecting a mark seeks to it, and a mark near the end of the file ends
        # playback at once - so "End of file" would replace the report of the very
        # event the reviewer just selected. Reaching the end is incidental there;
        # which event they picked is not.
        if time.monotonic() - self._mark_reported_at < self.MARK_REPORT_GRACE_SECONDS:
            return
        self.statusBar().showMessage("End of file", 4000)

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def _toggle_pause(self) -> None:
        if self._backend and self._backend.supports_playback:
            self._backend.toggle_pause()

    def _stop(self) -> None:
        """Return to the start and pause. Stop does not close the evidence.

        It used to call backend.stop(), which unloads the file, and then told the
        transport no media was loaded - disabling LOG, Snapshot and Fullscreen
        with nothing to re-enable them but reopening the file. One click next to
        Play left the reviewer unable to log anything.
        """
        if not self._backend:
            return
        if self._backend.supports_playback:
            if not self._backend.is_paused():
                self._backend.toggle_pause()
            self._backend.seek_absolute(0.0)

    def _seek_absolute(self, seconds: float) -> None:
        if self._backend and self._backend.supports_playback:
            self._backend.seek_absolute(seconds)

    def _seek_relative(self, seconds: float) -> None:
        if self._backend and self._backend.supports_playback:
            self._backend.seek_relative(seconds)

    def _frame_step(self, backwards: bool) -> None:
        if self._backend and self._backend.supports_playback:
            self._backend.frame_step(backwards)

    def _set_speed(self, speed: float) -> None:
        self._settings.player.speed = speed
        if self._backend:
            self._backend.set_speed(speed)

    def _nudge_speed(self, direction: int) -> None:
        from .transport_bar import SPEEDS

        current = self.transport.speed_combo.currentIndex()
        target = max(0, min(current + direction, len(SPEEDS) - 1))
        self.transport.speed_combo.setCurrentIndex(target)

    def _set_volume(self, volume: int) -> None:
        self._settings.player.volume = volume
        if self._backend:
            self._backend.set_volume(volume)

    def _nudge_volume(self, delta: int) -> None:
        self.transport.volume_slider.setValue(
            max(0, min(self.transport.volume_slider.value() + delta, 130))
        )

    def _set_muted(self, muted: bool) -> None:
        self._settings.player.muted = muted
        if self._backend:
            self._backend.set_muted(muted)

    def _toggle_mute(self) -> None:
        self.transport.mute_button.toggle()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._leave_fullscreen()
        else:
            self._docks_before_fullscreen = (
                self.playlist_dock.isVisible(),
                self.log_dock.isVisible(),
            )
            self.playlist_dock.hide()
            self.log_dock.hide()
            self.menuBar().hide()
            self.showFullScreen()

    def _leave_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        self.showNormal()
        self.menuBar().show()
        playlist, logs = getattr(self, "_docks_before_fullscreen", (True, True))
        self.playlist_dock.setVisible(playlist)
        self.log_dock.setVisible(logs)

    def _step_file(self, offset: int) -> None:
        self.playlist_dock.step(offset)

    def seek_to(self, seconds: float) -> None:
        """Move the player to a position. Used by the LOG modal's 'Go to' button."""
        self._seek_absolute(seconds)

    def current_playback_position(self) -> float | None:
        """Used by the LOG dialog's 'use current position' button."""
        if self._backend and self._backend.supports_playback:
            return self._backend.position()
        return None

    # ------------------------------------------------------------------ #
    # Event marking
    # ------------------------------------------------------------------ #

    def toggle_mark(self) -> None:
        """Space: open a mark at the playhead, or close the open one.

        Marking deliberately does not pause. The reviewer tags as the video runs
        and writes it up afterwards, which is the whole point of a single key.
        """
        if self._backend is None or not self._backend.supports_playback:
            return

        position = self._backend.position()

        if self._open_mark_start is None:
            self._open_mark_start = position
            self.transport.set_marking(True)
            self.transport.marker_bar.set_open_mark(position)
            self.statusBar().showMessage(
                f"Event started at {format_timecode(position)}  \u2014  "
                f"{self.shortcuts.key_for('mark_toggle')} to close it, "
                f"{self.shortcuts.key_for('cancel_mark')} to cancel",
                0,
            )
            return

        start, end = self._open_mark_start, position
        if end < start:
            # The reviewer seeked backwards before closing; take it as a span.
            start, end = end, start

        self._open_mark_start = None
        self.transport.set_marking(False)
        self.transport.marker_bar.set_open_mark(None)

        mark = EventMark(start=start, end=end)
        self._pending_marks.append(mark)
        self._refresh_marks()
        self._select_mark(self._index_of(mark))

        self.statusBar().showMessage(
            f"Event marked {format_timecode(start, millis=False)} to "
            f"{format_timecode(end, millis=False)}  \u2014  "
            f"{self.shortcuts.key_for('log_event')} to log it, "
            f"{self.shortcuts.key_for('toggle_repeat')} to repeat it",
            0,
        )

    def cancel_mark(self) -> bool:
        """Abandon an open mark. Returns whether there was one."""
        if self._open_mark_start is None:
            return False
        self._open_mark_start = None
        self.transport.set_marking(False)
        self.transport.marker_bar.set_open_mark(None)
        self.statusBar().showMessage("Mark cancelled", 3000)
        return True

    def _on_escape(self) -> None:
        if not self.cancel_mark():
            self._leave_fullscreen()

    # -- the mark list ------------------------------------------------------ #

    def _refresh_marks(self) -> None:
        """Rebuild the strip from logged entries plus this session's pending marks."""
        marks: list[EventMark] = []
        if self._media_context is not None:
            media_path = str(self._media_context.path)
            for entry in self._store.list_entries(
                case_id=self._settings.review.case_id, media_path=media_path
            ):
                if entry.event_offset_seconds is None:
                    continue
                # Addressed through the entry's own template, so a mark still
                # describes itself on a case whose template has changed since.
                template = self._store.get_template(entry.template_id)
                status = entry.value_for_role(template, FieldRole.STATUS) if template else None
                marks.append(
                    EventMark(
                        start=entry.event_offset_seconds,
                        end=entry.event_end_seconds,
                        entry_id=entry.entry_id,
                        observation=entry.summary(template) if template else "",
                        status="" if status is None else str(status),
                    )
                )
            marks.extend(self._pending_marks)

        marks.sort(key=lambda mark: mark.start)
        self._marks = marks
        self.transport.marker_bar.set_marks(marks)
        self.waveform_view.set_marks(marks)
        self.transport.marker_bar.set_active_index(self._active_mark)

    def _index_of(self, mark: EventMark) -> int | None:
        for index, candidate in enumerate(self._marks):
            if candidate is mark:
                return index
        return None

    def _select_mark(self, index: int | None, *, seek: bool = False) -> None:
        self._active_mark = index
        self.transport.marker_bar.set_active_index(index)
        if index is None:
            self._stop_repeat()
            return
        mark = self._marks[index]
        if seek:
            self._seek_absolute(mark.start)
        if self._repeating:
            self._arm_repeat(mark)

    def cycle_mark(self, direction: int) -> None:
        """P / N: step to the previous or next tagged event and seek to it."""
        if not self._marks:
            self.statusBar().showMessage("No tagged events in this file yet", 3000)
            return

        if self._active_mark is None:
            # Start from whatever is nearest the playhead.
            position = self._backend.position() if self._backend else 0.0
            if direction > 0:
                index = next((i for i, m in enumerate(self._marks) if m.start > position), 0)
            else:
                earlier = [i for i, m in enumerate(self._marks) if m.start < position]
                index = earlier[-1] if earlier else len(self._marks) - 1
        else:
            index = (self._active_mark + direction) % len(self._marks)

        self._select_mark(index, seek=True)
        self._report_mark(index)

    def _report_mark(self, index: int) -> None:
        """Say which event is now selected, and how long it is.

        Shared by stepping with P/N and by clicking the strip. Clicking used
        to report nothing at all, so selecting a mark that way looked like it
        had no end - the information was there, nothing said it.
        """
        mark = self._marks[index]
        detail = " ".join(mark.observation.split())[:60] if mark.observation else "not logged yet"
        self._mark_reported_at = time.monotonic()
        self.statusBar().showMessage(
            f"Event {index + 1} of {len(self._marks)}  \u2014  "
            f"{mark.describe_span()}  \u2014  {detail}",
            6000,
        )

    def _activate_mark(self, index: int) -> None:
        """A mark was clicked on the strip."""
        if 0 <= index < len(self._marks):
            self._select_mark(index, seek=True)
            self._report_mark(index)

    # -- repeat ------------------------------------------------------------- #

    def toggle_repeat(self) -> None:
        """R: loop the current tagged event so it can be watched over and over."""
        if self._repeating:
            self._stop_repeat()
            self.statusBar().showMessage("Repeat off", 3000)
            return

        if self._active_mark is None and self._marks:
            self._select_mark(0, seek=True)
        if self._active_mark is None:
            self.statusBar().showMessage(
                "Select a tagged event first "
                f"({self.shortcuts.key_for('previous_mark')} or "
                f"{self.shortcuts.key_for('next_mark')})",
                3000,
            )
            return

        mark = self._marks[self._active_mark]
        if mark.duration is None or mark.duration <= 0:
            self.statusBar().showMessage("That event has no duration to repeat", 4000)
            return

        self._repeating = True
        self.transport.set_repeating(True)
        self._arm_repeat(mark)
        self._seek_absolute(mark.start)
        if self._backend is not None:
            self._backend.play()
        self.statusBar().showMessage(
            f"Repeating {format_timecode(mark.start, millis=False)} to "
            f"{format_timecode(mark.effective_end, millis=False)}  \u2014  R to stop",
            0,
        )

    def _arm_repeat(self, mark: EventMark) -> None:
        if self._backend is None or mark.end is None:
            return
        self._backend.set_ab_loop(mark.start, mark.end)

    def _stop_repeat(self) -> None:
        if not self._repeating:
            return
        self._repeating = False
        self.transport.set_repeating(False)
        if self._backend is not None:
            self._backend.clear_ab_loop()

    # ------------------------------------------------------------------ #
    # The LOG workflow
    # ------------------------------------------------------------------ #

    def log_current_event(self) -> None:
        """Enter: pause and write up the event that was just marked.

        Closing a mark with Space leaves it pending; Enter is what turns it into a
        log entry. With no mark in play this behaves like the LOG button and logs
        at the playhead.
        """
        if self._open_mark_start is not None:
            # Enter while a mark is still open: close it first, so the natural
            # Space-Space-Enter rhythm works without a wasted keystroke.
            self.toggle_mark()

        mark = None
        if self._active_mark is not None and 0 <= self._active_mark < len(self._marks):
            candidate = self._marks[self._active_mark]
            if not candidate.is_logged:
                mark = candidate
        self.open_log_dialog(mark=mark)

    def open_log_dialog(self, mark: EventMark | None = None) -> None:
        """Pause, capture the frame, and open the modal with everything prefilled."""
        if self._media_context is None or self._backend is None:
            self.statusBar().showMessage("Open a file before logging an observation", 4000)
            return
        if self._open_log_dialog is not None:
            self._open_log_dialog.raise_()
            return

        # 1. Freeze the moment before anything else can move it.
        self._was_playing_before_log = (
            self._backend.supports_playback and not self._backend.is_paused()
        )
        if self._was_playing_before_log:
            self._backend.pause()

        if mark is not None:
            event_offset = mark.start
            event_duration = mark.duration
            # Put the player on the first frame of the event, so the captured
            # frame is the start of what was tagged rather than wherever playback
            # happened to stop.
            self._seek_absolute(mark.start)
        else:
            event_offset = self._backend.position() if self._backend.supports_playback else None
            event_duration = None

        # 2. Capture the frame the reviewer is actually looking at.
        snapshot_path: str | None = None
        if self._settings.review.capture_snapshot_on_log:
            snapshot_path = self._capture_snapshot(event_offset)

        # 3. Refresh what we know about the file, then ask.
        if self._backend.supports_playback:
            duration = self._backend.duration()
            if duration > 0:
                self._media_context.duration_seconds = duration

        dialog = LogEntryDialog(
            context=self._media_context,
            settings=self._settings,
            store=self._store,
            event_offset_seconds=event_offset,
            event_duration_seconds=event_duration,
            snapshot_path=snapshot_path,
            # What was being heard, written onto the entry. An observation made under
            # heavy noise reduction is a different claim from the same words on the
            # raw recording.
            audio_filters=self.audio_dock.chain().to_spec(),
            parent=self,
        )
        self._open_log_dialog = dialog
        try:
            accepted = dialog.exec() == LogEntryDialog.DialogCode.Accepted
        finally:
            self._open_log_dialog = None

        if accepted and dialog.entry is not None:
            if mark is not None and mark in self._pending_marks:
                # It is a real entry now, so it stops being a pending mark and
                # will be redrawn from the store instead.
                self._pending_marks.remove(mark)
                self._active_mark = None
            self._commit_entry(dialog.entry)
        elif snapshot_path:
            # Cancelled: do not leave an orphan image behind.
            Path(snapshot_path).unlink(missing_ok=True)

        if self._was_playing_before_log and self._settings.review.auto_resume_after_log:
            self._backend.play()

    def _commit_entry(self, entry: EntryRow) -> None:
        """Write locally first; the network is somebody else's problem."""
        try:
            self._store.save(entry)
        except Exception as exc:
            log.exception("Failed to save log entry")
            QMessageBox.critical(
                self,
                "Entry not saved",
                f"The entry could not be written to the local database:\n\n{exc}",
            )
            return

        self.refresh_log()
        template = self._store.get_template(entry.template_id)
        headline = entry.summary(template) if template else entry.file_name
        self.statusBar().showMessage(f"Logged: {headline[:70]}", 5000)
        if self._sync_worker is not None:
            self._sync_worker.request_sync()

    def _capture_snapshot(self, event_offset: float | None) -> str | None:
        if self._backend is None or self._media_context is None:
            return None
        stem = safe_filename(self._media_context.path.stem, max_length=60)
        marker = (
            f"{int(event_offset // 3600):02d}"
            f"{int(event_offset // 60) % 60:02d}"
            f"{int(event_offset) % 60:02d}"
            if event_offset is not None
            else _dt.datetime.now().strftime("%H%M%S")
        )
        folder = self._paths.snapshot_dir_for_case(self._settings.review.case_id)
        target = folder / f"{stem}_{marker}_{uuid.uuid4().hex[:8]}.png"

        # A recording has no frame to capture, so asking mpv for one always failed and
        # every audio entry went into the record with no picture at all. The waveform
        # around the moment is the honest equivalent: it shows what was logged, and it
        # is the thing the reviewer was looking at when they logged it.
        if self._media_context.kind == "audio":
            return self._capture_waveform_image(target, event_offset)

        try:
            if self._backend.capture_frame(target):
                return str(target)
        except Exception:
            log.warning("Snapshot capture failed", exc_info=True)
        return None

    def _capture_waveform_image(self, target: Path, event_offset: float | None) -> str | None:
        """Render the waveform around a moment, as an audio entry's snapshot."""
        if not self.waveform_view.has_waveform():
            return None
        before = self.waveform_view.view_span()
        try:
            if event_offset is not None:
                # Enough either side to show the event in its context rather than
                # filling the frame with it.
                self.waveform_view.set_view(event_offset - 4.0, event_offset + 8.0)
            image = QImage(self.waveform_view.size(), QImage.Format.Format_RGB32)
            self.waveform_view.render(image)
            target.parent.mkdir(parents=True, exist_ok=True)
            if image.save(str(target), "PNG"):
                return str(target)
        except Exception:
            log.warning("Could not render the waveform snapshot", exc_info=True)
        finally:
            self.waveform_view.set_view(*before)
        return None

    def _save_snapshot(self) -> None:
        """Manual snapshot, saved wherever the user chooses."""
        if self._backend is None or self._media_context is None:
            return
        suggested = self._paths.exports_dir / f"{safe_filename(self._media_context.path.stem)}.png"
        target, _ = QFileDialog.getSaveFileName(
            self, "Save snapshot", str(suggested), "PNG image (*.png)"
        )
        if not target:
            return
        if self._backend.capture_frame(target):
            self.statusBar().showMessage(f"Snapshot saved to {target}", 5000)
        else:
            QMessageBox.warning(self, "Snapshot failed", "The frame could not be captured.")

    # ------------------------------------------------------------------ #
    # Log dock
    # ------------------------------------------------------------------ #

    def refresh_log(self) -> None:
        case_id = self._settings.review.case_id
        search = self.log_dock.search_edit.text().strip() or None
        # The entries belonging to the file on screen, unless the whole case is asked
        # for or there is nothing open to scope to. The timeline's marks have always
        # been filtered this way; the log showing something else made the two panels
        # disagree about what was being reviewed.
        scope_to_file = not self.log_dock.all_files and self._media_context is not None
        media_path = str(self._media_context.path) if scope_to_file else None
        # Deleted rows are included only when asked for, but they have to be
        # askable: a deletion nobody can see afterwards reads as an erasure.
        entries = self._store.list_entries(
            case_id=case_id,
            search=search,
            include_deleted=self.log_dock.show_deleted,
            media_path=media_path,
        )
        # The columns follow the case's template; the others are passed so an entry
        # written on a different one still renders from its own definition.
        self.log_dock.set_template(
            self._store.template_for_case(case_id),
            {template.template_id: template for template in self._store.list_templates()},
        )
        self.log_dock.set_entries(
            entries,
            deleted=self._store.deleted_count(case_id),
            scoped_to_file=scope_to_file,
        )
        # Resetting the model clears the selection, so what the menu offers has to be
        # worked out again or it keeps offering commands for a row that has gone.
        self._refresh_entry_actions()
        self._refresh_marks()

    def _templates_for(self, entries: Sequence[EntryRow]) -> list[LogTemplate]:
        """Every template the given entries were written on.

        An export of a case whose template was changed contains both shapes, and
        each needs its own columns; squeezing one into the other's would be the
        label-reuse mistake this feature exists to undo. The case's current template
        leads, so its fields are the left-hand columns.
        """
        current = self._store.template_for_case(self._settings.review.case_id)
        ordered = [current]
        for template_id in dict.fromkeys(entry.template_id for entry in entries):
            if template_id == current.template_id:
                continue
            template = self._store.get_template(template_id)
            if template is not None:
                ordered.append(template)
        return ordered

    def _goto_entry(self, entry: EntryRow) -> None:
        """Make the log a navigable index: jump to the moment an entry describes."""
        target = Path(entry.media_path)
        if not target.is_file():
            QMessageBox.information(
                self,
                "File unavailable",
                f"The media for this entry is not where it was logged:\n\n{entry.media_path}",
            )
            return
        already_open = self._media_context is not None and self._media_context.path == target
        if not already_open:
            self.load_media(target, seek_to=entry.event_offset_seconds)
        elif entry.event_offset_seconds is not None:
            self._seek_absolute(entry.event_offset_seconds)

    # ------------------------------------------------------------------ #
    # The waveform, for audio
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Audio cleanup
    # ------------------------------------------------------------------ #

    def _sync_audio_chain_to_player(self, *, is_audio: bool) -> None:
        """Make the player match the panel, or clear it when the panel does not apply.

        Called on every load, because the two were otherwise only in step by luck and
        drifted apart in both directions:

        * opening a video left the recording's cleanup applied to the video's audio
          while the panel was hidden -- filtered audio with nothing on screen saying so,
          which is the one outcome this whole feature exists to prevent;
        * and opening another recording left the panel saying processing was on while
          the newly loaded file had nothing applied, so an entry written then would have
          recorded a cleanup the reviewer never actually heard.

        The chain is deliberately kept across recordings: somebody working through a
        folder from one recorder wants the same cleanup on each. It is re-applied here
        rather than assumed to have survived.
        """
        backend = self._mpv_backend
        if backend is None:
            return

        wanted = self.audio_dock.chain().to_spec() if is_audio else ""
        if not backend.set_audio_filters(wanted):
            log.warning("The player refused the audio chain %r on load", wanted)
            backend.set_audio_filters("")
            if is_audio:
                self.audio_dock.set_chain(AudioChain())

    def _on_audio_chain_changed(self, chain: AudioChain) -> None:
        """Apply the cleanup to playback, and say in the status bar what is being heard.

        If mpv refuses the chain, the panel is put back to the recording rather than
        left claiming processing is on. Believing you are listening to filtered audio
        when you are not is its own kind of wrong, and the opposite mistake -- thinking
        it is raw when it is filtered -- is the one this whole feature guards against.
        """
        backend = self._mpv_backend
        if backend is None:
            return

        spec = chain.to_spec()
        if not backend.set_audio_filters(spec):
            log.warning("The player refused the audio chain %r", spec)
            backend.set_audio_filters("")
            self.audio_dock.set_chain(AudioChain())
            QMessageBox.warning(
                self,
                "That cleanup could not be applied",
                "The player would not accept those settings, so the recording is "
                "playing unprocessed.\n\nNothing has been changed on disk.",
            )
            return

        # The shape follows what is being heard, after the dialling settles.
        if self._media_context is not None and self._media_context.kind == "audio":
            self._waveform_chain_timer.start()

        if chain.is_processing:
            self.statusBar().showMessage(f"Hearing processed audio: {chain.describe()}", 8000)
        else:
            self.statusBar().showMessage("Hearing the recording as it was", 4000)

    def toggle_audio_processing(self) -> None:
        """Switch the cleanup off and on. The comparison that catches an artefact."""
        if not self.audio_dock.isVisible():
            return
        self.audio_dock.toggle_bypass()

    def jump_to_next_sound(self) -> None:
        """Seek to the next moment that stands out above the noise floor.

        What somebody wants on opening three hours of near-silence: not a seek bar, but
        a way to walk the places worth listening to. Only moments well above the floor
        are offered, so this never sends the reviewer to empty air.
        """
        waveform = self.waveform_view._waveform
        if waveform is None:
            return
        moments = waveform.loudest_moments(count=60, apart=1.5)
        if not moments:
            self.statusBar().showMessage(
                "Nothing in this recording stands out above the noise floor", 5000
            )
            return

        here = self.current_playback_position() or 0.0
        ahead = [m for m in moments if m > here + 0.4]
        target = ahead[0] if ahead else moments[0]
        self._seek_absolute(target)
        self.waveform_view.zoom_to_span(target, target + 2.0)
        position = moments.index(target) + 1
        self.statusBar().showMessage(
            f"Sound {position} of {len(moments)} at {format_timecode(target)}"
            + ("" if ahead else " — wrapped to the first"),
            6000,
        )

    def _adjust_waveform_gain(self, factor: float) -> None:
        """Amplify the picture of the recording, never the recording.

        A session with one door slam in it leaves everything else a thin line, and the
        whisper somebody is hunting for is inside that line.
        """
        self.waveform_view.set_gain(self.waveform_view.gain() * factor)
        self.statusBar().showMessage(
            f"Waveform shown at {self.waveform_view.gain():.1f}x - the audio itself is unchanged",
            4000,
        )

    def _start_waveform(self, path: Path, *, keep_picture: bool = False) -> None:
        """Read the shape of a recording, from the cache if it is there.

        Cheap when cached and slow when not, so it always goes to a worker and the view
        says what it is doing in the meantime. A three-hour session takes about twenty
        seconds the first time.
        """
        self._stop_waveform()
        if not keep_picture:
            self.waveform_view.set_waveform(None)
            self.waveform_view.set_processed(None)
            self.waveform_view.set_reading(None)
            self._waveform_chain = ""
        else:
            # The old shape stays up, so the note is the only thing saying it is not
            # the one being heard. Three hours takes minutes to re-read.
            self.waveform_view.set_reading("Re-reading the shape…")
        self.waveform_view.set_message(f"Reading the shape of {path.name}…")

        chain = self._waveform_chain_spec()
        worker = WaveformWorker(path, self._paths.cache_dir, self, audio_filters=chain)
        worker.waveform_ready.connect(self._on_waveform_ready)
        worker.waveform_failed.connect(self._on_waveform_failed)
        worker.progress.connect(self._on_waveform_progress)
        worker.finished.connect(lambda w=worker: self._on_waveform_finished(w))
        worker.finished.connect(worker.deleteLater)
        self._waveform_worker = worker
        worker.start()

    def _waveform_chain_spec(self) -> str:
        """The filter chain the envelope should be read through, or "" for the file.

        Only for recordings: a video's waveform is not drawn at all, and the panel is
        hidden there. Time changes are excluded -- the envelope is drawn against the
        recording's own clock, and a slowed read would put every peak at the wrong
        moment, so the picture and the playhead would disagree and a span dragged on it
        would mark the wrong seconds. Bypass yields "" through ``to_spec``, which is
        what puts the view back on the recording when the reviewer compares.
        """
        if self._media_context is None or self._media_context.kind != "audio":
            return ""
        return self.audio_dock.chain().without_time_changes().to_spec()

    def _waveform_chain_description(self) -> str | None:
        """Words for the badge on the view, or None when it is the recording itself."""
        if not self._waveform_chain_spec():
            return None
        return self.audio_dock.chain().without_time_changes().describe()

    def _reread_waveform_for_chain(self) -> None:
        """Redraw the shape to match what is being heard, once the dialling has stopped.

        The reviewer's own choice: the waveform follows the cleanup. It is the whole
        point of cleaning up an EVP recording -- the event they are hunting is inside a
        line that hum and hiss have flattened, and with the cleanup applied it stands
        well clear of the floor. Because that changes what the picture *means*, it comes
        with the badge; the two are set together and never separately.

        The old envelope is left on screen while the new one is read, rather than
        blanking: a reviewer mid-way through examining a peak should not lose it for
        twenty seconds because they adjusted a cutoff.
        """
        if self._media_context is None or self._media_context.kind != "audio":
            return
        if self._waveform_chain_spec() == self._waveform_chain:
            return
        self._start_waveform(self._media_context.path, keep_picture=True)

    def _stop_waveform(self) -> None:
        """Abandon any read in flight. The reviewer has opened something else."""
        for attribute in ("_waveform_worker", "_detail_worker"):
            worker = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if worker is None:
                continue
            try:
                worker.cancel()
                worker.wait(2000)
            except RuntimeError:
                # Already deleted by Qt; even isRunning() raises on one of those.
                pass

    def _on_waveform_finished(self, worker: WaveformWorker) -> None:
        if self._waveform_worker is worker:
            self._waveform_worker = None

    def _on_waveform_progress(self, media_path: str, fraction: float) -> None:
        if self._media_context is None or str(self._media_context.path) != media_path:
            return
        self.waveform_view.set_message(
            f"Reading the shape of {Path(media_path).name}…  {fraction * 100:.0f}%"
        )
        if self.waveform_view.reading() is not None:
            self.waveform_view.set_reading(
                f"Re-reading the shape…  {fraction * 100:.0f}%"
            )

    def _on_waveform_ready(self, media_path: str, audio_filters: str, waveform: object) -> None:
        # The reviewer may have moved on while this ran -- to another recording, or to
        # another cleanup. Either way this envelope is a picture of something that is no
        # longer on screen, and drawing it would caption it with the wrong chain.
        if self._media_context is None or str(self._media_context.path) != media_path:
            return
        if audio_filters != self._waveform_chain_spec():
            return
        self._waveform_chain = audio_filters
        self.waveform_view.set_reading(None)
        self.waveform_view.set_processed(self._waveform_chain_description())
        self.waveform_view.set_waveform(waveform)
        self.waveform_view.set_duration(waveform.duration)
        self.waveform_view.set_position(self.current_playback_position() or 0.0)
        self._refresh_marks()

    def _on_waveform_failed(self, media_path: str, reason: str) -> None:
        if self._media_context is None or str(self._media_context.path) != media_path:
            return
        log.warning("Waveform unavailable for %s: %s", media_path, reason)
        self.waveform_view.set_reading(None)
        self.waveform_view.set_message(
            f"The waveform could not be read.\n\n{reason}\n\nPlayback and logging still work."
        )

    def _on_waveform_view_changed(self, start: float, end: float) -> None:
        """Re-read the visible span when it is finer than the stored envelope."""
        if self._media_context is None or not self.waveform_view.has_waveform():
            return
        span = end - start
        width = max(1, self.waveform_view.width())

        if span > DETAIL_THRESHOLD_SECONDS:
            self.waveform_view.set_detail(None, 0.0, 0.0)
            return

        if self._detail_worker is not None:
            with contextlib.suppress(RuntimeError):
                self._detail_worker.cancel()
            self._detail_worker = None

        # Two buckets a pixel, so the drawn column is the extreme of real samples
        # rather than a guess between them.
        per_second = max(200, int(width * 2 / max(0.01, span)))
        worker = WaveformDetailWorker(
            self._media_context.path,
            start,
            end,
            per_second,
            self,
            audio_filters=self._waveform_chain,
        )
        worker.detail_ready.connect(self._on_detail_ready)
        worker.finished.connect(worker.deleteLater)
        self._detail_worker = worker
        worker.start()

    def _on_detail_ready(
        self,
        media_path: str,
        audio_filters: str,
        start: float,
        end: float,
        waveform: object,
    ) -> None:
        if self._media_context is None or str(self._media_context.path) != media_path:
            return
        # A zoomed-in read of the raw file drawn over a processed overview would put
        # unprocessed detail behind a badge saying the opposite.
        if audio_filters != self._waveform_chain:
            return
        self.waveform_view.set_detail(waveform, start, end)

    def _on_waveform_span(self, start: float, end: float) -> None:
        """A span dragged on the waveform: the reviewer heard something there.

        It becomes a pending mark, exactly as Space-Space does while watching, so the
        rest of the workflow -- Enter to write it up, P/N to step through, R to repeat
        -- is the same whether the event was tagged by ear or by eye.
        """
        if end - start < 0.01:
            return
        mark = EventMark(start=start, end=end)
        self._pending_marks.append(mark)
        self._open_mark_start = None
        self.transport.set_marking(False)
        self._refresh_marks()
        self._select_mark(self._index_of(mark))
        self._seek_absolute(start)
        self.statusBar().showMessage(
            f"Marked {format_timecode(start)} to {format_timecode(end)}. "
            "Press Enter to write it up.",
            6000,
        )

    def _reload_annotations(self) -> None:
        """Hand the overlay every mark made against the recording now on screen.

        Marks from all of this recording's entries, not just one: a reviewer watching
        a file expects to see everything that has been drawn on it, which is also what
        a whole-recording export would burn in.
        """
        if self._media_context is None:
            self._annotation_overlay.set_annotations([])
            return
        self._annotation_overlay.set_annotations(
            self._store.annotations_for_media(
                str(self._media_context.path), case_id=self._settings.review.case_id
            )
        )

    def _set_annotations_visible(self, visible: bool) -> None:
        """Show or hide the marks on screen. Nothing is written either way."""
        self._annotation_overlay.set_enabled(visible)
        self.statusBar().showMessage(
            "Annotations shown" if visible else "Annotations hidden (they are still saved)",
            4000,
        )

    def _annotate_entry(self, entry: EntryRow) -> None:
        """Draw over the frame this event happens on."""
        if entry.media_kind.value != "video" or entry.event_offset_seconds is None:
            QMessageBox.information(
                self,
                "Nothing to annotate",
                "Annotations are drawn over a picture, and this entry has none.\n\n"
                "They are available for video. For a recording, the waveform already "
                "shows where the event is.",
            )
            return

        frame = self._frame_for(entry)
        if frame is None:
            return

        dialog = AnnotationEditor(
            entry,
            frame=frame,
            annotations=self._store.annotations_for(entry.entry_id),
            parent=self,
        )
        if dialog.exec() != AnnotationEditor.DialogCode.Accepted:
            return
        saved = dialog.saved_annotations
        if saved is None:
            return

        self._store.save_annotations(entry.entry_id, saved)
        self._reload_annotations()
        self.refresh_log()
        self.statusBar().showMessage(
            f"{len(saved)} mark(s) saved against this entry — the recording is unchanged",
            6000,
        )

    def _frame_for(self, entry: EntryRow) -> QImage | None:
        """The picture at the entry's moment, to draw on.

        Taken from the player, which has the file open and decoded. The media is loaded
        and seeked first if the reviewer is annotating an entry from a recording that is
        not the one on screen.
        """
        target = Path(entry.media_path)
        if not target.is_file():
            QMessageBox.warning(
                self,
                "Media not found",
                f"The recording for this entry is not where the entry says it is:\n\n{target}",
            )
            return None

        if self._media_context is None or self._media_context.path != target:
            self.load_media(target, seek_to=entry.event_offset_seconds)
        else:
            self._seek_absolute(entry.event_offset_seconds or 0.0)

        backend = self._mpv_backend
        if backend is None:
            QMessageBox.warning(
                self,
                "Playback unavailable",
                "The frame could not be captured because playback is not available.",
            )
            return None

        # Let the seek land before grabbing the frame, or the picture is whatever was
        # on screen beforehand -- which would be a mark drawn over the wrong moment.
        backend.pause()
        deadline = QDeadlineTimer(4000)
        while not deadline.hasExpired():
            QApplication.processEvents()
            if backend.video_size():
                break

        shot = self._paths.cache_dir / "annotate_frame.png"
        if not backend.capture_frame(shot):
            QMessageBox.warning(
                self,
                "Frame not captured",
                "The current frame could not be captured, so there is nothing to draw on.",
            )
            return None
        image = QImage(str(shot))
        return None if image.isNull() else image

    def _export_clip(self, entry: EntryRow) -> None:
        """Cut this entry's event out of its recording as a file of its own."""
        dialog = ClipExportDialog(
            entry,
            template=self._store.get_template(entry.template_id),
            annotations=self._store.annotations_for(entry.entry_id),
            default_directory=self._paths.exports_dir,
            parent=self,
        )
        if dialog.exec() != ClipExportDialog.DialogCode.Accepted:
            return
        clip = dialog.result_clip
        if clip is None:
            return

        note = f"Clip saved: {clip.path.name}"
        if clip.lead_in > 0.001:
            # Said here as well as in the sidecar, because somebody who never opens the
            # text file still needs to know the clip starts before the event.
            note += f"  —  begins {clip.lead_in:.2f}s before the marked moment"
        self.statusBar().showMessage(note, 10000)

    def _view_entry(self, entry: EntryRow) -> None:
        """Show everything one entry holds, with a way into the editor.

        Re-read from the store first. The row the table is holding was loaded when the
        log last refreshed, and a pull from the server may have moved on since.
        """
        current = self._store.get(entry.entry_id) or entry
        dialog = EntryDetailDialog(
            current,
            store=self._store,
            settings=self._settings,
            commit=self._commit_entry,
            parent=self,
        )
        dialog.goto_requested.connect(self._goto_entry)
        dialog.delete_requested.connect(self._delete_entry)
        dialog.restore_requested.connect(self._restore_entry)
        dialog.clip_requested.connect(self._export_clip)
        dialog.exec()

    def _edit_entry(self, entry: EntryRow) -> None:
        target = Path(entry.media_path)
        context = MediaContext(
            path=target,
            kind=entry.media_kind.value,
            duration_seconds=entry.media_duration_seconds,
            sha256=entry.media_sha256,
        )
        dialog = LogEntryDialog(
            context=context,
            settings=self._settings,
            store=self._store,
            event_offset_seconds=entry.event_offset_seconds,
            snapshot_path=entry.snapshot_path,
            existing=entry,
            parent=self,
        )
        if dialog.exec() == LogEntryDialog.DialogCode.Accepted and dialog.entry is not None:
            self._commit_entry(dialog.entry)

    def _refresh_entry_actions(self) -> None:
        """Enable the entry commands only when there is an entry for them to act on."""
        # isHidden, not isVisible: the question is whether the reviewer closed the Log
        # panel, and isVisible is also false for a window that simply has not been
        # shown yet -- which would leave every command dead on startup.
        has_entry = not self.log_dock.isHidden() and self.log_dock.selected_entry() is not None
        for action in getattr(self, "_entry_actions", ()):
            action.setEnabled(has_entry)
        annotate = getattr(self, "_annotate_action", None)
        if annotate is not None:
            # Needs a video, not a selection.
            annotate.setEnabled(
                self._media_context is not None and self._media_context.kind == "video"
            )

    def _selected_log_entry(self) -> EntryRow | None:
        """The entry the Review menu acts on, surfacing the Log panel if hidden.

        A hidden panel means no visible list and so no selection; showing it is
        more use than an error about there being nothing selected.
        """
        if not self.log_dock.isVisible():
            self.log_dock.show()
            self.statusBar().showMessage("Select an entry in the Log panel first", 5000)
            return None
        entry = self.log_dock.selected_entry()
        if entry is None:
            # The menu entries are greyed out without a selection, so reaching here
            # means something else asked -- and a silent refusal is what made Delete
            # look broken in the first place.
            QMessageBox.information(
                self,
                "Nothing selected",
                "Select an entry in the Log panel first, then try again.",
            )
        return entry

    def _edit_selected_entry(self) -> None:
        entry = self._selected_log_entry()
        if entry is not None:
            self._edit_entry(entry)

    def _annotate_selected_entry(self) -> None:
        entry = self._selected_log_entry()
        if entry is not None:
            self._annotate_entry(entry)

    def annotate_here(self) -> None:
        """Draw over the moment on screen, whether or not a row is selected.

        Marks belong to a logged event -- that is what makes them part of the record
        rather than a doodle -- but requiring the reviewer to log one, find it in the
        Session Log and right-click it before they can draw meant nothing on screen
        while watching said the feature existed. This takes the obvious entry if there
        is one and otherwise starts the event, which is the step they would have had to
        take anyway.
        """
        if self._media_context is None:
            self.statusBar().showMessage("Open a video before annotating", 4000)
            return
        if self._media_context.kind != "video":
            QMessageBox.information(
                self,
                "Nothing to annotate",
                "Annotations are drawn over a picture.\n\n"
                "They are available for video. For a recording, the waveform already "
                "shows where the event is.",
            )
            return

        entry = self._entry_to_annotate()
        if entry is not None:
            self._annotate_entry(entry)
            return

        # No event here yet. Log one first -- the annotation needs something to belong
        # to -- and go straight into drawing if it is saved.
        before = {row.entry_id for row in self._store.list_entries(
            case_id=self._settings.review.case_id,
            media_path=str(self._media_context.path),
        )}
        self.open_log_dialog()
        after = self._store.list_entries(
            case_id=self._settings.review.case_id,
            media_path=str(self._media_context.path),
        )
        fresh = [row for row in after if row.entry_id not in before]
        if fresh:
            self._annotate_entry(fresh[0])

    def _entry_to_annotate(self) -> EntryRow | None:
        """The event the playhead is in, or the row the reviewer has selected.

        Selection wins: if they have picked a row, that is the one they mean, even if
        the player happens to sit elsewhere.
        """
        selected = self.log_dock.selected_entry() if not self.log_dock.isHidden() else None
        if selected is not None and selected.media_kind.value == "video":
            return selected

        if self._media_context is None:
            return None
        position = self.current_playback_position() or 0.0
        candidates = [
            row
            for row in self._store.list_entries(
                case_id=self._settings.review.case_id,
                media_path=str(self._media_context.path),
            )
            if row.event_offset_seconds is not None and row.media_kind.value == "video"
        ]
        for row in candidates:
            start = row.event_offset_seconds or 0.0
            end = row.event_end_seconds if row.event_end_seconds is not None else start
            # A moment's grace either side: nobody parks the playhead on the exact
            # frame they logged, and an event with no duration is a single instant.
            if start - 1.0 <= position <= max(end, start) + 1.0:
                return row
        return None

    def _delete_selected_entry(self) -> None:
        entry = self._selected_log_entry()
        if entry is not None:
            self._delete_entry(entry)

    def _delete_entry(self, entry: EntryRow) -> None:
        """Flag an entry deleted, and say plainly that it is still there.

        The old wording explained the soft delete and then left the row to vanish with
        no way to see it again, which reads as "gone" however carefully it is phrased.
        """
        template = self._store.get_template(entry.template_id)
        headline = entry.summary(template) if template else entry.file_name
        confirmation = QMessageBox.question(
            self,
            "Delete entry",
            f"Delete this entry?\n\n    {headline[:90]}\n\n"
            "Evidence records are never erased. It is flagged as deleted and kept, and "
            "the deletion is recorded on the server. Tick Show deleted in the log to "
            "see it again, or to put it back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation is not QMessageBox.StandardButton.Yes:
            return
        self._store.soft_delete(entry.entry_id)
        self.refresh_log()
        self.statusBar().showMessage(
            "Entry deleted. It is kept - tick Show deleted in the log to restore it.", 8000
        )
        if self._sync_worker is not None:
            self._sync_worker.request_sync()

    def _restore_entry(self, entry: EntryRow) -> None:
        """Put a deleted entry back.

        The counterpart soft_delete always implied. Without it the confirmation above
        was true of the database and false of the application.
        """
        self._store.restore(entry.entry_id)
        self.refresh_log()
        self.statusBar().showMessage("Entry restored.", 5000)
        if self._sync_worker is not None:
            self._sync_worker.request_sync()

    # ------------------------------------------------------------------ #
    # File and folder opening
    # ------------------------------------------------------------------ #

    def _open_file_dialog(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(ALL_MEDIA_EXTENSIONS))
        start = self._settings.player.last_directory or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open evidence file",
            start,
            f"Media files ({patterns});;All files (*)",
        )
        if path:
            folder = str(Path(path).parent)
            if self.playlist_dock.current_path() is None:
                self.playlist_dock.open_folder(folder)
            self.load_media(path)

    def _open_folder_dialog(self) -> None:
        start = self._settings.player.last_directory or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Open evidence folder", start)
        if not folder:
            return
        count = self.playlist_dock.open_folder(folder)
        self.playlist_dock.setVisible(True)
        self._settings.player.last_directory = folder
        self.statusBar().showMessage(
            f"{count} media file{'' if count == 1 else 's'} found in {folder}", 6000
        )
        if count:
            self.playlist_dock.step(1)

    # -- drag and drop ------------------------------------------------------ #

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                self.playlist_dock.open_folder(path)
                self.playlist_dock.setVisible(True)
                self.playlist_dock.step(1)
                return
            if path.is_file():
                self.load_media(path)
                return

    # ------------------------------------------------------------------ #
    # Export, settings, sync
    # ------------------------------------------------------------------ #

    def _export_log(self) -> None:
        """Export the case. If the log is filtered, say so and ask which set.

        This used to export `log_dock.model.entries` - whatever the filter box
        happened to be showing - and say "Exported N entries" either way. A
        reviewer who had left a word in the filter handed over an evidence export
        silently missing most of the case.
        """
        case_id = self._settings.review.case_id
        everything = self._store.list_entries(case_id=case_id)
        shown = self.log_dock.model.entries
        filter_text = self.log_dock.search_edit.text().strip()

        if not everything:
            QMessageBox.information(
                self,
                "Nothing to export",
                f"{self._settings.review.case_name} has no entries yet.",
            )
            return

        entries = everything
        if filter_text and len(shown) != len(everything):
            answer = QMessageBox.question(
                self,
                "The log is filtered",
                f"The log is filtered by “{filter_text}”, showing {len(shown)} of "
                f"{len(everything)} entries.\n\nExport the whole case?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer is QMessageBox.StandardButton.No:
                entries = shown
                if not entries:
                    QMessageBox.information(
                        self,
                        "Nothing to export",
                        f"No entries match “{filter_text}”.",
                    )
                    return

        default_name = (
            f"{safe_filename(self._settings.review.case_name)}_"
            f"{utc_now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
        target, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export log",
            str(self._paths.exports_dir / default_name),
            "Excel workbook (*.xlsx);;CSV file (*.csv)",
        )
        if not target:
            return
        if not Path(target).suffix:
            target += ".csv" if "CSV" in selected_filter else ".xlsx"

        try:
            written = export_entries(entries, target, self._templates_for(entries))
        except Exception as exc:
            log.exception("Export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        # Say when the file is a subset, so the number in it is never a surprise
        # to whoever receives it.
        scope = "" if len(entries) == len(everything) else f" (filtered from {len(everything)})"
        self.statusBar().showMessage(f"Exported {len(entries)} entries{scope} to {written}", 8000)

    def _edit_templates(self) -> None:
        """Open the template editor, then pick up whatever it changed.

        The log is refreshed unconditionally: the columns come from the case's
        template, so a field added or renamed in there changes the table even when the
        entries themselves have not moved.
        """
        dialog = TemplateEditorDialog(
            self._store, case_id=self._settings.review.case_id, parent=self
        )
        dialog.exec()
        self.refresh_log()
        # Templates are shared, so an edit is something colleagues need. Pushing it now
        # rather than on the next idle cycle means the person who asked for the change
        # can see it land.
        if self._sync_worker is not None:
            self._sync_worker.request_sync()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._settings, self, store=self._store)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        self._ensure_case_exists()
        self.apply_shortcuts()
        self._refresh_status_labels()
        self.transport.set_jump_step(self._settings.player.jump_step_seconds)
        self.refresh_log()
        self.restart_sync()

    def _ensure_case_exists(self) -> None:
        review = self._settings.review
        self._store.upsert_case(Case(case_id=review.case_id, name=review.case_name))

    def _refresh_status_labels(self) -> None:
        review = self._settings.review
        self.case_button.setText(f"  Case: {review.case_name}  ")
        self.investigator_label.setText(f"  {review.investigator_name}  ")

    # -- switching between cases -------------------------------------------- #

    def _rebuild_case_menu(self) -> None:
        """Built on open, so a case added on another screen shows up without a restart."""
        self.case_menu.clear()
        current = self._settings.review.case_id
        counts = self._store.case_entry_counts()

        for case in sorted(self._store.list_cases(), key=lambda c: c.name.casefold()):
            count = counts.get(case.case_id, 0)
            action = self.case_menu.addAction(f"{case.name}  ({count})")
            action.setCheckable(True)
            action.setChecked(case.case_id == current)
            # Default arguments, or every action would close over the last case.
            action.triggered.connect(
                lambda _checked=False, cid=case.case_id, name=case.name: self.switch_case(cid, name)
            )

        self.case_menu.addSeparator()
        self.case_menu.addAction("New case…", self._create_case)
        self.case_menu.addAction("Rename this case…", self._rename_case)

    def _rename_case(self) -> None:
        """Change the case's display name, keeping its id.

        The id is deliberately not editable: it is on every entry already, it is
        the snapshot folder name, and the server keys on it. Renaming is the safe
        half of what the old Settings boxes allowed.
        """
        review = self._settings.review
        name, accepted = QInputDialog.getText(
            self, "Rename case", "Case name", text=review.case_name
        )
        name = name.strip()
        if not accepted or not name or name == review.case_name:
            return

        review.case_name = name
        self._settings.save()
        self._store.upsert_case(Case(case_id=review.case_id, name=name))
        self._refresh_status_labels()
        # So the server shows the new name too, rather than only this machine.
        self.restart_sync()
        self.statusBar().showMessage(f"Renamed to {name}", 4000)

    def _create_case(self) -> None:
        name, accepted = QInputDialog.getText(self, "New case", "Case name")
        name = name.strip()
        if not accepted or not name:
            return

        case_id = slugify_case_id(name, max_length=60)
        if not case_id:
            QMessageBox.warning(
                self,
                "That name cannot be used",
                "A case name needs at least one letter or digit.",
            )
            return

        existing = {case.case_id for case in self._store.list_cases()}
        if case_id in existing and case_id != self._settings.review.case_id:
            # Two names can slug to one id. Switching to it is almost certainly
            # what was meant, and it beats silently merging two cases.
            QMessageBox.information(
                self,
                "That case already exists",
                f"A case with the id '{case_id}' already exists. Switching to it.",
            )
        self.switch_case(case_id, name)

    def switch_case(self, case_id: str, case_name: str) -> None:
        """Make `case_id` the active case: what the log shows and new entries join."""
        review = self._settings.review
        if case_id == review.case_id and case_name == review.case_name:
            return

        review.case_id = case_id
        review.case_name = case_name
        self._settings.save()

        self._ensure_case_exists()
        self._refresh_status_labels()
        self.refresh_log()
        self._refresh_marks()
        # The worker is built around one case, so it has to be rebuilt. The pull
        # cursor is stored on the case, so this resumes rather than re-pulls.
        self.restart_sync()
        self.statusBar().showMessage(f"Now logging to {case_name}", 4000)

    # -- sync --------------------------------------------------------------- #

    def start_sync(self) -> None:
        if not self._settings.api.is_configured:
            self._set_sync_indicator("disabled", self._store.pending_count(), "Remote sync is off")
            return
        worker = SyncWorker(
            self._store,
            self._settings.api,
            get_api_token(),
            case_id=self._settings.review.case_id,
            parent=self,
        )
        worker.status_changed.connect(self._set_sync_indicator)
        worker.remote_merged.connect(self._on_remote_merged)
        worker.subscription_required.connect(self._on_subscription_required)
        self._sync_worker = worker
        worker.start()

    def stop_sync(self) -> None:
        if self._sync_worker is None:
            return
        self._sync_worker.stop()
        if not self._sync_worker.wait(5000):
            log.warning("Sync worker did not stop cleanly; terminating")
            self._sync_worker.terminate()
            self._sync_worker.wait(1000)
        self._sync_worker = None

    def restart_sync(self) -> None:
        self.stop_sync()
        self.start_sync()

    def _sync_now(self) -> None:
        if self._sync_worker is None:
            QMessageBox.information(
                self,
                "Sync is off",
                "Enable a server in Settings → Server to upload entries.",
            )
            return
        # request_sync is the whole job now: a cycle pushes and pulls.
        self._sync_worker.request_sync()
        self.statusBar().showMessage("Syncing…", 3000)

    def _set_sync_indicator(self, state: str, pending: int, message: str) -> None:
        glyph, colour = SYNC_INDICATORS.get(state, ("?", "#8b949e"))
        caption = SYNC_CAPTIONS.get(state, "")
        if pending:
            # "Sync off - 2 waiting" is the whole problem stated in the corner of
            # the window, which is where someone waiting on an upload will look.
            # With no caption - the healthy case, mid-upload - this used to be a
            # bare "3", which answers nothing: three of what?
            caption = f"{caption} - {pending} waiting" if caption else f"{pending} waiting"
        suffix = f" {caption}" if caption else ""
        self.sync_label.setText(f"  {glyph}{suffix}  ")
        self.sync_label.setStyleSheet(f"color:{colour}; font-weight:600;")
        self.sync_label.setToolTip(message)
        if state in ("error", "offline", "unsubscribed"):
            self.statusBar().showMessage(message, 6000)

    def _on_subscription_required(
        self, message: str, reason: str, upgrade_url: str
    ) -> None:
        """The token is fine; the account behind it is not paying.

        Said once per reason, and only to somebody who has turned syncing on --
        they asked for this to work, so being told why it does not is an answer
        rather than an advertisement. Everything else in the application carries
        on regardless, and the wording says so: nothing is lost, nothing is
        locked, the work is still on this machine.
        """
        if self._subscription_prompt_reason == (reason or "?"):
            return
        self._subscription_prompt_reason = reason or "?"

        token_problem = reason in ("token_not_linked", "no_account")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Syncing is paused")
        box.setText(message or "A subscription is needed to sync.")
        # For a token problem nothing resumes by itself: this token will never be
        # accepted again, and saying "it will upload once syncing resumes" sent
        # people to wait for something that could not happen.
        resumes = (
            "will upload once a new token is entered under Settings → Sync."
            if token_problem
            else "will upload by itself once syncing resumes."
        )
        box.setInformativeText(
            "Everything else keeps working. Your entries, annotations, clips and "
            "audio cleanup are all on this computer as usual, and anything waiting "
            + resumes
        )
        if upgrade_url:
            # The server sends a token problem to the page that issues tokens, a
            # failed payment to the profile where the card is updated, and anything
            # else to the plans. The button has to say which, or "See plans" opens
            # a token page and looks like a sales pitch to somebody already paying.
            if token_problem:
                label = "Get a new token"
            elif reason == "payment_failed":
                label = "Update payment method"
            else:
                label = "See plans"
            see = box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is see:
                QDesktopServices.openUrl(QUrl(upgrade_url))
        else:
            box.exec()

    def _on_remote_merged(self, count: int) -> None:
        self.statusBar().showMessage(f"Merged {count} entries from the server", 5000)
        self.refresh_log()

    # ------------------------------------------------------------------ #
    # Help
    # ------------------------------------------------------------------ #

    def _show_shortcuts(self) -> None:
        self._build_shortcuts_dialog().exec()

    def _build_shortcuts_dialog(self) -> QDialog:
        """The key reference, generated from the live bindings.

        A QMessageBox held this before: thirty actions in five tables, in a box
        that cannot scroll and cannot be resized, so on a laptop the bottom of the
        list was simply unreachable. This is the application's own answer to
        "which key does that", so it has to survive a short screen.

        Built separately from being shown, so it can be checked without a modal
        loop that nothing would ever answer.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard shortcuts")
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        body = QTextBrowser()
        body.setOpenExternalLinks(False)
        body.setHtml(self.shortcuts.help_html())
        layout.addWidget(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.setSizeGripEnabled(True)
        dialog.resize(560, 620)
        fit_to_screen(dialog)
        return dialog

    def _show_about(self) -> None:
        from ..media.mpv_loader import describe_availability

        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            "<p>Review evidence media and record timestamped observations.</p>"
            f"<p style='color:#9aa3ad'>{describe_availability()}<br>"
            f"Database: {self._paths.database}</p>",
        )

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #

    def _restore_geometry(self) -> None:
        ui = self._settings.ui
        restored = False
        if ui.window_geometry:
            restored = self.restoreGeometry(QByteArray.fromBase64(ui.window_geometry.encode()))
        if ui.window_state:
            self.restoreState(QByteArray.fromBase64(ui.window_state.encode()))

        # Either way the result has to fit the screen in front of us: a first run
        # sizes to content, and a remembered geometry may have come from a larger
        # monitor that is no longer attached.
        fit_to_screen(self, desired=self.size() if restored else QSize(1100, 720))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        pending = self._store.pending_count()
        if pending and self._settings.api.is_configured:
            answer = QMessageBox.question(
                self,
                "Entries not yet uploaded",
                f"{pending} entr{'y is' if pending == 1 else 'ies are'} still waiting to "
                "upload. They are saved safely on this computer and will be sent the next "
                "time the app runs.\n\nQuit now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                # Staying is the safe answer, so it is the one Enter gives. Delete
                # and the blocked-keys prompt already default this way.
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        ui = self._settings.ui
        ui.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        ui.window_state = bytes(self.saveState().toBase64()).decode()
        ui.playlist_dock_visible = self.playlist_dock.isVisible()
        ui.log_dock_visible = self.log_dock.isVisible()
        try:
            self._settings.save()
        except OSError:
            log.warning("Could not persist settings on exit", exc_info=True)

        self._stop_hashing()
        self.stop_sync()
        if self._mpv_backend is not None:
            self._mpv_backend.shutdown()
        self._store.close()

        QApplication.processEvents()
        event.accept()
