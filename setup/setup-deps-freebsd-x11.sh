#!/bin/sh
# Conductor — FreeBSD X11 deps installer.
#
# Run as a wheel-group user; uses sudo for pkg + sysrc.
# Tested on FreeBSD 15.0 (Xfce on Xorg, VirtualBox); should work on any
# FreeBSD 14+ X11 desktop. Wayland on FreeBSD isn't covered.
#
# Installs conductor's tools into the desktop you already run. On a bare
# system, CONDUCTOR_FREEBSD_DESKTOP=1 also installs Xorg + Xfce + LightDM.
set -eu

say()  { printf "\033[1;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "    \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "    \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "    \033[31mfail\033[0m  %s\n" "$*" >&2; exit 1; }


# ── 1. Sanity ──────────────────────────────────────────────────────
say "Verifying FreeBSD environment"
if [ "$(uname -s)" != "FreeBSD" ]; then
    die "This script must run on FreeBSD. uname -s = $(uname -s)"
fi
if ! command -v sudo >/dev/null 2>&1; then
    die "sudo missing. Run as root or 'pkg install sudo' first."
fi
ok "$(uname -srm)"


# ── 2. Bootstrap pkg ───────────────────────────────────────────────
# Fresh installs sometimes ship the boot-only `pkg` stub. First call
# triggers a self-update prompt that needs `-y` to be non-interactive.
say "Bootstrapping pkg"
sudo pkg bootstrap -y >/dev/null 2>&1 || true
# `pkg update` answers its own questions "no" under </dev/null; when a repo
# won't update (no network, or an OS-version mismatch after an upgrade), say
# what pkg said and carry on with the catalogue already on disk.
if UPDATE_OUT=$(sudo pkg update </dev/null 2>&1) && ! printf '%s' "$UPDATE_OUT" | grep -q "Error updating"; then
    ok "pkg ready"
else
    warn "pkg update didn't finish; installing from the local catalogue. pkg said:"
    printf '%s\n' "$UPDATE_OUT" | grep -E -i "error|unable|wrong|mismatch" | sed 's/^/          /'
fi


# ── 3. X11 + Xfce + display manager (only when asked) ──────────────
WITH_DESKTOP="${CONDUCTOR_FREEBSD_DESKTOP:-0}"
if [ "$WITH_DESKTOP" = 1 ]; then
    say "Installing X.org + Xfce + lightdm (CONDUCTOR_FREEBSD_DESKTOP=1)"
    PKGS_X11="xorg xfce xfce4-goodies lightdm lightdm-gtk-greeter dbus consolekit2"
    sudo pkg install -y $PKGS_X11 || die "X11 stack install failed"
    ok "X.org + Xfce + lightdm installed"
else
    say "Using the desktop already installed (CONDUCTOR_FREEBSD_DESKTOP=1 installs Xorg + Xfce)"
    sudo pkg install -y dbus >/dev/null || die "dbus install failed"
fi


# ── 4. Conductor's input/window/clipboard tools ────────────────────
# wmctrl proper isn't in FreeBSD's binary pkg repo; the community fork
# `wmctrl-fork` ships the same `wmctrl` binary with the same CLI flags
# (-l, -G, -p, -a). The linux_x11 backend doesn't care which one it
# finds on $PATH.
say "Installing Conductor's X11 control tools"
PKGS_TOOLS="xdotool xclip xwininfo"
sudo pkg install -y $PKGS_TOOLS || die "xdotool/xclip install failed"
ok "xdotool / xclip installed"

if command -v wmctrl >/dev/null 2>&1; then
    ok "wmctrl already installed"
elif sudo pkg install -y wmctrl-fork >/dev/null 2>&1; then
    ok "wmctrl-fork installed (provides wmctrl(1))"
elif sudo pkg install -y wmctrl >/dev/null 2>&1; then
    ok "wmctrl installed"
else
    warn "wmctrl not in pkg; list_windows falls back to xdotool."
    warn "To build it: cd /usr/ports/x11/wmctrl && sudo make install clean"
fi

# scrot — X11 screenshot fallback. mss (the Python lib our other
# platforms use) explicitly rejects FreeBSD with `System 'freebsd' not
# (yet?) implemented.` even though the underlying Xlib path works.
# _common.py's take_screenshot() chain ends with `scrot -z` so we keep
# it preinstalled.
sudo pkg install -y scrot || warn "scrot install failed (screenshots may fail)"
ok "scrot installed (X11 screenshot fallback)"


# ── 5. Python, PyGObject, AT-SPI and uv ──────────────────────────
# The venv must run the Python the pygobject package was built for, with
# system site-packages, so `import gi` works (the same trap as on Linux).
# So the Python follows an installed pygobject; only without one does the
# script install python3 and the pygobject that matches it. (Installing
# python3 first would pull the newest Python, which the existing pygobject
# wasn't built for.)
say "Python, PyGObject and AT-SPI"
PYVER=$(pkg query -g '%n' 'py3*-pygobject' 2>/dev/null | sed -n 's/^\(py3[0-9]*\)-pygobject$/\1/p' | sort | tail -1)
if [ -z "$PYVER" ]; then
    sudo pkg install -y python3 >/dev/null || die "python3 install failed"
    PYVER=$(python3 -c 'import sys; print("py%d%d" % sys.version_info[:2])')
fi
PYDOT=$(echo "$PYVER" | sed 's/^py3/3./')
# at-spi2-core brings the Atspi typelib the backend imports through gi.
sudo pkg install -y at-spi2-core "${PYVER}-pygobject" >/dev/null \
    || warn "${PYVER}-pygobject or at-spi2-core didn't install"
if "python${PYDOT}" -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi" 2>/dev/null; then
    ok "python${PYDOT} imports gi and Atspi"
else
    warn "python${PYDOT} can't import gi/Atspi; the ui_* tools won't work"
fi
# Pillow from pkg: PyPI has no FreeBSD wheels, so the venv borrows it.
sudo pkg install -y "${PYVER}-pillow" >/dev/null 2>&1 \
    || warn "${PYVER}-pillow not in pkg; uv will build Pillow from source"
# uv from pkg can be built for a newer release than this one (its libc
# symbols don't resolve), so test that it runs, not just that it's there.
UV_OK=0
if uv --version >/dev/null 2>&1; then
    UV_OK=1
elif ! command -v uv >/dev/null 2>&1 && sudo pkg install -y uv >/dev/null 2>&1; then
    if uv --version >/dev/null 2>&1; then
        UV_OK=1
    else
        warn "uv from pkg doesn't run on $(freebsd-version) (built for a newer release); removing it"
        sudo pkg delete -y uv >/dev/null 2>&1 || true
    fi
fi
if [ "$UV_OK" = 1 ]; then
    ok "$(uv --version)"
else
    warn "no working uv; the steps below use python -m venv and pip instead"
fi


# ── 7. Enable services ─────────────────────────────────────────────
say "Enabling services at boot (sysrc)"
sudo sysrc dbus_enable=YES         >/dev/null
ok "dbus will start at boot"
if [ "$WITH_DESKTOP" = 1 ]; then
    sudo sysrc lightdm_enable=YES  >/dev/null
    sudo sysrc moused_enable=YES   >/dev/null
    ok "lightdm + moused will start at boot"
fi

# Start dbus now so the next steps don't need a reboot to test
sudo service dbus start 2>/dev/null || true


# ── 8. Verify the binaries the executor will call ──────────────────
say "Verifying control tools"
MISSING=0
for cmd in xdotool wmctrl xclip xrandr; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd"
    else
        warn "$cmd MISSING — the agent will have limited capabilities"
        MISSING=1
    fi
done


# ── 9. The venv ───────────────────────────────────────────────────
# Python ${PYDOT} with system site-packages, so it sees pkg's pygobject and
# Pillow. uv when it runs here, python -m venv + pip otherwise.
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
VENV_OK=0
if [ "${CONDUCTOR_NO_VENV:-0}" = 1 ]; then
    say "Skipping the venv (CONDUCTOR_NO_VENV=1)"
else
    say "Python environment (.venv)"
    if [ -d "$REPO_DIR/.venv" ]; then
        ok ".venv exists; updating it"
    elif [ "$UV_OK" = 1 ]; then
        (cd "$REPO_DIR" && uv venv --quiet --python "python${PYDOT}" --system-site-packages) || warn "uv venv failed"
    else
        "python${PYDOT}" -m venv --system-site-packages "$REPO_DIR/.venv" || warn "python -m venv failed"
    fi
    if [ "$UV_OK" = 1 ]; then
        (cd "$REPO_DIR" && uv sync --quiet --inexact) || warn "uv sync failed"
    else
        "$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR" || warn "pip install failed"
    fi
    if "$REPO_DIR/.venv/bin/python" -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi; import conductor_desktop" 2>/dev/null; then
        ok ".venv imports conductor_desktop, gi and Atspi"; VENV_OK=1
    else
        warn ".venv can't import conductor_desktop or gi/Atspi"
    fi
fi


# ── 10. Final note ────────────────────────────────────────────────
echo ""
echo "=========================================================================="
if [ "$VENV_OK" = 1 ]; then
    echo "Done. Inside the desktop session: $REPO_DIR/.venv/bin/conductor-desktop-check"
else
    echo "FreeBSD X11 deps installed. From the repo root, inside the desktop session:"
    echo ""
    if [ "$UV_OK" = 1 ]; then
        echo "    uv venv --python python${PYDOT} --system-site-packages"
        echo "    uv sync"
    else
        echo "    python${PYDOT} -m venv --system-site-packages .venv"
        echo "    .venv/bin/pip install -e ."
    fi
    echo "    .venv/bin/conductor-desktop-check"
fi
echo ""
echo "The platform detector reports 'freebsd-x11' and uses the same X11 backend"
echo "as Linux. FreeBSD has no /proc, so change notices don't run."
if [ "$WITH_DESKTOP" = 1 ]; then
    echo ""
    echo "Reboot, or start the display manager now: sudo service lightdm start"
fi
echo "=========================================================================="

if [ "$MISSING" -ne 0 ]; then
    exit 1
fi
