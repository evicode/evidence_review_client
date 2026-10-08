"""Locate and load ``libmpv`` before ``python-mpv`` is imported.

``python-mpv`` finds the library with ``ctypes.util.find_library``, which on Windows
walks ``PATH`` looking for an exact filename. So the job here is to find whichever
build is present, make sure a file named ``mpv-2.dll`` exists beside it, and put
that directory on ``PATH`` and the DLL search path.

Failures raise :class:`MpvNotAvailable` with a message the UI can show verbatim,
rather than surfacing a ctypes traceback.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Filenames python-mpv will accept, and the name we normalise to on Windows.
WINDOWS_DLL_NAMES = ("mpv-2.dll", "libmpv-2.dll", "mpv-1.dll", "libmpv.dll")
PREFERRED_WINDOWS_NAME = "mpv-2.dll"

POSIX_LIB_NAMES = ("libmpv.so.2", "libmpv.so.1", "libmpv.so", "libmpv.2.dylib", "libmpv.dylib")

INSTALL_HINT = (
    "libmpv could not be found.\n\n"
    "The installer normally places mpv-2.dll next to the application. If you are "
    "running from source, fetch it once with:\n\n"
    "    powershell -ExecutionPolicy Bypass -File installer\\fetch_libmpv.ps1\n\n"
    "or set EVREV_MPV_DIR to the folder containing mpv-2.dll."
)


class MpvNotAvailable(RuntimeError):
    """Raised when libmpv cannot be located or loaded."""


def _candidate_directories() -> list[Path]:
    """Every place worth looking, most specific first."""
    candidates: list[Path] = []

    override = os.environ.get("EVREV_MPV_DIR")
    if override:
        candidates.append(Path(override))

    if getattr(sys, "frozen", False):
        # PyInstaller one-dir: the bundle root and its _internal folder.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.extend([Path(meipass), Path(meipass) / "mpv"])
        exe_dir = Path(sys.executable).parent
        candidates.extend([exe_dir, exe_dir / "_internal", exe_dir / "_internal" / "mpv"])

    package_root = Path(__file__).resolve().parent.parent
    candidates.append(package_root / "resources" / "mpv")

    # Development checkout: <repo>/vendor/mpv
    repo_root = package_root.parent.parent
    candidates.extend([repo_root / "vendor" / "mpv", repo_root / "vendor"])

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - unreachable path on Windows
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def find_library_file() -> Path | None:
    """Return the path to a usable libmpv binary, or ``None``."""
    names = WINDOWS_DLL_NAMES if os.name == "nt" else POSIX_LIB_NAMES

    for directory in _candidate_directories():
        if not directory.is_dir():
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate

    # Fall back to whatever is already on the system search path.
    for name in names:
        found = shutil.which(name) if os.name == "nt" else None
        if found:
            return Path(found)
    import ctypes.util

    for name in names:
        located = ctypes.util.find_library(name)
        if located:
            return Path(located)
    return None


def _normalise_windows_name(library: Path) -> Path:
    """Ensure a file literally named ``mpv-2.dll`` exists in the same folder."""
    if library.name.lower() == PREFERRED_WINDOWS_NAME:
        return library
    preferred = library.parent / PREFERRED_WINDOWS_NAME
    if preferred.is_file():
        return preferred
    try:
        shutil.copy2(library, preferred)
        log.info("Copied %s to %s for python-mpv discovery", library.name, preferred)
        return preferred
    except OSError:
        # Read-only install directory: leave the original and hope find_library
        # accepts it via PATH.
        log.warning("Could not create %s; using %s directly", preferred, library)
        return library


_loaded = False


def ensure_mpv_loadable() -> Path:
    """Prepare the environment so ``import mpv`` succeeds. Returns the library path."""
    global _loaded

    library = find_library_file()
    if library is None:
        raise MpvNotAvailable(INSTALL_HINT)

    if os.name == "nt":
        library = _normalise_windows_name(library)

    directory = str(library.parent)
    if not _loaded:
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(directory)
            except OSError:  # pragma: no cover - directory vanished
                log.warning("add_dll_directory failed for %s", directory)
        existing = os.environ.get("PATH", "")
        if directory not in existing.split(os.pathsep):
            os.environ["PATH"] = directory + os.pathsep + existing
        _loaded = True

    return library


def import_mpv():
    """Import and return the ``mpv`` module, with libmpv resolved first."""
    library = ensure_mpv_loadable()
    try:
        import mpv  # deliberately deferred until the DLL is findable
    except (OSError, ImportError) as exc:
        raise MpvNotAvailable(
            f"Found {library} but it could not be loaded.\n\n{exc}\n\n"
            "This usually means a 32-bit library was paired with 64-bit Python, "
            "or a required Visual C++ runtime is missing."
        ) from exc
    return mpv


def describe_availability() -> str:
    """Diagnostic for the Settings dialog.

    The missing case carries the install hint. Diagnostics is where the first-run
    wizard sends a reviewer whose video will not play, and it used to greet them
    with shouty capitals and no remedy - the end of a round trip rather than the
    answer to it.
    """
    try:
        library = ensure_mpv_loadable()
    except MpvNotAvailable as exc:
        return f"libmpv: not found - video playback is unavailable.\n\n{exc}"
    return f"libmpv: {library}"
