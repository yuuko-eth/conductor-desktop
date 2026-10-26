#!/usr/bin/env bash
# Install system deps for GNOME on Wayland (Ubuntu, Debian, Fedora, Arch):
# capture and clipboard tools, AT-SPI, ydotool 1.x with its daemon, flat
# pointer acceleration, the accessibility bus, the unsafe_mode extension (on
# request), and .venv to match the system python3-gi.
#
#   CONDUCTOR_GNOME_UNSAFE_MODE=1  install the unsafe_mode extension, no prompt
#   CONDUCTOR_GNOME_UNSAFE_MODE=0  skip it, no prompt
#   CONDUCTOR_NO_VENV=1            don't create .venv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/linux-common.sh
. "$SCRIPT_DIR/lib/linux-common.sh"
# shellcheck source=lib/ydotool.sh
. "$SCRIPT_DIR/lib/ydotool.sh"

echo "==> Installing deps for GNOME Wayland..."

if command -v apt &>/dev/null; then
  sudo apt update
  # libgirepository: 2.0-dev on Ubuntu 24.04+, 1.0-dev on 22.04/Debian 12
  # (for the PyGObject build on 22.04; see venv_setup).
  GI_DEV="libgirepository-2.0-dev"
  if ! apt-cache show "$GI_DEV" &>/dev/null; then
    GI_DEV="libgirepository1.0-dev"
  fi
  # shellcheck disable=SC2046
  sudo apt install -y \
    wl-clipboard \
    gnome-screenshot \
    at-spi2-core \
    python3-gi \
    gir1.2-atspi-2.0 \
    gir1.2-ayatanaappindicator3-0.1 \
    libayatana-appindicator3-1 \
    "$GI_DEV" \
    libcairo2-dev \
    pkg-config \
    $(ydotool_build_deps apt)
elif command -v dnf &>/dev/null; then
  # shellcheck disable=SC2046
  sudo dnf install -y \
    wl-clipboard \
    gnome-screenshot \
    at-spi2-core \
    python3-gobject \
    libappindicator-gtk3 \
    gobject-introspection-devel \
    cairo-gobject-devel \
    $(ydotool_build_deps dnf)
elif command -v pacman &>/dev/null; then
  # shellcheck disable=SC2046
  sudo pacman -S --needed --noconfirm \
    wl-clipboard \
    gnome-screenshot \
    at-spi2-core \
    python-gobject \
    libayatana-appindicator \
    $(ydotool_build_deps pacman)
else
  echo "Unsupported package manager. Install by hand:"
  echo "  wl-clipboard, gnome-screenshot, at-spi2-core, python3-gi, gir1.2-atspi-2.0,"
  echo "  and the ydotool 1.x build deps (cmake, libevdev, scdoc, a C++ compiler)"
  exit 1
fi

echo ""
ydotool_install
ydotool_service

echo ""
echo "==> Verifying tools..."
MISSING=0
for cmd in wl-paste wl-copy gnome-screenshot gdbus; do
  echo -n "  $cmd: "
  if command -v "$cmd" &>/dev/null; then echo "OK"; else echo "MISSING"; MISSING=1; fi
done
ydotool_check || MISSING=1

echo ""
a11y_enable

echo ""
echo "==> Pointer acceleration..."
# ydotool moves the pointer with relative steps, and libinput's default curve
# scales them (about 2x at agent speeds), so clicks overshoot. Flat makes the
# steps 1:1 with screen pixels.
if command -v gsettings &>/dev/null; then
  CUR=$(gsettings get org.gnome.desktop.peripherals.mouse accel-profile 2>/dev/null || echo unknown)
  if [ "$CUR" != "'flat'" ]; then
    echo "  Setting org.gnome.desktop.peripherals.mouse accel-profile = flat (was $CUR)"
    gsettings set org.gnome.desktop.peripherals.mouse accel-profile flat
  else
    echo "  accel-profile already flat"
  fi
fi

echo ""
echo "==> unsafe_mode..."
# The window list, focus, workspace tracking and the screenshot service go
# through GNOME Shell's D-Bus API, which GNOME 41+ refuses unless
# global.context.unsafe_mode is on. The flag resets at every login; the
# extension sets it again at each one. It lowers a GNOME safeguard (any
# process on the session bus can then run code in the shell), so it's
# offered, not assumed.
GNOME_MAJOR=$(gnome-shell --version 2>/dev/null | awk '{print $3}' | cut -d. -f1 || true)
echo "  GNOME Shell ${GNOME_MAJOR:-unknown}. GNOME 41+ needs unsafe_mode for windows"
echo "  and capture; it lowers a GNOME safeguard, so install it only on a machine"
echo "  set aside for an agent."
ANSWER="${CONDUCTOR_GNOME_UNSAFE_MODE:-}"
# -r is true even with no terminal attached; opening it is the test.
if [ -z "$ANSWER" ] && { : < /dev/tty; } 2>/dev/null; then
  printf '  Install the unsafe_mode extension now? [y/N] ' > /dev/tty
  read -r reply < /dev/tty || reply=""
  case "$reply" in [yY]*) ANSWER=1 ;; *) ANSWER=0 ;; esac
fi
if [ "$ANSWER" = 1 ]; then
  "$SCRIPT_DIR/install-gnome-unsafe-mode.sh"
else
  echo "  Skipped. Later: setup/install-gnome-unsafe-mode.sh"
fi

if [ "$MISSING" -ne 0 ]; then
  echo ""
  echo "WARNING: Some tools are missing. The agent will have limited capabilities."
fi

venv_setup "$REPO_DIR"
echo ""
ydotool_client_env_hint
echo ""
echo "==> Done. Log out and back in once, so the extension and the a11y bus"
echo "    apply to the whole session."
