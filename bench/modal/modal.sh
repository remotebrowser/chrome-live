#!/usr/bin/env bash
# Modal platform hooks for the chrome-live startup bench.
# Starts the modal_bench.py daemon as a coprocess, implements the bp_* hooks
# by talking to it, then sources lib.sh and runs the timed loop.
#
# Usage:
#   MODE=cold   COUNT=10 bash bench/modal.sh
#   MODE=resume COUNT=10 bash bench/modal.sh
#
# Env:
#   ENV_FILE  path to a .env with MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
#   PYTHON    python interpreter (default: bench/.venv/bin/python if present, else python3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM=modal
export PLATFORM

# --- locate + load the Modal token from .env -------------------------------
_load_env() {
  local candidates=()
  [ -n "${ENV_FILE:-}" ] && candidates+=("$ENV_FILE")
  candidates+=("$SCRIPT_DIR/../.env")
  local common parent
  if common="$(git -C "$SCRIPT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
    parent="$(dirname "$common")"
    candidates+=("$parent/.env")
  fi
  for f in "${candidates[@]}"; do
    if [ -f "$f" ]; then
      set -a; . "$f"; set +a
      echo "[setup] loaded env from $f"
      return 0
    fi
  done
  echo "[setup] no .env found (looked in: ${candidates[*]})" >&2
  return 1
}

if [ -z "${MODAL_TOKEN_ID:-}" ] || [ -z "${MODAL_TOKEN_SECRET:-}" ]; then
  _load_env || { echo "MODAL_TOKEN_ID/SECRET not set and no .env; aborting" >&2; exit 1; }
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then PYTHON="$SCRIPT_DIR/.venv/bin/python"; else PYTHON="python3"; fi
fi

# --- daemon coprocess -------------------------------------------------------
# Reads one reply line per command from the daemon's stdout. stderr (the
# daemon's `# ...` logs) passes through to the terminal. Hard wall-clock cap
# lives in lib.sh (BENCH_TIMEOUT watchdog).
: "${CTL_READ_TIMEOUT:=60}"
_send() {
  printf '%s\n' "$1" >&"${MODAL[1]}"
  local reply
  if ! IFS= read -r -t "$CTL_READ_TIMEOUT" reply <&"${MODAL[0]}"; then
    # Same teardown-deadlock protection as in daytona.sh; outer BENCH_TIMEOUT
    # watchdog in lib.sh is the final backstop.
    printf 'ERR daemon read timeout %ss for: %s' "$CTL_READ_TIMEOUT" "$1"
    return 1
  fi
  printf '%s' "$reply"
}

CDP_URL=""
NOVNC_URL=""

_parse_trigger() {
  # input: "TRIGGER_OK <cdp> <novnc>"
  case "$1" in
    TRIGGER_OK\ *)
      CDP_URL="$(echo "$1" | awk '{print $2}')"
      NOVNC_URL="$(echo "$1" | awk '{print $3}')"
      ;;
    *)
      echo "trigger failed: $1" >&2
      return 1
      ;;
  esac
}

# --- bp_* hook contract -----------------------------------------------------
bp_setup() {
  echo "[setup] starting modal_bench.py coprocess"
  coproc MODAL { exec "$PYTHON" "$SCRIPT_DIR/modal_bench.py" serve; }
  # Tell lib.sh's _spawn_probe to close these FDs in probe subshells, otherwise
  # parent's coproc reads + bash's `wait` deadlock. See lib.sh for details.
  export BENCH_COPROC_FDS="${MODAL[0]} ${MODAL[1]}"
  local r
  r="$(_send setup)"
  case "$r" in
    SETUP_OK\ *) echo "[setup] $r" ;;
    *) echo "[setup] failed: $r" >&2; return 1 ;;
  esac
}

bp_make_cold() { :; }   # cold trigger creates fresh each time; nothing to prep

bp_make_paused() {
  local r
  r="$(_send snapshot)"
  case "$r" in
    SNAPSHOT_OK) return 0 ;;
    SNAPSHOT_UNSUPPORTED*) echo "[prep] snapshot unsupported: $r" >&2; return 1 ;;
    *) echo "[prep] snapshot error: $r" >&2; return 1 ;;
  esac
}

bp_trigger() {
  local r
  if [ "$MODE" = "resume" ]; then
    r="$(_send trigger-resume)"
  else
    r="$(_send trigger-cold)"
  fi
  _parse_trigger "$r"
}

bp_ready_cdp_external() {
  [ -n "$CDP_URL" ] || return 1
  # Chrome rejects /json unless Host is localhost/IP; TLS SNI still routes by URL host.
  curl -sf -o /dev/null -H "Host: localhost" "$CDP_URL/json/version"
}

# Internal: ask the daemon to run curl 127.0.0.1:9222 INSIDE the sandbox via
# Modal sb.exec(). Bypasses the public WAN tunnel; primary signal for the report.
bp_ready_cdp_internal() {
  local r
  r="$(_send wait-cdp-internal)"
  [ "$r" = "CDP_OK" ]
}

bp_ready_novnc() {
  [ -n "$NOVNC_URL" ] || return 1
  curl -sf -o /dev/null "$NOVNC_URL/"
}

bp_teardown() {
  [ -n "${MODAL+x}" ] || return 0
  _send teardown >/dev/null 2>&1 || true
  [ -n "${MODAL_PID:-}" ] && kill "$MODAL_PID" 2>/dev/null || true
}

# shellcheck source=bench/lib.sh
. "$SCRIPT_DIR/../lib.sh"

bench_main
