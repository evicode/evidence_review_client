# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for Evidence Review.

One-dir build (not one-file): it starts faster, is far less likely to trip
antivirus heuristics, and lets the installer patch a single DLL without rebuilding
everything.
"""

import os
from pathlib import Path

REPO = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller

# A console build keeps stdout/stderr attached, which is the only practical way to
# see an import or startup failure in a frozen app. Set by build.ps1 -Console.
CONSOLE = os.environ.get("EVREV_BUILD_CONSOLE") == "1"
SRC = REPO / "src"
ENTRY = SRC / "evidence_review" / "__main__.py"

# --------------------------------------------------------------------------- #
# Native dependencies
# --------------------------------------------------------------------------- #
binaries = []
libmpv = REPO / "vendor" / "mpv" / "mpv-2.dll"
if libmpv.is_file():
    # Lands at _internal/mpv/mpv-2.dll, where media/mpv_loader.py looks for it.
    binaries.append((str(libmpv), "mpv"))
else:
    print("=" * 74)
    print(" WARNING: vendor/mpv/mpv-2.dll is missing.")
    print(" The build will succeed but video and audio playback will not work.")
    print(" Run installer\\fetch_libmpv.ps1 first.")
    print("=" * 74)

# ffmpeg, for cutting an event out of its recording. It lands at _internal/ffmpeg,
# where media/clip.py looks for it. The shared build is used rather than the static
# one because the static ffmpeg.exe and ffprobe.exe each embed the whole of libavcodec,
# which is twice the size for the same thing.
ffmpeg_dir = REPO / "vendor" / "ffmpeg"
ffmpeg_exe = ffmpeg_dir / "ffmpeg.exe"
if ffmpeg_exe.is_file():
    for item in sorted(ffmpeg_dir.iterdir()):
        if item.suffix.lower() in (".exe", ".dll"):
            binaries.append((str(item), "ffmpeg"))
else:
    print("=" * 74)
    print(" WARNING: vendor/ffmpeg/ffmpeg.exe is missing.")
    print(" The build will succeed but clips cannot be exported.")
    print(" Run installer\\fetch_ffmpeg.ps1 first.")
    print("=" * 74)

datas = []
icon_file = SRC / "evidence_review" / "resources" / "app.ico"
if icon_file.is_file():
    datas.append((str(icon_file), "evidence_review/resources"))

# --------------------------------------------------------------------------- #
# Windows version resource
#
# Read out of version.py rather than written here, so the Details tab in file
# properties cannot drift from what the application reports about itself. Without
# this the executable has no version at all: support cannot ask which build someone
# is running, inventory tools read nothing, and an unversioned binary counts against
# a download's reputation.
# --------------------------------------------------------------------------- #
version_info = {}
exec((SRC / "evidence_review" / "version.py").read_text(encoding="utf-8"), version_info)
APP_VERSION = version_info["APP_VERSION"]
APP_NAME = version_info["APP_NAME"]

# Windows wants four numbers. "1.2.0" becomes (1, 2, 0, 0).
_parts = [int(part) for part in APP_VERSION.split(".")[:3]]
while len(_parts) < 4:
    _parts.append(0)
VERSION_TUPLE = tuple(_parts)

version_file = Path(SPECPATH) / "build" / "version_info.txt"  # noqa: F821
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION_TUPLE},
    prodvers={VERSION_TUPLE},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{APP_NAME}'),
          StringStruct('FileDescription', '{APP_NAME}'),
          StringStruct('FileVersion', '{APP_VERSION}'),
          StringStruct('InternalName', 'EvidenceReview'),
          StringStruct('OriginalFilename', 'EvidenceReview.exe'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{APP_VERSION}'),
        ],
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""",
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# Imports PyInstaller cannot see
# --------------------------------------------------------------------------- #
hiddenimports = [
    # The entry script is executed as top-level `__main__`, so the package it
    # imports from must be collected explicitly.
    "evidence_review",
    "evidence_review.app",
    "mpv",
    "keyring.backends.Windows",
    "keyring.backends.fail",
    "keyring.backends.null",
    "win32ctypes.core",
    "win32ctypes.core.ctypes",
    "evidence_review.media.mpv_backend",
]

# Qt ships a great deal we never touch; dropping it roughly halves the install.
excludes = [
    "tkinter",
    # Both were used only by the removed wall-clock autofill. openpyxl imports
    # PIL lazily and copes without it.
    "mutagen",
    "PIL",
    "unittest",
    "pydoc_data",
    "test",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
]

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EvidenceReview",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX compression is a reliable way to get flagged by antivirus
    console=CONSOLE,     # GUI application: no console window unless debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_file) if icon_file.is_file() else None,
    version=str(version_file),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EvidenceReview",
)
