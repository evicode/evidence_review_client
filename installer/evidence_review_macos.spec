# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for Evidence Review on macOS.

Produces `Evidence Review.app`, which build_macos.sh then wraps in a .dmg.

Kept separate from the Windows spec rather than branching inside it: the two
differ in the native library they bundle, the keyring backend they need, the
icon format, and the fact that this one emits an .app bundle. One file trying to
be both would be harder to read than two that are each honest about one target.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller

CONSOLE = os.environ.get("EVREV_BUILD_CONSOLE") == "1"
SRC = REPO / "src"
ENTRY = SRC / "evidence_review" / "__main__.py"

# --------------------------------------------------------------------------- #
# libmpv
# --------------------------------------------------------------------------- #
# Looked for in vendor/mpv first, as on Windows, then wherever Homebrew put it.
# media/mpv_loader.py already searches for libmpv.2.dylib and libmpv.dylib, so
# nothing in the application needs to change for macOS.
binaries = []


def _find_libmpv() -> Path | None:
    names = ("libmpv.2.dylib", "libmpv.dylib")
    vendored = REPO / "vendor" / "mpv"
    for name in names:
        candidate = vendored / name
        if candidate.is_file():
            return candidate

    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.run(
                [brew, "--prefix", "mpv"], capture_output=True, text=True, timeout=30
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            prefix = ""
        if prefix:
            for name in names:
                candidate = Path(prefix) / "lib" / name
                if candidate.is_file():
                    return candidate

    for base in ("/opt/homebrew/lib", "/usr/local/lib"):
        for name in names:
            candidate = Path(base) / name
            if candidate.is_file():
                return candidate
    return None


libmpv = _find_libmpv()
if libmpv is not None:
    # Lands at Contents/Frameworks/mpv/, which mpv_loader searches.
    binaries.append((str(libmpv), "mpv"))
else:
    print("=" * 74)
    print(" WARNING: libmpv was not found.")
    print(" The build will succeed but video and audio playback will not work.")
    print(" Install it first:  brew install mpv")
    print("=" * 74)

datas = []
icns = REPO / "installer" / "app.icns"
png = SRC / "evidence_review" / "resources" / "app.png"
ico = SRC / "evidence_review" / "resources" / "app.ico"
# Qt reads the window icon from the package at runtime; whichever of these
# exists gets carried so the icon is not missing in the dock.
for source in (png, ico):
    if source.is_file():
        datas.append((str(source), "evidence_review/resources"))

# --------------------------------------------------------------------------- #
# Imports PyInstaller cannot see
# --------------------------------------------------------------------------- #
hiddenimports = [
    "evidence_review",
    "evidence_review.app",
    "mpv",
    # Keychain, rather than the Windows Credential Manager. keyring picks this
    # itself at runtime; it is named here so PyInstaller actually ships it.
    "keyring.backends.macOS",
    "keyring.backends.fail",
    "keyring.backends.null",
    "evidence_review.media.mpv_backend",
]

excludes = [
    "tkinter",
    "mutagen",
    "PIL",
    "unittest",
    "pydoc_data",
    "test",
    # The Windows keyring backend and its ctypes shim have no meaning here.
    "keyring.backends.Windows",
    "win32ctypes",
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
    name="Evidence Review",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    # Left to the machine doing the build. Set EVREV_TARGET_ARCH to "universal2"
    # if the environment has universal wheels for every dependency; it does not
    # by default, and a mismatch fails late and confusingly.
    target_arch=os.environ.get("EVREV_TARGET_ARCH") or None,
    codesign_identity=os.environ.get("EVREV_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Evidence Review",
)

app = BUNDLE(  # noqa: F821
    coll,
    name="Evidence Review.app",
    icon=str(icns) if icns.is_file() else None,
    bundle_identifier="org.evidencereview.client",
    info_plist={
        "CFBundleName": "Evidence Review",
        "CFBundleDisplayName": "Evidence Review",
        "CFBundleShortVersionString": os.environ.get("EVREV_VERSION", "1.1.0"),
        "CFBundleVersion": os.environ.get("EVREV_VERSION", "1.1.0"),
        # Without this macOS runs the app at 1x and scales it up, which on a
        # Retina display makes video review pointlessly soft.
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # Evidence lives in folders the user picks, and macOS gates those behind
        # a prompt. Without a usage string the prompt has no explanation on it.
        "NSDesktopFolderUsageDescription":
            "Evidence Review opens media files you choose for review.",
        "NSDocumentsFolderUsageDescription":
            "Evidence Review opens media files you choose for review.",
        "NSDownloadsFolderUsageDescription":
            "Evidence Review opens media files you choose for review.",
        "NSRemovableVolumesUsageDescription":
            "Evidence is often reviewed directly from a card reader or external drive.",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "Evidence media",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": [
                    "public.movie",
                    "public.audio",
                    "public.image",
                ],
            }
        ],
    },
)
