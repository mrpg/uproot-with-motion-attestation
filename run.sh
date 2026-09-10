#!/usr/bin/env bash
# SPDX-License-Identifier: 0BSD

set -euo pipefail

export MOTION_ATTESTATION_PORT="${MOTION_ATTESTATION_PORT:-35001}"
export MOTION_ATTESTATION_URL="http://127.0.0.1:${MOTION_ATTESTATION_PORT}"
export MOTION_ATTESTATION_PROXY_KEY
MOTION_ATTESTATION_PROXY_KEY="$(
    node -e "process.stdout.write(require('node:crypto').randomBytes(32).toString('hex'))"
)"

npm run prepare:motion-attestation --silent

node motion-attestation/server.mjs &
motion_pid=$!
uproot_pid=""

shutdown() {
    trap - EXIT INT TERM

    if [[ -n "$uproot_pid" ]]; then
        kill "$uproot_pid" 2>/dev/null || true
        wait "$uproot_pid" 2>/dev/null || true
    fi

    kill "$motion_pid" 2>/dev/null || true
    wait "$motion_pid" 2>/dev/null || true
}

trap shutdown EXIT INT TERM

node motion-attestation/wait.mjs

uv run uproot run "$@" &
uproot_pid=$!

set +e
wait -n "$motion_pid" "$uproot_pid"
status=$?
set -e

if kill -0 "$uproot_pid" 2>/dev/null; then
    echo "motion-attestation stopped unexpectedly" >&2
    status=1
fi

exit "$status"
