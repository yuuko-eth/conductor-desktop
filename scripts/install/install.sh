#!/usr/bin/env bash
# conductor-desktop installer for macOS, Linux and FreeBSD (Windows: install.ps1).
#
#   curl -fsSL https://conductor-mcp.io/install.sh | bash
#   fetch -qo - https://conductor-mcp.io/install.sh | bash      (FreeBSD, no curl in base)
#   curl -fsSL https://conductor-mcp.io/install.sh | bash -s -- --client claude --grounder managed --gateway <url> --token cgr_live_…
#
# Source-first: it fetches the source (a release tarball, or git), runs the
# machine's setup script (system deps, accessibility, ydotool on Wayland, and a
# .venv), checks capture and input when run inside the desktop, and registers
# the server with your MCP client. Rerunning it updates in place and keeps
# .venv. Every question has a flag, so it also runs unattended.
set -euo pipefail

# The variables were CONDUCTOR_MCP_* before the rename; carry any still set
# over to the new names (a set new name wins).
for _old in $(env | sed -n 's/^\(CONDUCTOR_MCP_[A-Z0-9_]*\)=.*/\1/p'); do
  _new="CONDUCTOR_DESKTOP_${_old#CONDUCTOR_MCP_}"
  [ -n "${!_new+x}" ] || export "$_new=${!_old}"
done

REPO="${CONDUCTOR_DESKTOP_REPO:-yuuko-eth/conductor-desktop}"
REF="${CONDUCTOR_DESKTOP_REF:-main}"
DEST="${CONDUCTOR_DESKTOP_HOME:-$HOME/.local/share/conductor-desktop}"
# Installs made before the rename live under the old folder; keep updating it.
if [ -z "${CONDUCTOR_DESKTOP_HOME:-}" ] && [ ! -e "$DEST" ] && [ -d "$HOME/.local/share/conductor-mcp" ]; then
  DEST="$HOME/.local/share/conductor-mcp"
fi
SERVER_NAME="conductor-desktop"

SOURCE=""        # --source: a checkout dir, a .tar.gz, or a URL to one
USE_GIT=0
DESKTOP=""
CLIENTS=""       # comma list: claude, claude-desktop, pi, none ("" = ask / detect)
GROUNDER=""      # none | managed | self-host ("" = ask, none when unattended)
GATEWAY="${CONDUCTOR_DESKTOP_GATEWAY_URL:-}"
API_KEY="${CONDUCTOR_DESKTOP_API_KEY:-}"
G_URL="${CONDUCTOR_DESKTOP_GROUNDER_URL:-}"
G_FAMILY="${CONDUCTOR_DESKTOP_GROUNDER_FAMILY:-}"
D_URL="${CONDUCTOR_DESKTOP_DESCRIBE_URL:-}"
D_FAMILY="${CONDUCTOR_DESKTOP_DESCRIBE_FAMILY:-}"
RUN_CHECK=1
YES=0

usage() {
  cat <<EOF
conductor-desktop installer

Where it goes
  --dir <path>             install folder (default $DEST)
  --ref <tag|branch>       version to fetch (default $REF)
  --git                    clone with git instead of downloading a tarball
  --source <dir|tgz|url>   install from this source instead of GitHub

Setup
  --desktop <name>         skip desktop detection: x11 | gnome | kde | wlroots
  --no-check               don't run conductor-desktop-check at the end

Grounder (optional; the agent works from screenshots without one)
  --grounder none          no model (default when unattended)
  --grounder managed --gateway <url> --token <cgr_live_…>
  --grounder self-host --url <url> [--family qwen|holo|…] [--describe-url <url>]

MCP client
  --client <list>          claude, claude-desktop, pi or none, comma-separated
                           (default: every one that's installed)
  -y, --yes                don't ask; take the defaults
  -h, --help               show this
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)           shift; DEST="${1:-}" ;;
    --dir=*)         DEST="${1#*=}" ;;
    --ref)           shift; REF="${1:-}" ;;
    --ref=*)         REF="${1#*=}" ;;
    --git)           USE_GIT=1 ;;
    --source)        shift; SOURCE="${1:-}" ;;
    --source=*)      SOURCE="${1#*=}" ;;
    --desktop)       shift; DESKTOP="${1:-}" ;;
    --desktop=*)     DESKTOP="${1#*=}" ;;
    --no-check)      RUN_CHECK=0 ;;
    --grounder)      shift; GROUNDER="${1:-}" ;;
    --grounder=*)    GROUNDER="${1#*=}" ;;
    --managed)       GROUNDER=managed ;;
    --self-host)     GROUNDER=self-host ;;
    --gateway)       shift; GATEWAY="${1:-}" ;;
    --gateway=*)     GATEWAY="${1#*=}" ;;
    --token)         shift; API_KEY="${1:-}" ;;
    --token=*)       API_KEY="${1#*=}" ;;
    --url)           shift; G_URL="${1:-}" ;;
    --url=*)         G_URL="${1#*=}" ;;
    --family)        shift; G_FAMILY="${1:-}" ;;
    --family=*)      G_FAMILY="${1#*=}" ;;
    --describe-url)  shift; D_URL="${1:-}" ;;
    --describe-url=*) D_URL="${1#*=}" ;;
    --client)        shift; CLIENTS="${1:-}" ;;
    --client=*)      CLIENTS="${1#*=}" ;;
    -y|--yes)        YES=1 ;;
    -h|--help)       usage; exit 0 ;;
    *) printf 'error: unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1m>> %s\033[0m\n' "$*"; }
