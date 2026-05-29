# shellcheck shell=bash
# Shared benchmark harness for chrome-live startup latency.
# Platform scripts (e.g. modal.sh) source this, implement the bp_* hooks below,
# then call bench_main. All wall-clock timing lives here.
#
# bp_* hook contract (platform script must define these):
#   bp_setup        create first instance + wait first boot (warm caches)
#   bp_make_cold    prepare a cold trigger (terminate prior instance)
#   bp_make_paused  prepare a paused/snapshot trigger; return 1 if unsupported
#   bp_trigger      TIMED: create/restore the instance
#   bp_ready_cdp    return 0 once CDP /json/version answers
#   bp_ready_novnc  return 0 once noVNC (:80) returns HTTP 200
#   bp_teardown     terminate the instance
#
# Env knobs:
#   MODE            cold | resume          (default cold)
#   COUNT           iterations             (default 10)
#   READY_TIMEOUT   seconds per signal     (default 120)
#   POLL_INTERVAL   seconds between polls  (default 0.2)
#   PLATFORM        label for output       (default unknown)

set -euo pipefail

MODE="${MODE:-cold}"
COUNT="${COUNT:-10}"
READY_TIMEOUT="${READY_TIMEOUT:-120}"
POLL_INTERVAL="${POLL_INTERVAL:-0.2}"
PLATFORM="${PLATFORM:-unknown}"

# Milliseconds since epoch. Prefer bash's $EPOCHREALTIME, fall back to perl.
now_ms() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    # EPOCHREALTIME is like 1700000000.123456; strip to ms.
    local t="${EPOCHREALTIME/[.,]/}"   # drop decimal separator -> microseconds
    echo "$(( ${t} / 1000 ))"
  else
    perl -MTime::HiRes=time -e 'printf "%d\n", time()*1000'
  fi
}

# Nearest-rank percentile. Args: <p> <sorted-ascending values...>
# Reads values as remaining args; caller must pass them sorted.
percentile() {
  local p="$1"; shift
  local n=$#
  [ "$n" -eq 0 ] && { echo 0; return; }
  # rank = ceil(p/100 * n), 1-indexed
  local rank=$(( (p * n + 99) / 100 ))
  [ "$rank" -lt 1 ] && rank=1
  [ "$rank" -gt "$n" ] && rank="$n"
  eval "echo \${$rank}"
}

# min/max of args
_min() { local m="$1"; shift; for v in "$@"; do [ "$v" -lt "$m" ] && m="$v"; done; echo "$m"; }
_max() { local m="$1"; shift; for v in "$@"; do [ "$v" -gt "$m" ] && m="$v"; done; echo "$m"; }

# Poll a readiness command until it succeeds or READY_TIMEOUT elapses.
# Args: <command...>; returns 0 on ready, 1 on timeout.
_wait_ready() {
  local deadline_ms=$(( $(now_ms) + READY_TIMEOUT * 1000 ))
  while [ "$(now_ms)" -lt "$deadline_ms" ]; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done
  return 1
}

_report_signal() {
  local label="$1"; shift
  local vals
  IFS=$'\n' vals=($(printf '%s\n' "$@" | sort -n)); unset IFS
  local n=${#vals[@]}
  if [ "$n" -eq 0 ]; then
    printf '  %-12s no successful samples\n' "$label"
    return
  fi
  local p50 p95 mn mx
  p50=$(percentile 50 "${vals[@]}")
  p95=$(percentile 95 "${vals[@]}")
  mn=$(_min "${vals[@]}")
  mx=$(_max "${vals[@]}")
  printf '  %-12s n=%-3d p50=%5dms  p95=%5dms  min=%5dms  max=%5dms\n' \
    "$label" "$n" "$p50" "$p95" "$mn" "$mx"
}

bench_main() {
  echo "=== chrome-live startup bench | platform=$PLATFORM mode=$MODE count=$COUNT ==="
  bp_setup

  if [ "$MODE" = "resume" ]; then
    if ! bp_make_paused; then
      echo "MODE=resume unsupported on this platform (bp_make_paused returned non-zero). Aborting."
      bp_teardown || true
      return 2
    fi
  fi

  local cdp_samples=() novnc_samples=()
  local i start end_cdp end_novnc
  for (( i=1; i<=COUNT; i++ )); do
    if [ "$MODE" = "resume" ]; then
      bp_make_paused >/dev/null || true   # snapshot already exists; no-op refresh
    else
      bp_make_cold
    fi

    start=$(now_ms)
    bp_trigger

    if _wait_ready bp_ready_cdp; then
      end_cdp=$(now_ms)
      cdp_samples+=( $(( end_cdp - start )) )
    else
      echo "  [run $i] CDP not ready within ${READY_TIMEOUT}s"
    fi

    if _wait_ready bp_ready_novnc; then
      end_novnc=$(now_ms)
      novnc_samples+=( $(( end_novnc - start )) )
    else
      echo "  [run $i] noVNC not ready within ${READY_TIMEOUT}s"
    fi

    printf '  [run %2d/%d] cdp=%sms novnc=%sms\n' "$i" "$COUNT" \
      "${cdp_samples[-1]:-NA}" "${novnc_samples[-1]:-NA}"

    bp_teardown
  done

  echo "=== results | platform=$PLATFORM mode=$MODE ==="
  _report_signal "CDP" "${cdp_samples[@]}"
  _report_signal "noVNC" "${novnc_samples[@]}"
}
