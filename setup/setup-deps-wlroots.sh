#!/usr/bin/env bash
# Install system deps for wlroots compositors on Wayland (Hyprland, sway,
# labwc, Wayfire, river, Cosmic; Raspberry Pi OS runs labwc): capture and
# clipboard tools, AT-SPI, ydotool 1.x with its daemon, the accessibility bus,
# and .venv to match the system python3-gi. CONDUCTOR_NO_VENV=1 skips the venv.
#
# Hyprland and sway report windows through their own IPC (hyprctl, swaymsg).
# The others expose only titles, through wlrctl, which is installed when the
# distro packages it and skipped otherwise (Arch: it's in the AUR).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/linux-common.sh
. "$SCRIPT_DIR/lib/linux-common.sh"
# shellcheck source=lib/ydotool.sh
. "$SCRIPT_DIR/lib/ydotool.sh"

echo "==> Installing deps for wlroots Wayland..."

# ydotool comes through ydotool_build_deps: the source-build deps on apt
# (Debian, Ubuntu and Pi OS package 0.x, which aborts on absolute moves), the
# packaged 1.x on dnf and pacman.
if command -v apt &>/dev/null; then
  sudo apt update
  # libgirepository: 2.0-dev on Ubuntu 24.04+, 1.0-dev on 22.04/Debian 12
  GI_DEV="libgirepository-2.0-dev"
  if ! apt-cache show "$GI_DEV" &>/dev/null; then
    GI_DEV="libgirepository1.0-dev"
  fi
  # shellcheck disable=SC2046
  sudo apt install -y \
    wl-clipboard \
    grim \
    at-spi2-core \
    python3-gi \
    gir1.2-atspi-2.0 \
    gir1.2-ayatanaappindicator3-0.1 \
    libayatana-appindicator3-1 \
    "$GI_DEV" \
    libcairo2-dev \
    pkg-config \
    $(ydotool_build_deps apt)
  sudo apt install -y wlrctl 2>/dev/null || echo "  wlrctl: not in the repos (only titles-only compositors need it)"
elif command -v dnf &>/dev/null; then
  # shellcheck disable=SC2046
  sudo dnf install -y \
    wl-clipboard \
    grim \
    at-spi2-core \
    python3-gobject \
    libappindicator-gtk3 \
    gobject-introspection-devel \
    cairo-gobject-devel \
    $(ydotool_build_deps dnf)
  sudo dnf install -y wlrctl 2>/dev/null || echo "  wlrctl: not in the repos (only titles-only compositors need it)"
elif command -v pacman &>/dev/null; then
  # shellcheck disable=SC2046
  sudo pacman -S --needed --noconfirm \
    wl-clipboard \
    grim \
    at-spi2-core \
    python-gobject \
    libayatana-appindicator \
    $(ydotool_build_deps pacman)
  command -v wlrctl &>/dev/null || echo "  wlrctl: in the AUR, not pacman (only titles-only compositors need it)"
else
  echo "Unsupported package manager. Install by hand:"
  echo "  wl-clipboard, grim, at-spi2-core, python3-gi, gir1.2-atspi-2.0,"
  echo "  the ydotool 1.x build deps (cmake, libevdev, scdoc, a C++ compiler),"
  echo "  and wlrctl for labwc / Wayfire / river / Cosmic"
  exit 1
fi

echo ""
ydotool_install
ydotool_service

echo ""
echo "==> Verifying tools..."
MISSING=0
for cmd in wl-paste wl-copy grim; do
  echo -n "  $cmd: "
  if command -v "$cmd" &>/dev/null; then echo "OK"; else echo "MISSING"; MISSING=1; fi
done
ydotool_check || MISSING=1
echo -n "  window list: "
if command -v hyprctl &>/dev/null; then echo "hyprctl (Hyprland)"
elif command -v swaymsg &>/dev/null; then echo "swaymsg (sway)"
elif command -v wlrctl &>/dev/null; then echo "wlrctl (titles only)"
else echo "none found: install wlrctl for labwc / Wayfire / river / Cosmic"
fi

echo ""
a11y_enable

echo ""
echo "  Screenshots: grim reads the compositor directly (wlr-screencopy)."

if [ "$MISSING" -ne 0 ]; then
  echo ""
  echo "WARNING: Some tools are missing. The agent will have limited capabilities."
fi

venv_setup "$REPO_DIR"
echo ""
ydotool_client_env_hint
echo ""
echo "==> Done."