warn() { printf '>> warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Can we prompt? `[ -r /dev/tty ]` is true even with no terminal attached
# (CI, a container, ssh without -t); opening it is the real test. Under
# `curl | bash` stdin is this script, so prompts read /dev/tty, never stdin.
have_tty() { [ "$YES" = 0 ] && ( exec 3</dev/tty ) 2>/dev/null; }
ask() { # $1 prompt  $2 default -> answer on stdout
  local a=""
  if have_tty; then printf '%s ' "$1" > /dev/tty; read -r a < /dev/tty || a=""; fi
  echo "${a:-$2}"
}

fetch() { # $1 url  $2 dest
  if   command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  elif command -v fetch >/dev/null 2>&1; then fetch -q -o "$2" "$1"   # FreeBSD base
  else die "need curl, wget or fetch"; fi
}

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux|FreeBSD) ;;
  *) die "unsupported OS: $OS (Windows: use install.ps1)" ;;
esac

# ── 1. uv ────────────────────────────────────────────────────────────────
# FreeBSD's setup script finds uv itself, or uses pip where uv won't run.
UV=""
if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"
elif [ "$OS" != FreeBSD ]; then
  say "installing uv (astral.sh)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || die "uv didn't install; see https://docs.astral.sh/uv/"
fi
if [ -n "$UV" ]; then PATH="$(dirname "$UV"):$PATH"; export PATH; fi

# ── 2. source ────────────────────────────────────────────────────────────
# Updating replaces everything but .venv, so a rerun keeps the environment.
replace_tree() { # $1 = new tree; moves it into $DEST, keeping $DEST/.venv
  mkdir -p "$DEST"
  find "$DEST" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  # A source folder may be someone's checkout: never copy its .venv or .git.
  (cd "$1" && tar cf - --exclude=./.venv --exclude=./.git .) | (cd "$DEST" && tar xf -)
}

say "conductor-desktop -> $DEST"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
if [ -n "$SOURCE" ]; then
  if [ -d "$SOURCE" ]; then
    replace_tree "$(cd "$SOURCE" && pwd)"
  else
    case "$SOURCE" in
      http://*|https://*) fetch "$SOURCE" "$TMP/src.tgz" ;;
      *) cp "$SOURCE" "$TMP/src.tgz" ;;
    esac
    mkdir "$TMP/src" && tar xzf "$TMP/src.tgz" -C "$TMP/src"
    # A GitHub tarball has one top folder; a plain archive of the tree doesn't.
    top="$TMP/src"; [ -f "$top/pyproject.toml" ] || top="$(find "$TMP/src" -mindepth 1 -maxdepth 1 -type d | head -1)"
    replace_tree "$top"
  fi
elif [ "$USE_GIT" = 1 ]; then
  command -v git >/dev/null 2>&1 || die "--git needs git"
  if [ -d "$DEST/.git" ]; then
    git -C "$DEST" fetch --quiet --depth 1 origin "$REF" && git -C "$DEST" checkout --quiet --force FETCH_HEAD
  else
    git clone --quiet --depth 1 --branch "$REF" "https://github.com/$REPO" "$TMP/src"
    replace_tree "$TMP/src"
  fi
else
  # refs/heads/ and refs/tags/ both resolve through codeload's short form.
  fetch "https://codeload.github.com/$REPO/tar.gz/$REF" "$TMP/src.tgz" \
    || die "couldn't download $REPO@$REF"
  mkdir "$TMP/src" && tar xzf "$TMP/src.tgz" -C "$TMP/src"
  replace_tree "$(find "$TMP/src" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi
