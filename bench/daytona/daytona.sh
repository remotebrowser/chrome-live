#!/usr/bin/env bash
# Daytona implementation of the bench bp_* hook contract (see ../lib.sh).
#
#   MODE=resume COUNT=10 bash bench/daytona/daytona.sh   # stop -> start
#   MODE=cold   COUNT=10 bash bench/daytona/daytona.sh   # stop+archive -> start
#
# Hook mapping onto Daytona's lifecycle:
#   bp_make_paused -> stop only        (filesystem stays on local NVMe -> resume)
#   bp_make_cold   -> stop + archive   (filesystem to object storage -> cold)
#   bp_trigger     -> start            (TIMED)
#   bp_ready_cdp   -> curl <public preview URL>/json/version
#
# CDP (:9222) is in Daytona's previewable 3000-9999 range and is the only signal
# this bench measures. The noVNC preview URL is also printed (open it in a browser
# to see the Chrome session) but isn't used for any timing decision; noVNC lives
# on :80 internally, so bp_setup installs a socat :8080->:80 forwarder. Storage
# probe details (NVMe vs network) are saved to bench/daytona/storage-info.txt by
# daytona_ctl.py; this script doesn't dump them to stdout.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/../lib.sh"
PLATFORM=daytona
: "${READY_TIMEOUT:=120}"   # Daytona create + archive restore can run a bit longer

# --- config -----------------------------------------------------------------
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
PYBIN="${PYBIN:-$SCRIPT_DIR/.venv/bin/python}"
CTLPY="$SCRIPT_DIR/daytona_ctl.py"
# chrome-live amd64 manifest digest (Daytona rejects the :latest tag).
IMAGE="${IMAGE:-ghcr.io/remotebrowser/chrome-live@sha256:d977214aefd18cbf8071b196cd9c2dbb301f0fd50ee4200bdd47d68c2b6e423f}"
CPU="${CPU:-2}"; MEM="${MEM:-2}"; DISK="${DISK:-10}"
CURL_MAXTIME="${CURL_MAXTIME:-5}"

CDP_URL=""; NOVNC_URL=""; SANDBOX_ID=""

# --- coprocess plumbing ------------------------------------------------------
ctl_start() {
  [[ -f "$ENV_FILE" ]] || { echo "[setup] ENV_FILE not found: $ENV_FILE"; return 1; }
  set -a; . "$ENV_FILE"; set +a
  [ -n "${DAYTONA_API_KEY:-}" ] || { echo "[setup] DAYTONA_API_KEY not found in $ENV_FILE"; return 1; }
  coproc CTL { exec "$PYBIN" "$CTLPY"; }
  # Tell lib.sh which FDs background probe subshells must close. If a subshell
  # inherits these and holds them open, the parent's read from the coproc
  # deadlocks (observed: bench hangs after wait-cdp's reply was already
  # delivered, because the wait for ext/novnc subshells never returns).
  export BENCH_COPROC_FDS="${CTL[0]} ${CTL[1]}"
}

# ctl <command...> -> prints the single response line; returns nonzero on ERR.
# Hard wall-clock cap lives in lib.sh (BENCH_TIMEOUT watchdog) — that kills
# the whole bench if any hook (including SDK calls hidden behind this coproc)
# wedges, so we don't need per-call timeouts here.
: "${CTL_READ_TIMEOUT:=60}"
ctl() {
  printf '%s\n' "$*" >&"${CTL[1]}"
  local resp
  if ! IFS= read -r -t "$CTL_READ_TIMEOUT" resp <&"${CTL[0]}"; then
    # Read timed out (or pipe closed). Surface as ERR so callers don't deadlock
    # (e.g. teardown hanging on a slow daytona.delete). The BENCH_TIMEOUT
    # watchdog in lib.sh is still the outer backstop.
    printf 'ERR ctl read timeout %ss for: %s\n' "$CTL_READ_TIMEOUT" "$*"
    return 1
  fi
  printf '%s\n' "$resp"
  case "$resp" in OK*) return 0;; *) return 1;; esac
}

# --- hooks -------------------------------------------------------------------
bp_setup() {
  ctl_start || return 1

  local resp
  echo "[setup] creating sandbox ($CPU vCPU / $MEM GiB / $DISK GiB disk)"
  resp=$(ctl "create $IMAGE $CPU $MEM $DISK") || { echo "[setup] create failed: $resp"; return 1; }
  SANDBOX_ID="${resp#OK }"
  echo "[setup] sandbox ready: $SANDBOX_ID"

  resp=$(ctl "preview 9222") || { echo "[setup] preview 9222 failed: $resp"; return 1; }
  CDP_URL=$(printf '%s' "$resp" | awk '{print $2}')
  echo "[setup] CDP endpoint: $CDP_URL"

  # noVNC isn't a measurement signal, just a convenience URL for humans.
  resp=$(ctl "installfwd") || echo "[setup] WARN installfwd: $resp (noVNC URL may not load)"
  resp=$(ctl "preview 8080") || { echo "[setup] preview 8080 failed: $resp"; return 1; }
  NOVNC_URL=$(printf '%s' "$resp" | awk '{print $2}')
  echo "[setup] noVNC endpoint (informational): $NOVNC_URL"

  ctl "info $SCRIPT_DIR/storage-info.txt" >/dev/null \
    && echo "[setup] storage probe saved to bench/daytona/storage-info.txt" \
    || echo "[setup] storage probe failed (non-fatal)"
}

bp_make_cold()   { ctl stop >/dev/null && ctl archive >/dev/null; }
bp_make_paused() { ctl stop >/dev/null; }
bp_trigger()     { ctl start >/dev/null; }

bp_ready_cdp_external() {
  curl -fsS --max-time "$CURL_MAXTIME" -o /dev/null "$CDP_URL/json/version"
}

# Internal CDP via Daytona SDK process.exec inside the sandbox. This is the
# PRIMARY signal the harness reports for platform decisions: it bypasses the
# public preview proxy (which adds ~2-3s on Daytona) and measures actual
# in-container readiness plus one SDK round-trip.
bp_ready_cdp_internal() {
  # No $(...) capture: that subshell + the ext/novnc background subshells +
  # the coproc all together wedge bash's read on the response. ctl already
  # returns 0 iff the reply starts with "OK", which is all we need here.
  ctl wait-cdp >/dev/null
}

# Optional: harness polls this if defined and prints an informational
# "novnc ready in Xms" line per run. noVNC is NOT in the [result] summary.
bp_ready_novnc() {
  [ -n "$NOVNC_URL" ] || return 1
  curl -fsS --max-time "$CURL_MAXTIME" -o /dev/null "$NOVNC_URL"
}

bp_teardown() {
  [ -n "${SANDBOX_ID:-}" ] || return 0
  ctl delete >/dev/null 2>&1 || echo "[teardown] WARN delete failed (check Daytona console for $SANDBOX_ID)"
  ctl quit   >/dev/null 2>&1 || true
}

# --- run ---------------------------------------------------------------------
bench_main
