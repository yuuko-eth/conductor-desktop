#!/usr/bin/env bash
# autopilot — thin shim around `pi` (the Pi coding agent) that:
#   1. Loads NVM so the Node-installed `pi` binary is on PATH in non-interactive shells.
#   2. Rewrites `-p "<content>"` / `--prompt "<content>"` into `-p "$AUTOPILOT_PREFIX <content>"`
#      so the prompt is dispatched as a Conductor harness slash-command (default `/task`).
#      Override with e.g. AUTOPILOT_PREFIX="/task Observe with describe before acting."
#   3. Forwards every other argument verbatim to `pi`.
#
# Install: copy to ~/.local/bin/autopilot on a host that has Pi + Conductor Desktop set up,
# then `chmod +x ~/.local/bin/autopilot`.

set -e

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"

# Hosts without NVM (Haiku, FreeBSD) keep `pi` in npm's global prefix bin dir,
# which non-interactive ssh shells don't pick up from .profile/.bashrc.
[ -d "$HOME/.npm-global/bin" ] && export PATH="$HOME/.npm-global/bin:$PATH"

PREFIX="${AUTOPILOT_PREFIX:-/task}"

args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--prompt)
            if [[ $# -lt 2 ]]; then
                echo "autopilot: $1 requires a value" >&2
                exit 2
            fi
            args+=("$1" "$PREFIX $2")
            shift 2
            ;;
        -p=*|--prompt=*)
            flag="${1%%=*}"
            val="${1#*=}"
            args+=("${flag}=${PREFIX} ${val}")
            shift
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

if ! command -v pi >/dev/null 2>&1; then
    echo "autopilot: 'pi' not found on PATH after sourcing NVM ($NVM_DIR)." >&2
    echo "  Install Pi (npm i -g @earendil-works/pi-coding-agent) and retry." >&2
    exit 127
fi

exec pi "${args[@]}"
