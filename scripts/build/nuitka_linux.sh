#!/usr/bin/env bash
# Build conductor-desktop as a Linux single-file binary using Nuitka.
#
# TEMPLATE — not yet verified end-to-end on Linux. The macOS build
# (nuitka_macos.sh) is the proven reference.
#
# Output: dist/conductor-desktop  (single file via --onefile)
# Build time estimate: 10-20min depending on host CPU.
#
# Usage:
#   scripts/build/nuitka_linux.sh                  # x11 backend (default)
#   BACKEND=wlroots scripts/build/nuitka_linux.sh  # wlroots/sway/wayfire
#   BACKEND=gnome scripts/build/nuitka_linux.sh    # gnome-wayland (see caveat)
#   SLIM=1 scripts/build/nuitka_linux.sh           # drop imagehash → -35MB
#   NO_GI=1 scripts/build/nuitka_linux.sh          # portable, no AT-SPI2
#
# PY= MATTERS on the gi flavor: the binary loads the TARGET's system python3-gi
# at runtime, so the frozen interpreter's ABI must match what that distro ships
# (22.04→3.10, 24.04→3.12, Debian 12→3.11). Mismatch degrades SILENTLY.
#
# CAVEAT — PyGObject (gi):
#   The AT-SPI2 backends (linux_x11.py, linux_gnome.py, linux_wlroots.py)
#   import `gi.repository.Atspi`, which loads GIR XML at runtime against
#   system-installed *.typelib files. Nuitka cannot freeze that — the
#   compiled binary will still depend on `python3-gi` + libgirepository
#   + the AT-SPI2 typelibs being installed on the target host.
#
#   Two strategies:
#     (1) "Mostly static" — accept the gi runtime dependency. Document
#         that targets need `apt install python3-gi gir1.2-atspi-2.0`
#         (or your distro's equivalent). Smaller binary.
#     (2) "Truly portable" — pass NO_GI=1 to drop AT-SPI2 entirely. The
#         resulting binary uses only subprocess tools (xdotool, wmctrl,
#         wlrctl, wl-clipboard). Loses semantic accessibility tree
#         querying — describe() falls back to OCR/visual.
#
# Requires: Python 3.12+, uv, scripts/setup-deps-{x11,wlroots,gnome-wayland}.sh
# already run on the build host. Run from conductor-desktop/.
set -euo pipefail

cd "$(dirname "$0")/../.."  # → conductor-desktop/

OUT_DIR="${OUT_DIR:-dist}"
PY="${PY:-python3.12}"
ENTRY="scripts/build/conductor_desktop_entry.py"

# Desktop backend -> pyproject extra. pyproject defines exactly TWO Linux
# extras (linux-x11, linux-wayland), so wlroots/gnome must MAP onto
# linux-wayland instead of being interpolated straight in: ".[linux-wlroots]"
# and ".[linux-gnome]" do not exist, and the install below used to swallow that
# with a silent `|| install -e .` fallback — so the documented invocations
# quietly built something other than what they asked for.
#
# Both extras are currently EMPTY: the X11/wlroots/GNOME differences are system
# packages on the USER's machine (https://docs.conductor-mcp.io/platforms/linux/), not build inputs.
# So this axis does not fork the artifact — one binary per arch x flavor serves
# all three desktops. Kept wired up only so the build
# stays correct if either extra ever gains real dependencies.
BACKEND="${BACKEND:-x11}"
case "$BACKEND" in
  x11)                   BACKEND_EXTRA="linux-x11" ;;
  wayland|wlroots|gnome) BACKEND_EXTRA="linux-wayland" ;;
  *) echo "unknown BACKEND=$BACKEND (want: x11 | wayland | wlroots | gnome)" >&2; exit 2 ;;
esac

if [ ! -f "$ENTRY" ]; then
  echo "missing build shim: $ENTRY" >&2
  exit 1
fi

# 1. Isolated build venv. On Linux you usually want --system-site-packages
# so apt's python3-gi is reachable (PyGObject from PyPI needs
# libgirepository-2.0-dev, which is Ubuntu 24.04+ only).
BUILD_VENV="${BUILD_VENV:-/tmp/conductor-desktop-build-venv}"
if [ ! -x "$BUILD_VENV/bin/python" ]; then
  echo ">> creating build venv at $BUILD_VENV (with system site-packages)"
  uv venv --python "$PY" --system-site-packages "$BUILD_VENV"
fi
uv pip install --python "$BUILD_VENV/bin/python" -q nuitka ordered-set zstandard
# No `|| install -e .` fallback: BACKEND_EXTRA is now guaranteed to name a real
# extra, so a failure here is a genuine build-env problem and must be loud.
uv pip install --python "$BUILD_VENV/bin/python" -q -e ".[${BACKEND_EXTRA}]"

# 2. Compose Nuitka args.
mkdir -p "$OUT_DIR"
ARGS=(
  --onefile                         # bootstrap-extracts to /tmp on first run
  --standalone
  --output-dir="$OUT_DIR"
  --output-filename=conductor-desktop
  --assume-yes-for-downloads
  --include-package=conductor_desktop
  --include-package-data=conductor_desktop   # tray PNGs (assets/) + dashboard static
  --include-package=mcp
  --nofollow-import-to=tkinter
  --nofollow-import-to=test
  --nofollow-import-to=unittest
  --nofollow-import-to=pytest
  --remove-output
  --no-progressbar
)

# Drop AT-SPI2 path entirely if requested.
if [ "${NO_GI:-0}" = "1" ]; then
  ARGS+=(--nofollow-import-to=gi)
  ARGS+=(--nofollow-import-to=dbus_fast)
  echo ">> NO_GI=1 — building without AT-SPI2 (subprocess tools only)"
fi

# Slim: drop imagehash + scipy + pywt (~35MB).
if [ "${SLIM:-0}" = "1" ]; then
  ARGS+=(--nofollow-import-to=imagehash --nofollow-import-to=scipy --nofollow-import-to=pywt)
fi

ARGS+=(--main="$(pwd)/$ENTRY")

echo ">> compiling…"
"$BUILD_VENV/bin/python" -m nuitka "${ARGS[@]}"

BIN="$OUT_DIR/conductor-desktop"
echo
echo ">> done: $BIN"
du -sh "$BIN" 2>/dev/null || true

cat <<EOF

Runtime requirements on the target host:
  - X11 backend:     xdotool, wmctrl, xclip
  - wlroots backend: wlrctl, wl-clipboard
  - gnome backend:   gnome-shell, dbus
  - For AT-SPI2 (unless built with NO_GI=1):
      apt install python3-gi gir1.2-atspi-2.0  # or distro equiv
  - libX11 / libwayland-client at the right ABI

Smoke test:
  CONDUCTOR_DESKTOP_GROUNDER_URL=http://your-vlm:8000 ./$BIN
EOF
