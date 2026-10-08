"""libmpv-backed playback for video and audio.

mpv renders directly into a native child window handed to it via ``wid``, which is
why :class:`MpvSurface` forces a native window handle. Property changes arrive on
mpv's own event thread; we re-emit them as Qt signals, which Qt delivers to the UI
thread as queued connections.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from .base import MediaBackend
from .mpv_loader import MpvNotAvailable, import_mpv

log = logging.getLogger(__name__)


class MpvSurface(QWidget):
    """A black, native child window for mpv to draw into."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.setPalette(palette)
        self.setMinimumSize(320, 180)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def window_id(self) -> int:
        return int(self.winId())


class MpvBackend(MediaBackend):
    """Playback engine wrapping libmpv."""

    def __init__(
        self,
        surface: MpvSurface,
        *,
        hardware_decoding: str = "auto-safe",
        volume: int = 100,
        muted: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._surface = surface
        self._alive = False
        self._duration = 0.0
        self._position = 0.0
        self._paused = True
        self._metadata: dict = {}

        mpv_module = import_mpv()
        self._mpv_module = mpv_module

        # Force the native handle to exist before mpv is told about it.
        window_id = surface.window_id()

        try:
            self._mpv = mpv_module.MPV(
                wid=str(window_id),
                vo="gpu",
                hwdec=hardware_decoding or "no",
                keep_open="yes",  # hold the last frame so it can still be logged
                idle="yes",
                osc=False,
                osd_level=0,
                terminal=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                input_cursor=False,
                cursor_autohide="no",
                border=False,
                force_window="no",
                volume=max(0, min(int(volume), 130)),
                mute="yes" if muted else "no",
                log_handler=self._on_mpv_log,
                loglevel="error",
            )
        except Exception as exc:
            raise MpvNotAvailable(f"libmpv failed to initialise: {exc}") from exc

        self._alive = True
        self._install_observers()

    # -- setup -------------------------------------------------------------- #

    def _install_observers(self) -> None:
        self._mpv.observe_property("time-pos", self._on_time_pos)
        self._mpv.observe_property("duration", self._on_duration)
        self._mpv.observe_property("pause", self._on_pause)
        self._mpv.observe_property("eof-reached", self._on_eof)
        self._mpv.observe_property("metadata", self._on_metadata)

    def _on_mpv_log(self, level: str, prefix: str, text: str) -> None:
        message = text.strip()
        if not message:
            return
        if level in ("fatal", "error"):
            log.error("mpv[%s] %s", prefix, message)
            self.error.emit(message)
        else:
            log.debug("mpv[%s] %s", prefix, message)

    # -- property observers (called on mpv's event thread) ------------------ #

    def _on_time_pos(self, _name: str, value: float | None) -> None:
        if value is None:
            return
        self._position = float(value)
        self.position_changed.emit(self._position)

    def _on_duration(self, _name: str, value: float | None) -> None:
        self._duration = float(value or 0.0)
        self.duration_changed.emit(self._duration)

    def _on_pause(self, _name: str, value: bool | None) -> None:
        self._paused = bool(value)
        self.paused_changed.emit(self._paused)

    def _on_eof(self, _name: str, value: bool | None) -> None:
        if value:
            self.ended.emit()

    def _on_metadata(self, _name: str, value: dict | None) -> None:
        self._metadata = dict(value or {})
        self.metadata_changed.emit(self._metadata)

    # -- lifecycle ---------------------------------------------------------- #

    def load(self, path: str | Path) -> None:
        target = Path(path)
        self._path = target
        self._duration = 0.0
        self._position = 0.0
        self._metadata = {}
        self._command("loadfile", str(target), "replace")
        self.loaded.emit(str(target))

    def stop(self) -> None:
        self._command("stop")
        self._path = None
        self._position = 0.0
        self._duration = 0.0
        self.position_changed.emit(0.0)
        self.duration_changed.emit(0.0)

    def shutdown(self) -> None:
        if not self._alive:
            return
        self._alive = False
        try:
            self._mpv.terminate()
        except Exception:
            log.debug("mpv terminate raised during shutdown", exc_info=True)

    # -- transport ---------------------------------------------------------- #

    def play(self) -> None:
        self._set("pause", False)

    def pause(self) -> None:
        self._set("pause", True)

    def toggle_pause(self) -> None:
        self._set("pause", not self._paused)

    def is_paused(self) -> bool:
        return self._paused

    def seek_absolute(self, seconds: float) -> None:
        target = max(0.0, float(seconds))
        if self._duration:
            target = min(target, max(self._duration - 0.001, 0.0))
        self._command("seek", str(target), "absolute", "exact")

    def seek_relative(self, seconds: float) -> None:
        self._command("seek", str(float(seconds)), "relative", "exact")

    def frame_step(self, backwards: bool = False) -> None:
        self._command("frame-back-step" if backwards else "frame-step")

    def position(self) -> float:
        return self._position

    def duration(self) -> float:
        return self._duration

    # -- audio / rate ------------------------------------------------------- #

    def set_ab_loop(self, start: float, end: float) -> None:
        """Use mpv's own A-B loop, which is frame-accurate and costs us nothing.

        Polling the position and seeking back would drift and stutter; mpv loops
        internally at the demuxer level.
        """
        if end <= start:
            self.clear_ab_loop()
            return
        self._set("ab-loop-a", float(start))
        self._set("ab-loop-b", float(end))

    def clear_ab_loop(self) -> None:
        self._set("ab-loop-a", "no")
        self._set("ab-loop-b", "no")

    def set_volume(self, volume: int) -> None:
        self._set("volume", max(0, min(int(volume), 130)))

    def set_muted(self, muted: bool) -> None:
        self._set("mute", bool(muted))

    def set_speed(self, speed: float) -> None:
        self._set("speed", max(0.05, min(float(speed), 8.0)))

    # -- capture ------------------------------------------------------------ #

    def capture_frame(self, target: str | Path) -> bool:
        """Write the current frame with no OSD or subtitles burned in."""
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        # "video" means the decoded frame only, without subtitles or OSD.
        if not self._command("screenshot-to-file", str(path), "video"):
            return False
        return path.is_file()

    def metadata(self) -> dict:
        return dict(self._metadata)

    # -- tracks ------------------------------------------------------------- #

    def track_list(self) -> list[dict]:
        if not self._alive:
            return []
        try:
            return list(self._mpv.track_list or [])
        except Exception:  # noqa: BLE001
            return []

    # -- overlays ----------------------------------------------------------- #
    #
    # mpv draws into a native child window, which on Windows is composited over
    # anything Qt places on top of it. So annotations are handed to mpv itself rather
    # than painted over the player: mpv composites them with the video it is already
    # drawing, which also means they survive fullscreen and resizing for free.

    #: Ours, so nothing else mpv draws gets clobbered.
    OVERLAY_ID = 7

    def video_size(self) -> tuple[int, int] | None:
        """The decoded picture size, or None before a frame has arrived."""
        width = self._get("dwidth")
        height = self._get("dheight")
        if not width or not height:
            return None
        return int(width), int(height)

    def osd_geometry(self) -> tuple[int, int, int, int] | None:
        """Where the picture sits in the window: (left, top, width, height).

        An overlay is positioned in window coordinates, but an annotation is positioned
        against the picture. With the video letterboxed they are not the same thing, and
        mpv reports the margins precisely so there is no need to guess.
        """
        dimensions = self._get("osd-dimensions")
        if not dimensions:
            return None
        try:
            width = int(dimensions["w"]) - int(dimensions["ml"]) - int(dimensions["mr"])
            height = int(dimensions["h"]) - int(dimensions["mt"]) - int(dimensions["mb"])
            left, top = int(dimensions["ml"]), int(dimensions["mt"])
        except (KeyError, TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return left, top, width, height

    def show_overlay(self, path: str | Path, left: int, top: int, width: int, height: int) -> bool:
        """Composite a BGRA bitmap over the video.

        python-mpv's own FileOverlay helper cannot be used: its update() passes ten
        positional arguments to a nine-parameter method, so it raises before mpv is
        ever reached. The underlying command is called directly instead.
        """
        if not self._alive:
            return False
        try:
            self._mpv.overlay_add(
                self.OVERLAY_ID, int(left), int(top), str(path), 0, "bgra",
                int(width), int(height), int(width) * 4,
            )
            return True
        except self._mpv_module.ShutdownError:
            self._alive = False
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv overlay failed: %s", exc)
            return False

    def clear_overlay(self) -> bool:
        if not self._alive:
            return False
        try:
            self._mpv.overlay_remove(self.OVERLAY_ID)
            return True
        except self._mpv_module.ShutdownError:
            self._alive = False
            return False
        except Exception:  # noqa: BLE001
            # Removing an overlay that was never added is not worth reporting.
            return False

    def set_audio_filters(self, spec: str) -> bool:
        """Apply a filter chain to playback, or clear it with "".

        Live: the reviewer can switch processing on and off while listening, which is
        what makes comparing against the recording possible at all. Nothing is written
        to the file.

        Returns whether mpv took it. A chain it refuses leaves playback unfiltered, and
        the caller must not then tell the reviewer that processing is on -- believing
        you are hearing processed audio when you are not is its own kind of wrong.
        """
        if not self._alive:
            return False
        try:
            self._mpv.af = spec or ""
            return True
        except self._mpv_module.ShutdownError:
            self._alive = False
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv refused the audio filter chain %r: %s", spec, exc)
            return False

    def audio_filters(self) -> str:
        """What mpv reports as applied, for checking the display against reality."""
        applied = self._get("af")
        if not applied:
            return ""
        if isinstance(applied, str):
            return applied
        try:
            return ",".join(str(item.get("name", "")) for item in applied)
        except (AttributeError, TypeError):  # pragma: no cover
            return ""

    def _get(self, name: str) -> object:
        if not self._alive:
            return None
        try:
            return getattr(self._mpv, name.replace("-", "_"))
        except self._mpv_module.ShutdownError:
            self._alive = False
            return None
        except Exception:  # noqa: BLE001
            return None

    def _command(self, *args: str) -> bool:
        if not self._alive:
            return False
        try:
            self._mpv.command(*args)
            return True
        except self._mpv_module.ShutdownError:
            self._alive = False
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv command %s failed: %s", args, exc)
            self.error.emit(str(exc))
            return False

    def _set(self, name: str, value: object) -> bool:
        if not self._alive:
            return False
        try:
            setattr(self._mpv, name.replace("-", "_"), value)
            return True
        except self._mpv_module.ShutdownError:
            self._alive = False
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv property %s=%r failed: %s", name, value, exc)
            return False
