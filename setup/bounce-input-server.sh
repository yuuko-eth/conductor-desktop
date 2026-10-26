#!/bin/sh
# bounce-input-server.sh — restart Haiku's input_server.
#
# Why: input_server scans device add-ons (including conductor_input at
# ~/config/non-packaged/add-ons/input_server/devices/conductor_input) only
# at startup. If the add-on was replaced after boot, or input_server's
# add-on scan lost a race with /boot/home during cold boot, observation
# (screenshot, describe, locate) keeps working but click/type/key silently
# no-op: input_send returns rc=0 because write_port succeeds, but nothing
# is listening on the other end of the port.
#
# What it does:
#   1. Probe the conductor_input port via `input_send ping` and exit
#      early if it's already healthy.
#   2. Kill input_server. launch_daemon respawns it (input_server is a
#      managed system service); the new process rescans add-on dirs and
#      reloads conductor_input.
#   3. Re-probe every 500ms until rc=0 or the 5s budget runs out.
#
# Caveat: input_server owns the desktop's keyboard and mouse. Restarting
# it freezes input for ~1s. Run from SSH, or in a terminal you can keep
# focused while you wait.

set -u

INPUT_SEND="${HOME}/config/non-packaged/bin/input_send"
PROBE_BUDGET_MS=5000
PROBE_INTERVAL_MS=500

if [ ! -x "$INPUT_SEND" ]; then
    echo "bounce-input-server: $INPUT_SEND not found or not executable" >&2
    exit 1
fi

probe() {
    "$INPUT_SEND" ping >/dev/null 2>&1
}

if probe; then
    echo "bounce-input-server: conductor_input port already responsive — nothing to do"
    exit 0
fi

echo "bounce-input-server: conductor_input port not responding; restarting input_server..."

if ! killall input_server 2>/dev/null; then
    echo "bounce-input-server: warning — killall returned non-zero (input_server may not have been running)" >&2
fi

waited=0
while [ "$waited" -lt "$PROBE_BUDGET_MS" ]; do
    sleep_secs=$(awk "BEGIN {printf \"%.3f\", $PROBE_INTERVAL_MS / 1000}")
    sleep "$sleep_secs"
    waited=$((waited + PROBE_INTERVAL_MS))
    if probe; then
        echo "bounce-input-server: ok (recovered after ${waited}ms)"
        exit 0
    fi
done

echo "bounce-input-server: ping never recovered within ${PROBE_BUDGET_MS}ms" >&2
echo "  Check that ~/config/non-packaged/add-ons/input_server/devices/conductor_input exists" >&2
echo "  and that launch_daemon respawned input_server (\`listteam | grep input_server\`)." >&2
exit 1
