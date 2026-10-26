#!/usr/bin/env bash
# Prepare macOS for conductor-desktop: Xcode command-line tools (for git), the
# permission grants, and .venv (`uv sync`; pyproject's platform dependencies
# cover pyobjc, so no extra is needed). CONDUCTOR_NO_VENV=1 skips the venv.
set -euo pipefail

echo "==> Preparing macOS for conductor-desktop..."

# Xcode command line tools, for git. pyobjc installs from wheels.
if ! xcode-select -p &>/dev/null; then
  echo "  Installing the Xcode command line tools..."
  xcode-select --install || true
  # Read the answer from the terminal: under `curl … | bash`, stdin is
  # this script, and a bare `read` would swallow its next line.
  # -r is true even with no terminal attached; opening it is the test.
  if { : < /dev/tty; } 2>/dev/null; then
    echo "  Finish the installer dialog, then press Enter."
    read -r _ < /dev/tty
  else
    echo "  Finish the installer dialog, then run this script again."
    exit 0
  fi
fi


# ── Permission guidance ──

echo "================================================================"
echo "  REQUIRED: Screen Recording and Accessibility"
echo "================================================================"
echo ""
echo "  System Settings → Privacy & Security, under both"
echo "  Screen Recording and Accessibility."
echo ""
echo "  Grant the app that STARTS the server:"
echo "    - Claude Code, pi or another agent in a terminal: grant the"
echo "      terminal (Terminal, iTerm2, Ghostty, ...)."
echo "    - Claude Desktop: it doesn't pass its grants on, so macOS"
echo "      asks about the server's own binary. conductor-desktop-check"
echo "      prints that path; add it with + and cmd+shift+G."
echo ""
echo "  Without Screen Recording, screenshots show the desktop and menu"
echo "  bar but every window is black. Without Accessibility, clicks and"
echo "  keys silently do nothing."
echo ""
echo "  After granting, quit the app with cmd+Q and open it again:"
echo "  macOS only rereads a grant when the app starts."
echo ""
echo "================================================================"
echo ""
# The environment: pyobjc comes from pyproject's platform dependencies.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "${CONDUCTOR_NO_VENV:-0}" = 1 ]; then
  echo "==> Skipping the venv (CONDUCTOR_NO_VENV=1)"
elif command -v uv &>/dev/null || [ -x "$HOME/.local/bin/uv" ]; then
  echo "==> Python environment (.venv)..."
  (cd "$REPO_DIR" && "$(command -v uv || echo "$HOME/.local/bin/uv")" sync --quiet) && echo "  .venv: synced"
else
  echo "==> uv not found. Install it, then run 'uv sync' in $REPO_DIR:"
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
echo ""
echo "==> Done. Next: $REPO_DIR/.venv/bin/conductor-desktop-check"
