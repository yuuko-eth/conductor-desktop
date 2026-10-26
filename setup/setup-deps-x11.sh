#!/usr/bin/env bash
# Install system deps for Linux X11 (any desktop on an X11 session, SteamOS
# Desktop Mode), turn on the accessibility bus, and create .venv to match the
# system python3-gi. CONDUCTOR_NO_VENV=1 skips the venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/linux-common.sh
. "$SCRIPT_DIR/lib/linux-common.sh"

echo "==> Installing deps for Linux X11..."

if command -v apt &>/dev/null; then
  sudo apt update
  # libgirepository: 2.0-dev on Ubuntu 24.04+, 1.0-dev on 22.04/Debian 12
  GI_DEV="libgirepository-2.0-dev"
  if ! apt-cache show "$GI_DEV" &>/dev/null; then
    GI_DEV="libgirepository1.0-dev"
  fi
  sudo apt install -y \
    wmctrl \
    x11-utils \
    xdotool \
    xclip \
    scrot \
    at-spi2-core \
    python3-gi \
    python3-xlib \
    gir1.2-atspi-2.0 \
    gir1.2-ayatanaappindicator3-0.1 \
    libayatana-appindicator3-1 \
    "$GI_DEV" \
    libcairo2-dev \
    pkg-config
elif command -v dnf &>/dev/null; then
  sudo dnf install -y \
    wmctrl \
    xwininfo \
    xdotool \
    xclip \
    scrot \
    at-spi2-core \
    python3-gobject \
    python3-xlib \
    libappindicator-gtk3 \
    gobject-introspection-devel \
    cairo-gobject-devel
elif command -v pacman &>/dev/null; then
  # Arch / SteamOS Desktop Mode. On SteamOS, /usr is read-only and
  # atomic updates wipe pacman-installed binaries — see
  # https://docs.conductor-mcp.io/platforms/linux/steamos/ for the post-update re-run incantation.
  if command -v steamos-readonly &>/dev/null; then
    echo "  Detected SteamOS — disabling /usr read-only for install..."
    sudo steamos-readonly disable
    # Re-init keyring if it was wiped by an atomic update.
    if ! sudo pacman-key --list-keys archlinux >/dev/null 2>&1; then
      echo "  Initialising pacman keyring (wiped by atomic update?)..."
      sudo pacman-key --init
      sudo pacman-key --populate archlinux holo
    fi
  fi
  sudo pacman -S --needed --noconfirm \
    wmctrl \
    xorg-xwininfo \
    xdotool \
    xclip \
    scrot \
    at-spi2-core \
    python-gobject \
    python-xlib \
    libayatana-appindicator \
    gobject-introspection
  if command -v steamos-readonly &>/dev/null; then
    sudo steamos-readonly enable
    echo "  SteamOS: re-enabled /usr read-only. After the next OS update,"
    echo "  re-run this script (or just 'pacman -S wmctrl') to restore deps."
  fi
else
  echo "Unsupported package manager. Install manually:"
  echo "  wmctrl, xwininfo, xdotool, xclip, at-spi2-core, python3-gi, python3-xlib, gir1.2-atspi-2.0"
  exit 1
fi

echo ""
echo "==> Verifying tools..."
MISSING=0
for cmd in wmctrl xdotool xclip scrot; do
  echo -n "  $cmd: "
  if command -v "$cmd" &>/dev/null; then echo "OK"; else echo "MISSING"; MISSING=1; fi
done

echo ""
a11y_enable

echo ""
echo "==> Permissions check..."
echo "  Screenshots: No special permissions needed on X11."
echo "  Input (xdotool): No special permissions needed on X11."
echo "  Window mgmt (wmctrl): No special permissions needed on X11."
echo ""

if [ "$MISSING" -ne 0 ]; then
  echo "WARNING: Some tools are missing. The agent will have limited capabilities."
fi

venv_setup "$REPO_DIR"
echo ""
echo "==> Done."
