# shellcheck shell=bash
# Shared steps for the Linux setup scripts. Source it, then call
# a11y_enable and, last, venv_setup <repo root>.

# Turn the AT-SPI bridge on. Despite the GNOME schema name, at-spi's bus
# launcher reads this key on every desktop, and some ship it false (Ubuntu
# 24.04 GNOME, KDE neon 24.04). With it off no app registers: the bus answers,
# ui_tree finds a few helper processes with no children, and the ui_* tools
# report "no app or window matches" as though the target were wrong.
a11y_enable() {
  echo "==> Accessibility bus (needed by the ui_* tools and change notices)..."
  if command -v gsettings &>/dev/null; then
    local cur
    cur=$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null || echo unknown)
    if [ "$cur" != "true" ]; then
      if gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null; then
        echo "  toolkit-accessibility: set to true (was $cur)."
        echo "  Restart apps that were already open, so they register."
      else
        echo "  WARNING: couldn't set toolkit-accessibility (no session bus? run"
        echo "  this inside the desktop session)."
      fi
    else
      echo "  toolkit-accessibility: already true"
    fi
  else
    echo "  gsettings not found: install it (glib2.0-bin / glib2), or no app"
    echo "  registers with AT-SPI and every ui_* tool comes back empty."
  fi
  # What the bus itself says is what matters.
  if command -v gdbus &>/dev/null; then
    echo -n "  org.a11y.Status IsEnabled: "
    gdbus call --session -d org.a11y.Bus -o /org/a11y/bus \
      -m org.freedesktop.DBus.Properties.Get org.a11y.Status IsEnabled 2>/dev/null \
      || echo "unreadable (not in a desktop session?)"
  fi
}

# The Python version the system's PyGObject (_gi) was built for, e.g. 3.12;
# empty when there is no system gi.
gi_abi() {
  local so d
  for d in /usr/lib/python3/dist-packages/gi \
           /usr/lib/python3*/site-packages/gi \
           /usr/lib64/python3*/site-packages/gi \
           /usr/local/lib/python3*/site-packages/gi; do
    so=$(ls $d/_gi.cpython-*.so 2>/dev/null | head -1)
    [ -n "$so" ] && break
  done
  [ -n "$so" ] || return 0
  basename "$so" | sed -E 's/_gi\.cpython-([0-9])([0-9]+).*/\1.\2/'
}

_uv() {
  if command -v uv &>/dev/null; then uv "$@"
  elif [ -x "$HOME/.local/bin/uv" ]; then "$HOME/.local/bin/uv" "$@"
  else return 127
  fi
}

# Create and sync <repo>/.venv so it can import the system's gi.
#
# The venv has to run the Python the system _gi.so was built for, and see
# system site-packages; on any other version `import gi` fails with a
# misleading circular-import error and the ui_* tools, the change notices,
# the tray and (on GNOME) the whole backend go quietly missing. When the
# system gi is older than requires-python (Ubuntu 22.04: 3.10), build
# PyGObject into a 3.12 venv instead; those distros ship only
# girepository-1.0, so it stays <3.51, and later syncs need --inexact or
# uv removes it (the lock doesn't list it).
#
# An existing .venv is synced and checked, never deleted.
# CONDUCTOR_NO_VENV=1 skips this step.
venv_setup() {
  local repo="$1" abi py
  [ "${CONDUCTOR_NO_VENV:-0}" = 1 ] && { echo "==> Skipping the venv (CONDUCTOR_NO_VENV=1)"; return 0; }
  abi=$(gi_abi)
  echo ""
  echo "==> Python environment (.venv)..."
  if ! _uv --version &>/dev/null; then
    echo "  uv not found. Install it, then rerun this script:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    return 0
  fi
  if [ -z "$abi" ]; then
    echo "  WARNING: no system PyGObject found; the ui_* tools and change"
    echo "  notices won't work. Install python3-gi (or your distro's name)."
  else
    echo "  System python3-gi is built for Python $abi"
  fi

  if [ -d "$repo/.venv" ]; then
    echo "  .venv exists: syncing (--inexact keeps a PyGObject build)"
    (cd "$repo" && _uv sync --inexact --quiet)
  elif [ -n "$abi" ] && [ "${abi#3.}" -lt 11 ]; then
    echo "  Python $abi is older than conductor-desktop needs (3.11): building"
    echo "  PyGObject into a Python 3.12 venv instead."
    (cd "$repo" && _uv venv --quiet --python 3.12 && _uv sync --quiet \
      && _uv pip install --quiet --python .venv/bin/python 'PyGObject<3.51' pycairo)
  else
    py=${abi:+python$abi}
    (cd "$repo" && _uv venv --quiet --system-site-packages ${py:+--python "$py"} && _uv sync --quiet)
  fi

  if "$repo/.venv/bin/python" -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi" 2>/dev/null; then
    echo "  .venv imports gi and Atspi: OK"
  else
    echo "  WARNING: .venv can't import gi/Atspi. If .venv was made earlier with"
    echo "  another Python, move it aside and rerun this script."
  fi
  echo ""
  echo "  Next: $repo/.venv/bin/conductor-desktop-check  (inside the desktop session)"
}