[ -f "$DEST/pyproject.toml" ] || die "no pyproject.toml in $DEST; the source looks wrong"

# ── 3. system deps and .venv ─────────────────────────────────────────────
DETECTED="$(bash "$DEST/setup/setup.sh" --detect ${DESKTOP:+--desktop "$DESKTOP"})" \
  || die "couldn't tell which desktop this is; pass --desktop x11|gnome|kde|wlroots"
say "setup: ${DETECTED#==> }"
bash "$DEST/setup/setup.sh" ${DESKTOP:+--desktop "$DESKTOP"} || die "the setup script failed (see above)"
CMD="$DEST/.venv/bin/conductor-desktop"
[ -x "$CMD" ] || die "$CMD is missing; the setup script didn't create .venv"

# Wayland input goes through ydotoold's socket, which the server is told about
# through the client's env.
YSOCK=""
case "$DETECTED" in
  *gnome-wayland*|*-kde*|*wlroots*) YSOCK="/run/user/$(id -u)/.ydotool_socket" ;;
esac

# ── 4. check ─────────────────────────────────────────────────────────────
# Only meaningful inside the desktop session: over SSH there's no screen.
in_desktop() {
  [ "$OS" = Darwin ] && [ -z "${SSH_CONNECTION:-}" ] && return 0
  [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] && [ -z "${SSH_CONNECTION:-}" ]
}
if [ "$RUN_CHECK" = 1 ]; then
  if in_desktop; then
    say "checking capture and input"
    YDOTOOL_SOCKET="${YSOCK:-${YDOTOOL_SOCKET:-}}" "$DEST/.venv/bin/conductor-desktop-check" \
      || warn "the check failed; see above, and the platform guide"
  else
    say "not inside the desktop session, so skipping the check. Run it there:"
    printf '     %s\n' "$DEST/.venv/bin/conductor-desktop-check"
  fi
fi

# ── 5. grounder ──────────────────────────────────────────────────────────
if [ -z "$GROUNDER" ]; then
  if   [ -n "$API_KEY" ] || [ "$G_FAMILY" = managed ]; then GROUNDER=managed
  elif [ -n "$G_URL" ]; then GROUNDER=self-host
  elif have_tty; then
    printf '\nA grounder model lets the agent find things by description and read the\nscreen as text. Without one it works from screenshots.\n' > /dev/tty
    case "$(ask 'Grounder: [1] none (default)  [2] managed gateway  [3] my own model server:' 1)" in
      2|managed) GROUNDER=managed ;;
      3|self-host) GROUNDER=self-host ;;
      *) GROUNDER=none ;;
    esac
  else
    GROUNDER=none
  fi
fi

GENV=()   # KEY=VALUE pairs for the client's env
case "$GROUNDER" in
  none) ;;
  managed)
    [ -n "$GATEWAY" ] || GATEWAY="${G_URL:-$(ask 'Gateway URL:' '')}"
    [ -n "$GATEWAY" ] || die "managed needs --gateway <url>"
    [ -n "$API_KEY" ] || API_KEY="$(ask 'Seat token (cgr_live_…):' '')"
    [ -n "$API_KEY" ] || die "managed needs --token <cgr_live_…>"
    case "$API_KEY" in cgr_live_*) ;; *) warn "that doesn't look like a seat token (cgr_live_…)";; esac
    GENV+=("CONDUCTOR_DESKTOP_GROUNDER_URL=$GATEWAY" "CONDUCTOR_DESKTOP_GROUNDER_FAMILY=managed" "CONDUCTOR_DESKTOP_API_KEY=$API_KEY") ;;
  self-host)
    [ -n "$G_URL" ] || G_URL="$(ask 'Model server URL [http://127.0.0.1:8080]:' 'http://127.0.0.1:8080')"
    [ -n "$G_FAMILY" ] || G_FAMILY="$(ask 'Model family [qwen]:' qwen)"
    GENV+=("CONDUCTOR_DESKTOP_GROUNDER_URL=$G_URL")
    [ "$G_FAMILY" != qwen ] && GENV+=("CONDUCTOR_DESKTOP_GROUNDER_FAMILY=$G_FAMILY")
    # Point-only and grounding-only families need a describe backend.
    case "$G_FAMILY" in
      holo|locateanything|venus|guiowl|maiui|evocua|uitars)
        [ -n "$D_URL" ] || D_URL="$(ask "'$G_FAMILY' can't describe. Describe server URL (Qwen3-VL), or Enter to skip:" '')"
        if [ -n "$D_URL" ]; then
          GENV+=("CONDUCTOR_DESKTOP_DESCRIBE_URL=$D_URL" "CONDUCTOR_DESKTOP_DESCRIBE_FAMILY=${D_FAMILY:-qwen}")
        else
          warn "no describe backend: read_screen and crop won't work with '$G_FAMILY' alone"
        fi ;;
    esac ;;
  *) die "--grounder must be none, managed or self-host" ;;
