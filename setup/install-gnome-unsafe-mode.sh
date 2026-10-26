#!/usr/bin/env bash
# Install the "Conductor Desktop unsafe_mode" GNOME Shell extension so
# global.context.unsafe_mode flips to true on every session start —
# letting conductor_desktop's GNOME backend call org.gnome.Shell.Eval
# without the operator hand-typing it in Looking Glass each login.
#
# WARNING: This lowers a GNOME security boundary. Only install on
# hosts dedicated to running conductor-desktop. Uninstall is just
# `gnome-extensions disable / uninstall`.
#
# Idempotent. Re-running picks up gnome-shell version bumps.

set -euo pipefail

UUID="unsafe-mode-on-login@conductor-desktop"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gnome-extension/$UUID"
DST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"

if ! command -v gnome-shell &>/dev/null; then
  echo "gnome-shell not on PATH — this script only makes sense on a GNOME host." >&2
  exit 1
fi

GNOME_MAJOR=$(gnome-shell --version | awk '{print $3}' | cut -d. -f1)
if ! [[ "$GNOME_MAJOR" =~ ^[0-9]+$ ]]; then
  echo "Could not parse GNOME Shell major version from \`gnome-shell --version\`" >&2
  exit 1
fi
echo "==> Detected GNOME Shell major version: $GNOME_MAJOR"

# Build the supported shell-version list dynamically. We claim
# compatibility with the detected major + the two adjacent versions
# above (so upgrades within the same API era keep working without
# re-installing). API eras: 40-44 (legacy), 45+ (ESM).
if [ "$GNOME_MAJOR" -lt 45 ]; then
  EXT_VARIANT="legacy"
  ENABLE_RANGE='["42","43","44"]'
else
  EXT_VARIANT="esm"
  # Open-ended: GNOME ships a new major every 6 months; bound to 50
  # for now and bump as we verify upstream API stability.
  ENABLE_RANGE='["45","46","47","48","49","50"]'
fi
echo "==> Using $EXT_VARIANT extension variant (shell-version=$ENABLE_RANGE)"

mkdir -p "$DST_DIR"
cp "$SRC_DIR/extension-$EXT_VARIANT.js" "$DST_DIR/extension.js"
sed "s|__SHELL_VERSIONS__|$ENABLE_RANGE|" \
  "$SRC_DIR/metadata.json.template" > "$DST_DIR/metadata.json"

echo "==> Installed to $DST_DIR"

# The extension was unsafe-mode-on-login@conductor-mcp before the rename;
# remove that copy so the two don't both load.
OLD_UUID="unsafe-mode-on-login@conductor-mcp"
OLD_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$OLD_UUID"
if [ -d "$OLD_DIR" ]; then
  gnome-extensions disable "$OLD_UUID" 2>/dev/null || true
  rm -rf "$OLD_DIR"
  echo "==> Removed the pre-rename $OLD_UUID"
fi
if command -v gsettings &>/dev/null; then
  ENABLED=$(gsettings get org.gnome.shell enabled-extensions)
  case "$ENABLED" in
    *"'$OLD_UUID'"*)
      ENABLED=$(printf '%s' "$ENABLED" | sed -e "s/'$OLD_UUID', //; s/, '$OLD_UUID'//; s/'$OLD_UUID'//")
      [ "$ENABLED" = "[]" ] && ENABLED="@as []"
      gsettings set org.gnome.shell enabled-extensions "$ENABLED" ;;
  esac
fi

# Enable. The extension won't load into the live gnome-shell process until
# the next shell restart (Wayland: logout+login; X11: Alt+F2 → r). And
# `gnome-extensions enable` asks that live shell, which doesn't know a
# just-installed extension yet: on GNOME 46 it exits 0 and saves nothing
# (on an Ubuntu 24.04 arm64 board). So write the enabled list directly as well;
# the shell reads it when it starts.
if command -v gnome-extensions &>/dev/null; then
  gnome-extensions enable "$UUID" 2>/dev/null || true
fi
if command -v gsettings &>/dev/null; then
  ENABLED=$(gsettings get org.gnome.shell enabled-extensions)
  case "$ENABLED" in
    *"'$UUID'"*) ;;
    "@as []") gsettings set org.gnome.shell enabled-extensions "['$UUID']" ;;
    *) gsettings set org.gnome.shell enabled-extensions "${ENABLED%]}, '$UUID']" ;;
  esac
fi

# Probe current state.
CURRENT=$(gnome-extensions info "$UUID" 2>/dev/null | awk '/^State:/ {print $2}' || echo unknown)
echo "==> Extension state: $CURRENT"

cat <<'EOF'

==> Next step:
    Wayland:  Log out and log back in. Extensions only load on shell
              start, and Wayland gnome-shell can't be reloaded in
              place. Verify with:
                  gdbus call --session --dest org.gnome.Shell \
                      --object-path /org/gnome/Shell \
                      --method org.gnome.Shell.Eval 'global.context.unsafe_mode'
              Expected: (true, 'true')

    X11:      Alt+F2 → type 'r' → Enter (gnome-shell --replace). Then
              the same gdbus probe above.

The flag now persists across reboots without manual Looking Glass —
the extension flips it on every gnome-shell start.
EOF
