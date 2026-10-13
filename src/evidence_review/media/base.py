"""The interface the UI talks to, regardless of which engine is rendering."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal


class MediaBackend(QObject):
    """Common surface for the mpv (video/audio) and Qt (still image) backends.

    Position and duration are in seconds. Every property change arrives as a signal
    on the UI thread, so callers never poll.
    """

    #: Current playback position, seconds.
    position_changed = Signal(float)
    #: Total duration, seconds. Emits 0.0 for stills.
    duration_changed = Signal(float)
    #: True when paused.
    paused_changed = Signal(bool)
    #: A file finished loading; carries the resolved path.
    loaded = Signal(str)
    #: Playback reached the end of the file.
    ended = Signal()
    #: Non-fatal problem worth showing the user.
    error = Signal(str)
    #: Container/tag metadata became available.
    metadata_changed = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path: Path | None = None

    # -- state -------------------------------------------------------------- #

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def supports_playback(self) -> bool:
        """False for still images, which have no transport."""
        return True

    # -- lifecycle ---------------------------------------------------------- #

    def load(self, path: str | Path) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        """Release engine resources. Safe to call more than once."""

    # -- transport ---------------------------------------------------------- #

    def play(self) -> None: ...
    def pause(self) -> None: ...
    def toggle_pause(self) -> None: ...

    def is_paused(self) -> bool:
        return True

    def seek_absolute(self, seconds: float) -> None: ...
    def seek_relative(self, seconds: float) -> None: ...
    def frame_step(self, backwards: bool = False) -> None: ...

    def position(self) -> float:
        return 0.0

    def duration(self) -> float:
        return 0.0

    # -- audio / rate ------------------------------------------------------- #

    def set_ab_loop(self, start: float, end: float) -> None:
        """Loop playback between two points, for replaying a tagged event."""

    def clear_ab_loop(self) -> None:
        """Stop looping."""

    def set_volume(self, volume: int) -> None: ...
    def set_muted(self, muted: bool) -> None: ...
    def set_speed(self, speed: float) -> None: ...

    # -- capture ------------------------------------------------------------ #

    def capture_frame(self, target: str | Path) -> bool:
        """Write the currently displayed frame to ``target``. Returns success."""
        return False

    def metadata(self) -> dict:
        return {}