esac
[ -n "$YSOCK" ] && GENV+=("YDOTOOL_SOCKET=$YSOCK")

# ── 6. MCP clients ───────────────────────────────────────────────────────
if [ "$OS" = Darwin ]; then
  CD_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
else
  CD_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/Claude/claude_desktop_config.json"
fi
PI_CFG="$HOME/.pi/agent/mcp.json"

if [ -z "$CLIENTS" ]; then
  found=()
  command -v claude >/dev/null 2>&1 && found+=(claude)
  { [ -d "$(dirname "$CD_CFG")" ] || [ -d /Applications/Claude.app ]; } && found+=(claude-desktop)
  { command -v pi >/dev/null 2>&1 || [ -d "$HOME/.pi/agent" ]; } && found+=(pi)
  CLIENTS="$(IFS=,; echo "${found[*]:-none}")"
  [ "$CLIENTS" != none ] && CLIENTS="$(ask "Register with [$CLIENTS]? (or a list, or none):" "$CLIENTS")"
fi

# Merge the server into a JSON config, keeping everything else in it. Values
# go through the environment, never into the program text.
merge_json() { # $1 config path
  mkdir -p "$(dirname "$1")"
  [ -f "$1" ] && cp "$1" "$1.bak.$(date +%s)"
  CFG="$1" SVR="$SERVER_NAME" CMD_PATH="$CMD" GENV="$(printf '%s\n' "${GENV[@]+"${GENV[@]}"}")" \
    "$DEST/.venv/bin/python" - <<'PY'
import json, os
p = os.environ["CFG"]
try:
    data = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    data = {}  # unreadable: the backup was taken above
env = dict(l.split("=", 1) for l in os.environ["GENV"].splitlines() if "=" in l)
entry = {"command": os.environ["CMD_PATH"], "args": []}
if env:
    entry["env"] = env
data.setdefault("mcpServers", {})[os.environ["SVR"]] = entry
with open(p, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
}

IFS=',' read -r -a CLIENT_LIST <<< "$CLIENTS"
for c in "${CLIENT_LIST[@]}"; do
  c="$(echo "$c" | tr -d ' ')"
  case "$c" in
    none|"") ;;
    claude)
      command -v claude >/dev/null 2>&1 || { warn "claude isn't installed; skipping Claude Code"; continue; }
      eargs=(); for kv in "${GENV[@]+"${GENV[@]}"}"; do eargs+=(-e "$kv"); done
      # --scope user: the default (local) scope only applies in this folder.
      # The name goes before -e: -e takes a list and would swallow it.
      claude mcp remove --scope user "$SERVER_NAME" >/dev/null 2>&1 || true
      claude mcp add --scope user "$SERVER_NAME" "${eargs[@]+"${eargs[@]}"}" -- "$CMD" >/dev/null
      say "Claude Code: added '$SERVER_NAME' (user scope). Restart it, or /mcp to connect." ;;
    claude-desktop)
      merge_json "$CD_CFG"
      say "Claude Desktop: added '$SERVER_NAME' to $CD_CFG. Quit and reopen it." ;;
    pi)
      merge_json "$PI_CFG"
      say "pi: added '$SERVER_NAME' to $PI_CFG" ;;
    *) warn "unknown client '$c' (claude, claude-desktop, pi, none)" ;;
  esac
done

echo
say "done. conductor-desktop is in $DEST"
if [ "$CLIENTS" = none ]; then
  echo "   To use it, point your MCP client at: $CMD"
  [ "${#GENV[@]}" -gt 0 ] && printf '   with env: %s\n' "${GENV[@]}"
fi
if [ "$OS" = Darwin ]; then
  echo "   macOS asks for Screen Recording and Accessibility the first time the"
  echo "   server runs: grant them, then quit and reopen the app that started it."
fi
echo "   Update: rerun this installer. Docs: https://github.com/$REPO"
