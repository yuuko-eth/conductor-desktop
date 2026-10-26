#!/usr/bin/env bash
# Build conductor-desktop as a macOS .app bundle using Nuitka.
#
# Output: dist/conductor-desktop.app  (~230MB unsigned, ~132MB binary inside)
# Build time: ~12-15min cold, ~6-8min warm (ccache).
#
# Usage:
#   scripts/build/nuitka_macos.sh                # default: full build
#   SLIM=1 scripts/build/nuitka_macos.sh         # drop imagehash → -35MB
#   ICON=path/to/icon.icns scripts/build/nuitka_macos.sh
#
# Requires: macOS 11+, Python 3.12+, uv. Run from conductor-desktop/.
#
# Why .app and not --onefile?
#   pystray's macOS backend pulls the `Foundation` pyobjc package, which
#   Nuitka refuses to compile outside `--mode=app`. Bundle is the right
#   shape anyway for a tray-using daemon.
set -euo pipefail

cd "$(dirname "$0")/../.."  # → conductor-desktop/

OUT_DIR="${OUT_DIR:-dist}"
PY="${PY:-python3.12}"
ENTRY="scripts/build/conductor_desktop_entry.py"
ICON="${ICON:-}"

if [ ! -f "$ENTRY" ]; then
  echo "missing build shim: $ENTRY" >&2
  exit 1
fi

# 1. Isolated build venv with nuitka + the macOS extras.
BUILD_VENV="${BUILD_VENV:-/tmp/conductor-desktop-build-venv}"
# Use stdlib venv + its own pip, NOT `uv pip`: this repo's pyproject sets
# `[tool.uv.pip] system = true` (for Linux apt-PyGObject), which forces uv into
# system mode from the repo dir and ignores both --python and VIRTUAL_ENV.
# Guard on pyvenv.cfg so a half-created/corrupted venv gets rebuilt.
if [ ! -f "$BUILD_VENV/pyvenv.cfg" ]; then
  echo ">> creating build venv at $BUILD_VENV"
  rm -rf "$BUILD_VENV"
  "$PY" -m venv "$BUILD_VENV"
fi
VPY="$BUILD_VENV/bin/python"
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q nuitka ordered-set zstandard
"$VPY" -m pip install -q -e ".[macos]"

# 2. Compose Nuitka args.
mkdir -p "$OUT_DIR"
ARGS=(
  --mode=app
  --output-dir="$OUT_DIR"
  --macos-app-mode=ui-element       # tray-only, no Dock icon
  --macos-app-name=conductor-desktop
  --assume-yes-for-downloads        # ccache
  --include-package=conductor_desktop
  --include-package-data=conductor_desktop   # tray PNGs (assets/) + dashboard static
  --include-package=mcp
  --nofollow-import-to=tkinter
  --nofollow-import-to=test
  --nofollow-import-to=unittest
  --nofollow-import-to=pytest
  --remove-output                   # delete .build/ after success
  --no-progressbar
)

# Optional slim build: drop imagehash (pulls scipy + pywt = ~35MB).
# imagehash is not a runtime dependency; this just guards against a
# transitive pull-in bloating the bundle.
if [ "${SLIM:-0}" = "1" ]; then
  ARGS+=(--nofollow-import-to=imagehash)
  ARGS+=(--nofollow-import-to=scipy)
  ARGS+=(--nofollow-import-to=pywt)
fi

# Optional icon — Nuitka warns without one but the build still succeeds.
if [ -n "$ICON" ]; then
  ARGS+=(--macos-app-icon="$ICON")
else
  ARGS+=(--macos-app-icon=none)     # silence the warning
fi

ARGS+=(--main="$(pwd)/$ENTRY")

echo ">> compiling…"
"$BUILD_VENV/bin/python" -m nuitka "${ARGS[@]}"

# 3. Rename the bundle to the canonical name. Nuitka picks it from the
# entry filename, which is `conductor_desktop_entry.app` in our case.
BUNDLE_ID="${BUNDLE_ID:-io.github.yuuko-eth.conductor-desktop}"  # was io.github.yuuko-eth.conductor-mcp; a new id means one TCC re-grant
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"   # '-' = ad-hoc; pass a Developer ID to distribute
SRC="$OUT_DIR/conductor_desktop_entry.app"
DST="$OUT_DIR/conductor-desktop.app"
if [ -d "$SRC" ]; then
  rm -rf "$DST"
  mv "$SRC" "$DST"
  # Also rename the inner executable so launching is intuitive.
  if [ -f "$DST/Contents/MacOS/conductor_desktop_entry" ]; then
    mv "$DST/Contents/MacOS/conductor_desktop_entry" "$DST/Contents/MacOS/conductor-desktop"
  fi

  # Keep Info.plist consistent with the renamed executable + give the bundle
  # a stable reverse-DNS identifier. Without these:
  #   - CFBundleExecutable points at the gone `conductor_desktop_entry`, so
  #     LaunchServices can't launch the bundle and System Settings refuses to
  #     add it to Accessibility / Screen-Recording ('+' validates the exec).
  #   - TCC keys grants on the signing identity; an unstable/ad-hoc-derived id
  #     means grants don't persist across rebuilds.
  PL="$DST/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleExecutable conductor-desktop" "$PL"
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$PL" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $BUNDLE_ID" "$PL"

  # Re-sign LAST. Editing Info.plist breaks Nuitka's ad-hoc seal, and the
  # in-place rename above means the original signature no longer matches the
  # bundle layout. --force replaces the existing signature; nested dylibs keep
  # the ones Nuitka applied.
  # --deep is REQUIRED: Nuitka lands package data (dashboard/static/*.js/.css,
  # .py/.cpp source) under Contents/MacOS/ instead of Contents/Resources/.
  # A shallow sign leaves those unsealed; macOS 14 codesign tolerates it but
  # macOS 26's strict validation reports "code object is not signed at all" and
  # spctl returns "no usable signature" — which makes the System Settings '+'
  # picker silently refuse the bundle. --deep seals the whole tree.
  echo ">> codesigning --deep ($CODESIGN_IDENTITY)…"
  codesign --force --deep --sign "$CODESIGN_IDENTITY" --identifier "$BUNDLE_ID" "$DST"
  codesign --verify --deep "$DST" || { echo "ERROR: codesign verify failed" >&2; exit 1; }
  # NOTE: copying a signed bundle to another Mac can perturb the seal (tar drops
  # SIP-protected xattrs; old rsync mangles many small files). If the target is
  # a newer macOS than this build host, re-run the same --deep sign ON THE
  # TARGET after transfer — that's the authoritative signature for its TCC.
fi

echo
echo ">> done: $DST"
du -sh "$DST"
echo
echo "Smoke test:"
echo "  CONDUCTOR_DESKTOP_GROUNDER_URL=http://your-vlm:8000 \\"
echo "    $DST/Contents/MacOS/conductor-desktop"
echo
echo "Distribute (Developer ID + notarize — re-uses the in-build signing step):"
echo "  CODESIGN_IDENTITY='Developer ID Application: …' scripts/build/nuitka_macos.sh"
echo "  xcrun notarytool submit ... && xcrun stapler staple $DST"
