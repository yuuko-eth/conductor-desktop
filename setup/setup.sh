#!/usr/bin/env bash
# Run the setup script for this machine: macOS, FreeBSD, or the Linux desktop
# you're logged in to (X11, GNOME Wayland, KDE Plasma Wayland, or a wlroots
# compositor). Each one installs system deps and leaves a ready .venv.
#
#   setup/setup.sh                 detect and run
#   setup/setup.sh --detect        print what it would run, and why
#   setup/setup.sh --desktop kde   skip detection: x11 | gnome | kde | wlroots
#
# Detection follows the server's own rules (conductor_desktop/core/
# platform_detect.py), reading XDG_SESSION_TYPE and XDG_CURRENT_DESKTOP. Over
# SSH those describe the SSH login, not the desktop, so then it asks logind for
# the user's graphical session, and failing that looks for a running
# compositor. Settings for the scripts themselves (CONDUCTOR_NO_VENV, ...) pass
# through the environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DESKTOP=""
DETECT_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --desktop)   shift; DESKTOP="${1:-}" ;;
    --desktop=*) DESKTOP="${1#*=}" ;;
    --detect)    DETECT_ONLY=1 ;;
    -h|--help)   sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# Desktop family from a session's type and desktop name, mirroring
# platform_detect: x11 wins; on Wayland the named desktop decides, and an
# unknown Wayland compositor gets the wlroots tools (grim, wlrctl).
family_of() { # $1 type  $2 desktop name(s)
  local type desk
  type=$(echo "$1" | tr '[:upper:]' '[:lower:]')
  desk=$(echo "$2" | tr '[:upper:]' '[:lower:]')
  case "$type" in
    x11) echo x11; return ;;
    wayland) ;;
    *) return 1 ;;
  esac
  case "$desk" in
    *gnome*)                     echo gnome ;;
    *kde*|*plasma*)              echo kde ;;
    *sway*|*hyprland*|*wayfire*|*labwc*|*river*|*cosmic*) echo wlroots ;;
    *unity*|*ubuntu*)            echo gnome ;;
    *)                           echo wlroots ;;
  esac
}

# Sets FAM and REASON; returns 1 when no desktop session can be found.
FAM=""; REASON=""
detect_linux() {
  local fam id sid type desk
  # SteamOS Desktop Mode is X11 even on Plasma 6.
  id=$(. /etc/os-release 2>/dev/null; echo "${ID:-}")
  if [ "$id" = steamos ]; then FAM=x11; REASON="SteamOS (Desktop Mode is X11)"; return; fi

  # 1. This shell is inside the desktop session.
  if fam=$(family_of "${XDG_SESSION_TYPE:-}" "${XDG_CURRENT_DESKTOP:-}"); then
    FAM=$fam; REASON="XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-}, XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}"
    return
  fi

  # 2. logind: this user's graphical session.
  if command -v loginctl >/dev/null 2>&1; then
    for sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$(id -un)" '$3 == u {print $1}'); do
      type=$(loginctl show-session "$sid" -p Type --value 2>/dev/null || true)
      desk=$(loginctl show-session "$sid" -p Desktop --value 2>/dev/null || true)
      if fam=$(family_of "$type" "$desk"); then
        FAM=$fam; REASON="logind session $sid: Type=$type, Desktop=${desk:-?}"
        return
      fi
    done
  fi

  # 3. A running compositor or X server owned by this user.
  local p
  for p in gnome-shell:gnome kwin_wayland:kde Hyprland:wlroots sway:wlroots labwc:wlroots \
           wayfire:wlroots river:wlroots cosmic-comp:wlroots kwin_x11:x11 xfwm4:x11 Xorg:x11; do
    if pgrep -u "$(id -u)" -x "${p%%:*}" >/dev/null 2>&1; then
      REASON="running ${p%%:*}"
      FAM=${p##*:}
      # gnome-shell is GNOME on Wayland unless an X server runs for it.
      if [ "$FAM" = gnome ] && pgrep -x Xorg >/dev/null 2>&1; then FAM=x11; fi
      return
    fi
  done
  return 1
}

normalize() { # accept the server's route names too (linux-kde, ...)
  case "$1" in
    x11|linux-x11) echo x11 ;;
    gnome|gnome-wayland|linux-gnome) echo gnome ;;
    kde|kde-wayland|plasma|linux-kde) echo kde ;;
    wlroots|hyprland|sway|labwc|linux-wlroots) echo wlroots ;;
    *) return 1 ;;
  esac
}

case "$(uname -s)" in
  Darwin)  SCRIPT=setup-deps-macos.sh;       REASON="macOS" ;;
  FreeBSD) SCRIPT=setup-deps-freebsd-x11.sh; REASON="FreeBSD (X11)" ;;
  Linux)
    if [ -n "$DESKTOP" ]; then
      FAM=$(normalize "$DESKTOP") || { echo "unknown --desktop '$DESKTOP': x11, gnome, kde or wlroots" >&2; exit 2; }
      REASON="--desktop $DESKTOP"
    elif ! detect_linux; then
      echo "Couldn't find your desktop session. Run this inside it, or pass" >&2
      echo "--desktop x11|gnome|kde|wlroots." >&2
      exit 1
    fi
    case "$FAM" in
      x11)     SCRIPT=setup-deps-x11.sh ;;
      gnome)   SCRIPT=setup-deps-gnome-wayland.sh ;;
      kde)     SCRIPT=setup-deps-kde.sh ;;
      wlroots) SCRIPT=setup-deps-wlroots.sh ;;
    esac ;;
  *) echo "No setup script for $(uname -s). Windows: setup\\setup-deps-windows.ps1" >&2; exit 1 ;;
esac

echo "==> $SCRIPT  ($REASON)"
[ "$DETECT_ONLY" = 1 ] && exit 0
# The FreeBSD script is POSIX sh; the rest are bash.
case "$SCRIPT" in
  *freebsd*) exec sh "$SCRIPT_DIR/$SCRIPT" ;;
  *)         exec bash "$SCRIPT_DIR/$SCRIPT" ;;
esac
