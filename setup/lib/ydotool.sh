# shellcheck shell=bash
# ydotool 1.x for the Wayland setup scripts. Source it, then call
# ydotool_install and ydotool_service; on apt systems the caller installs
# the build deps (ydotool_build_deps names them) in its own package step.
#
# The input backend needs ydotool 1.x. Debian, Ubuntu and Raspberry Pi OS
# package 0.1.8, whose click bitmask and key syntax differ and which aborts on
# `mousemove --absolute`, so there it's built from source into /usr/local.
# Fedora and Arch package 1.0.4, so there the package is used. Neither version
# answers `--version` ("Unknown command"), so which one you have is told by
# where it lives and which package manager put it there.
#
# The daemon: an ydotoold already serving the socket (Fedora's and Arch's
# packages ship a user unit on the same path) is left alone. Otherwise a
# system service runs it as root, so it can open /dev/uinput, and hands the
# socket to the desktop user. A user unit would need the user's systemd
# manager to have started after they joined the input group.

# The desktop user, also when the script runs under sudo.
_ydotool_user() { echo "${SUDO_USER:-$USER}"; }

_ydotool_pm() {
  if command -v apt &>/dev/null; then echo apt
  elif command -v dnf &>/dev/null; then echo dnf
  elif command -v pacman &>/dev/null; then echo pacman
  fi
}

# Socket path the daemon serves and the server must be told about.
ydotool_socket_path() { echo "/run/user/$(id -u "$(_ydotool_user)")/.ydotool_socket"; }

# Packages to install alongside the caller's own: the source-build deps on
# apt, the packaged 1.x on dnf and pacman.
ydotool_build_deps() {
  case "$1" in
    apt)    echo "build-essential cmake libevdev-dev scdoc git" ;;
    dnf)    echo "ydotool" ;;
    pacman) echo "ydotool" ;;
  esac
}

# Make sure a 1.x ydotool is installed: the package on dnf/pacman (installed
# through ydotool_build_deps), a source build into /usr/local elsewhere.
ydotool_install() {
  case "$(_ydotool_pm)" in
    dnf|pacman)
      echo "  ydotool 1.x: from the distro package ($(command -v ydotool || echo missing))"
      return 0 ;;
  esac
  if [ -x /usr/local/bin/ydotool ] && [ -x /usr/local/bin/ydotoold ]; then
    echo "  ydotool 1.x: already built (/usr/local/bin)"
    return 0
  fi
  echo "==> Building ydotool 1.x from source (the distro package is 0.x)..."
  local src="${YDOTOOL_SRC_DIR:-$HOME/src/ydotool}"
  if [ -d "$src/.git" ]; then
    git -C "$src" fetch --quiet --depth 1 origin && git -C "$src" reset --hard --quiet FETCH_HEAD
  else
    git clone --quiet --depth 1 https://github.com/ReimuNotMoe/ydotool "$src"
  fi
  cmake -S "$src" -B "$src/build" -DCMAKE_INSTALL_PREFIX=/usr/local >/dev/null
  make -C "$src/build" -j"$(nproc)" >/dev/null
  sudo make -C "$src/build" install >/dev/null
  sudo ldconfig 2>/dev/null || true
  echo "  ydotool 1.x: installed to /usr/local/bin"
}

# /dev/uinput access, the uinput module at boot, and a daemon on the socket.
ydotool_service() {
  local user uid gid sock daemon
  user=$(_ydotool_user); uid=$(id -u "$user"); gid=$(id -g "$user")
  sock=$(ydotool_socket_path)
  if [ -x /usr/local/bin/ydotoold ]; then daemon=/usr/local/bin/ydotoold
  else daemon=$(command -v ydotoold || echo /usr/bin/ydotoold)
  fi

  echo "==> ydotoold..."
  # Another daemon (the distro's user unit, say) already serves the socket.
  if [ -S "$sock" ] && pgrep -x ydotoold >/dev/null \
     && ! systemctl is-active --quiet ydotoold.service 2>/dev/null; then
    echo "  ydotoold: already running on $sock ($(pgrep -a -x ydotoold | head -1 | cut -d' ' -f2)); leaving it"
    return 0
  fi

  echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
    | sudo tee /etc/udev/rules.d/60-uinput.rules >/dev/null
  # Some images (Raspberry Pi OS) don't load uinput until something asks.
  echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
  sudo modprobe uinput 2>/dev/null || true
  sudo udevadm control --reload-rules
  sudo udevadm trigger /dev/uinput 2>/dev/null || true

  if ! command -v systemctl &>/dev/null || [ ! -d /run/systemd/system ]; then
    echo "  No systemd: start the daemon yourself, as root, at boot:"
    echo "    $daemon --socket-path=$sock --socket-own=$uid:$gid --socket-perm=0660"
    return 0
  fi

  # The socket lives in /run/user/<uid>, which appears when the user logs
  # in; until then the daemon fails and restarts. StartLimitIntervalSec=0
  # keeps systemd from giving up on it during that wait.
  sudo tee /etc/systemd/system/ydotoold.service >/dev/null <<EOF
[Unit]
Description=ydotool daemon (1.x) for conductor-desktop
After=multi-user.target
StartLimitIntervalSec=0
[Service]
Type=simple
ExecStart=$daemon --socket-path=$sock --socket-own=$uid:$gid --socket-perm=0660
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable ydotoold.service >/dev/null 2>&1
  # restart, not start: picks up a changed unit on a rerun.
  sudo systemctl restart ydotoold.service
  sleep 1
  if [ -S "$sock" ]; then
    echo "  ydotoold: running ($daemon), socket $sock"
  else
    echo "  ydotoold: started; the socket appears once $user is logged in"
  fi
}

# The line the MCP client config needs; the server passes it to ydotool.
ydotool_client_env_hint() {
  echo "  Add to your MCP client's env, or every input call fails:"
  echo "    \"YDOTOOL_SOCKET\": \"$(ydotool_socket_path)\""
}

# Report which ydotool is on PATH; returns 1 when it's likely 0.x.
ydotool_check() {
  local p
  p=$(command -v ydotool 2>/dev/null || true)
  case "$p" in
    "") echo "  ydotool: MISSING"; return 1 ;;
    /usr/local/bin/ydotool) echo "  ydotool: OK (1.x source build, $p)" ;;
    *)
      case "$(_ydotool_pm)" in
        dnf|pacman) echo "  ydotool: OK (1.x package, $p)" ;;
        *) echo "  ydotool: SUSPECT, $p comes first on PATH (probably the distro's 0.x)."
           echo "    Remove the distro package or put /usr/local/bin first."; return 1 ;;
      esac ;;
  esac
}
