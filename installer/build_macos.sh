#!/usr/bin/env bash
#
# Build Evidence Review for macOS.
#
#   1. Create an isolated build environment.
#   2. Check libmpv is available (Homebrew), since playback depends on it.
#   3. Run PyInstaller, producing dist/Evidence Review.app.
#   4. Wrap it in dist/EvidenceReview-<version>.dmg with a drag-to-Applications
#      layout, which is what a Mac user expects an installer to look like.
#
# Mirrors build.ps1 on Windows. Must run on macOS: PyInstaller freezes for the
# platform it is running on, and hdiutil only exists here.
#
#   ./build_macos.sh                 build the .app and the .dmg
#   ./build_macos.sh --app-only      stop after the .app
#   ./build_macos.sh --console       attach stdout/stderr, to see a startup crash
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
dist_dir="$here/dist"
build_dir="$here/build"
venv_dir="$here/.venv-build-macos"

app_only=0
for arg in "$@"; do
    case "$arg" in
        --app-only) app_only=1 ;;
        --console)  export EVREV_BUILD_CONSOLE=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This builds the macOS package and has to run on macOS." >&2
    echo "For Windows use installer/build.ps1." >&2
    exit 1
fi

# The version lives in pyproject.toml, as it does for the Windows build.
version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$repo_root/pyproject.toml" | head -1)"
if [[ -z "$version" ]]; then
    echo "Could not read the version from pyproject.toml." >&2
    exit 1
fi
export EVREV_VERSION="$version"

echo
echo "  Evidence Review $version - macOS build"
echo "  repo: $repo_root"
echo

# -- libmpv ------------------------------------------------------------------ #
# Not fatal: the spec warns and builds anyway, so a broken Homebrew does not
# block producing something testable. Playback simply will not work.
if ! ls /opt/homebrew/lib/libmpv*.dylib /usr/local/lib/libmpv*.dylib \
        "$repo_root"/vendor/mpv/libmpv*.dylib >/dev/null 2>&1; then
    echo "  libmpv not found. Install it with:  brew install mpv"
    echo "  Continuing; the build will have no video or audio playback."
    echo
fi

# -- build environment -------------------------------------------------------- #
echo "  [1/4] build environment"
rm -rf "$build_dir"
if [[ ! -d "$venv_dir" ]]; then
    python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$venv_dir/bin/python" -m pip install --quiet --disable-pip-version-check -e "$repo_root"
"$venv_dir/bin/python" -m pip install --quiet --disable-pip-version-check "pyinstaller~=6.11"

# -- freeze -------------------------------------------------------------------- #
echo "  [2/4] PyInstaller"
rm -rf "$dist_dir/Evidence Review.app" "$dist_dir/Evidence Review"
"$venv_dir/bin/python" -m PyInstaller \
    --noconfirm \
    --distpath "$dist_dir" \
    --workpath "$build_dir" \
    "$here/evidence_review_macos.spec"

app_path="$dist_dir/Evidence Review.app"
if [[ ! -d "$app_path" ]]; then
    echo "  PyInstaller did not produce the .app." >&2
    exit 1
fi
echo "  built: $app_path"

# -- ad-hoc signature ---------------------------------------------------------- #
# Not a Developer ID signature and not notarised, so Gatekeeper will still warn.
# It is here because an unsigned bundle on Apple silicon is refused outright
# rather than merely warned about, which looks like a corrupt download.
echo "  [3/4] ad-hoc signature"
identity="${EVREV_CODESIGN_IDENTITY:--}"
codesign --force --deep --sign "$identity" "$app_path" 2>/dev/null \
    && echo "  signed with: $identity" \
    || echo "  codesign failed; the app will still run after a Gatekeeper prompt"

if [[ "$app_only" == "1" ]]; then
    echo
    echo "  Done. Stopped before the .dmg as asked."
    exit 0
fi

# -- disk image ---------------------------------------------------------------- #
echo "  [4/4] disk image"
dmg_path="$dist_dir/EvidenceReview-$version.dmg"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

cp -R "$app_path" "$staging/"
# The drag-to-Applications gesture every Mac user already knows.
ln -s /Applications "$staging/Applications"

rm -f "$dmg_path"
hdiutil create \
    -volname "Evidence Review $version" \
    -srcfolder "$staging" \
    -ov -format UDZO \
    "$dmg_path" >/dev/null

size="$(du -h "$dmg_path" | cut -f1)"
echo
echo "  installer built: $dmg_path ($size)"
echo
echo "  Unsigned and un-notarised: on first launch macOS will refuse it, and the"
echo "  user has to right-click the app and choose Open, or allow it under"
echo "  System Settings > Privacy & Security. A Developer ID certificate and"
echo "  notarisation are what remove that step."
echo
