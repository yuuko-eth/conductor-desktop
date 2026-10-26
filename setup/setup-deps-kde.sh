#!/usr/bin/env bash
# Install system deps for KDE Plasma on Wayland (KDE neon, Kubuntu, Fedora KDE):
# spectacle, wl-clipboard, AT-SPI, ydotool 1.x with its daemon, the
# accessibility bus, and .venv to match the system python3-gi. Run it inside a
# Plasma session so it can check KWin and Klipper. Plasma on X11 uses
# setup-deps-x11.sh. CONDUCTOR_NO_VENV=1 skips the venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/linux-common.sh
. "$SCRIPT_DIR/lib/linux-common.sh"
# shellcheck source=lib/ydotool.sh
. "$SCRIPT_DIR/lib/ydotool.sh"

echo "==> Installing deps for KDE Plasma..."

if command -v apt &>/dev/null; then
  sudo apt update
  # KDE neon is a rolling repo on a fixed Ubuntu base. On a box that hasn't
  # been updated, installing spectacle drags in the newest Qt and frameworks
  # but leaves kwin and Plasma behind, and kwin_wayland then segfaults in
  # QKdeTheme at login (Qt 6.11 under Plasma 6.6). Make
  # the stack whole first.
  if grep -q '^ID=neon' /etc/os-release \
     && apt list --upgradable 2>/dev/null | grep -qE '^(qt6-|libqt6|kwin|plasma-|libkf6|kf6-)'; then
    echo "KDE neon has Qt/KDE upgrades pending. Installing on top of them"
    echo "mixes Qt versions and breaks the Plasma session. Update first:"
    echo "    sudo pkcon update      (or: sudo apt full-upgrade)"
    echo "then reboot and re-run this script."
    exit 1
  fi
  # libgirepository: 2.0-dev on Ubuntu 24.04+, 1.0-dev on 22.04/Debian 12
  GI_DEV="libgirepository-2.0-dev"
  if ! apt-cache show "$GI_DEV" &>/dev/null; then
    GI_DEV="libgirepository1.0-dev"
  fi
  # shellcheck disable=SC2046
  sudo apt install -y \
    kde-spectacle \
    wl-clipboard \
    at-spi2-core \
    python3-gi \
    gir1.2-atspi-2.0 \
    "$GI_DEV" \
    libcairo2-dev \
    pkg-config \
    $(ydotool_build_deps apt)
elif command -v dnf &>/dev/null; then
  # shellcheck disable=SC2046
  sudo dnf install -y \
    spectacle \
    wl-clipboard \
    at-spi2-core \
    python3-gobject \
    gobject-introspection-devel \
    cairo-gobject-devel \
    $(ydotool_build_deps dnf)
else
  echo "Unsupported package manager. Install manually:"
  echo "  spectacle, wl-clipboard, at-spi2-core, python3-gi, gir1.2-atspi-2.0"
  echo "  build-essential cmake libevdev-dev scdoc (for the ydotool source build)"
  exit 1
fi

echo ""
ydotool_install
ydotool_service

echo ""
echo "==> Verifying tools..."
MISSING=0
for cmd in spectacle wl-copy; do
  echo -n "  $cmd: "
  if command -v "$cmd" &>/dev/null; then echo "OK"; else echo "MISSING"; MISSING=1; fi
done

ydotool_check || MISSING=1

echo ""
echo "==> systemd tools (REQUIRED on KDE)..."
# Unlike the other desktops, KDE's window listing goes through a KWin script
# loaded over D-Bus. A KWin script cannot return a value, so it prints its
# answer and the backend reads it back out of the journal. The clipboard
# talks to Klipper over D-Bus for the same reason wl-paste can't be used
# (it hangs on Plasma 6). Both need systemd's tools.
for cmd in busctl journalctl; do
  echo -n "  $cmd: "
  if command -v "$cmd" &>/dev/null; then
    echo "OK"
  else
    echo "MISSING — without it list_windows/focus_window/list_screens and the"
    echo "    clipboard cannot work on KDE. A KDE without systemd is not supported."
    MISSING=1
  fi
done

echo ""
echo "==> KDE D-Bus services (only meaningful inside a Plasma session)..."
if command -v busctl &>/dev/null; then
  for svc in org.kde.KWin org.kde.klipper; do
    echo -n "  $svc: "
    if busctl --user --no-pager status "$svc" &>/dev/null; then
      echo "OK"
    else
      echo "not on the bus (fine over SSH; must be there in-session)"
    fi
  done
fi

echo ""
a11y_enable

echo ""
echo "==> Pointer acceleration..."
# Nothing to do here, unlike GNOME: KWin keeps acceleration per input device,
# and the input backend flattens it on ydotoold's virtual device when it
# connects (org.kde.KWin /org/kde/KWin/InputDevice/eventN). It touches only
# that device, so the physical mouse keeps its curve.
echo "  Handled at runtime: the input backend sets flat acceleration on the"
echo "  ydotoold virtual device when it connects. Nothing to configure."

echo ""
echo "  Screenshots: spectacle talks to org.kde.KWin.ScreenShot2 directly, so"
echo "               there is no portal prompt and no 'Share Screen?' dialog."
echo "               kwin does not implement wlr-screencopy, so grim won't work."
echo ""

if [ "$MISSING" -ne 0 ]; then
  echo "WARNING: Some tools are missing. The agent will have limited capabilities."
fi

venv_setup "$REPO_DIR"
echo ""
ydotool_client_env_hint
echo ""
echo "==> Done."
