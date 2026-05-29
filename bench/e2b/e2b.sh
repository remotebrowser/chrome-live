#!/usr/bin/env bash
# E2B startup benchmark for chrome-live. Mirrors the bp_* hook contract in
# lib.sh (see fly.sh for the reference shape). E2B runs Firecracker microVMs
# from a template (a memory+filesystem snapshot), so:
#
#   MODE=cold    -> kill the sandbox, then time `Sandbox.create(template)` +
#                   launching the browser stack (s6 init), to CDP/noVNC ready.
#                   The template snapshot is captured BEFORE Chrome starts (init
#                   can't be a start command, see below), so cold pays the full
#                   ~VM-spawn + Chrome-boot cost, unlike a Fly suspend/resume.
#   MODE=resume  -> beta_pause a fully-booted sandbox (Chrome running), then time
#                   `Sandbox.connect(id)` (auto-resume), restoring running Chrome
#                   from memory. This is E2B's headline path; resume is ~1s claimed.
#
# Sandbox lifecycle is SDK-only, so the hooks shell out to e2b_driver.py, which
# persists the live sandbox id + its public noVNC URL to bench/.e2b_state.
#
# Two E2B caveats (see e2b_driver.py / README "E2B notes"):
#   - The image's s6 entrypoint needs PID 1 via `unshare`, which E2B's
#     start-command context forbids (no CAP_SYS_ADMIN). So init is launched
#     post-create via commands.run inside bp_trigger, not as a start command.
#   - Chrome's DevTools rejects E2B's proxied Host header, so CDP can't be
#     reached through the public proxy (500). CDP readiness is measured
#     intra-sandbox (driver `wait_cdp` -> curl 127.0.0.1:9222). noVNC :80 has
#     no such check and is polled via the public get_host(80) URL.
#
# Requires:
#   - python3 with the `e2b` SDK installed (`pip install e2b`)
#   - E2B_API_KEY exported
#   - E2B_TEMPLATE = id of a template built by bench/e2b_build.py from the
#     public chrome-live image (Build System 2.0, API-key only).
#
# Usage:
#   E2B_API_KEY=… E2B_TEMPLATE=… MODE=cold   COUNT=10 READY_TIMEOUT=180 bench/e2b.sh
#   E2B_API_KEY=… E2B_TEMPLATE=… MODE=resume COUNT=10 READY_TIMEOUT=180 bench/e2b.sh
set -euo pipefail
cd "$(dirname "$0")"
PLATFORM="e2b"
# Template build / first snapshot restore is slow; give readiness a long timeout.
: "${READY_TIMEOUT:=180}"
source ../lib.sh

# Auto-load bench/.env (one dir up from this script) if creds aren't already set.
if [[ -z "${E2B_API_KEY:-}" || -z "${E2B_TEMPLATE:-}" ]] && [[ -f ../.env ]]; then
  set -a; . ../.env; set +a
fi

: "${E2B_API_KEY:?export E2B_API_KEY (Hobby tier OK for a trial)}"
: "${E2B_TEMPLATE:?set E2B_TEMPLATE to a template built from the chrome-live Dockerfile}"

# The e2b SDK lives in the bench-local venv; fall back to python3 if absent.
PY="./.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
DRIVER=("$PY" ./e2b_driver.py)
STATE_FILE="./.e2b_state"

_novnc_url() {
  [[ -f "$STATE_FILE" ]] || return 1
  "$PY" -c "import json; print(json.load(open('$STATE_FILE')).get('novnc_url',''))"
}

bp_setup()       { "${DRIVER[@]}" setup; }
bp_make_cold()   { "${DRIVER[@]}" cold; }
bp_make_paused() { "${DRIVER[@]}" paused; }
bp_trigger()     { "${DRIVER[@]}" trigger; }
bp_teardown()    { "${DRIVER[@]}" teardown; }

# Internal CDP is measured intra-sandbox via the driver: one commands.run on
# `curl 127.0.0.1:9222/json/version` (so elapsed reflects true readiness + one
# SDK round trip). E2B has NO usable external CDP path: Chrome's DevTools
# endpoint rejects E2B's public proxy because the proxy can't rewrite the Host
# header. So bp_ready_cdp_external is intentionally undefined; the harness will
# report only the internal number for E2B.
bp_ready_cdp_internal() { "${DRIVER[@]}" wait_cdp; }

bp_ready_novnc() {
  local u; u="$(_novnc_url)" || return 1
  [[ -n "$u" ]] || return 1
  curl -fsS --max-time 3 "$u/" >/dev/null
}

bench_main
