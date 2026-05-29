#!/usr/bin/env bash
# Platform-agnostic startup-latency harness.
# Platform hooks (bp_*) are defined in bench/<PLATFORM>.sh which sources this file.
#
# Contract the platform file must implement:
#   bp_setup       one-time provision + wait for first full boot (UNTIMED; image pull happens here)
#   bp_make_cold   put machine in stopped state (full stop)
#   bp_make_paused put machine in suspended state; return 1 if unsupported
#   bp_trigger     TIMED start/resume trigger (just kick it; do not wait)
#   bp_ready_cdp   probe CDP /json/version; return 0 when ready
#   bp_ready_novnc probe noVNC :80; return 0 when ready
#   bp_teardown    destroy everything
#
# Env: MODE=cold|resume  COUNT=10  READY_TIMEOUT=90  POLL_INTERVAL=0.2  PLATFORM=fly

set -uo pipefail

MODE="${MODE:-cold}"
COUNT="${COUNT:-10}"
READY_TIMEOUT="${READY_TIMEOUT:-90}"
POLL_INTERVAL="${POLL_INTERVAL:-0.2}"
PLATFORM="${PLATFORM:-fly}"

now_ms() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    # EPOCHREALTIME is "seconds.microseconds"; strip the dot, keep ms precision.
    local s="${EPOCHREALTIME%.*}" us="${EPOCHREALTIME#*.}"
    printf '%d\n' "$(( s * 1000 + 10#${us:0:3} ))"
  else
    perl -MTime::HiRes -e 'printf("%d\n", Time::HiRes::time()*1000)'
  fi
}

# percentile <pXX> <sorted-ascending values...>  -- nearest-rank
percentile() {
  local p="$1"; shift
  local n=$#
  [ "$n" -eq 0 ] && { echo 0; return; }
  # nearest-rank: rank = ceil(p/100 * n), 1-based
  local rank=$(( (p * n + 99) / 100 ))
  [ "$rank" -lt 1 ] && rank=1
  [ "$rank" -gt "$n" ] && rank="$n"
  eval "echo \${$rank}"
}

# _wait_ready <check_fn>  -- poll until success or timeout; echo elapsed ms (from the
# call's own start) on success, "TIMEOUT" on failure. Caller measures absolute latency
# from bp_trigger separately; this returns its own elapsed for convenience.
_wait_ready() {
  local check_fn="$1"
  local start deadline_ms elapsed
  start="$(now_ms)"
  deadline_ms=$(( start + READY_TIMEOUT * 1000 ))
  while :; do
    if "$check_fn" >/dev/null 2>&1; then
      elapsed=$(( $(now_ms) - start ))
      echo "$elapsed"
      return 0
    fi
    if [ "$(now_ms)" -ge "$deadline_ms" ]; then
      echo "TIMEOUT"
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

_summ() {
  # _summ <label> <values...>
  local label="$1"; shift
  local vals; vals="$(printf '%s\n' "$@" | sort -n)"
  # shellcheck disable=SC2206
  local arr=($vals)
  local n=${#arr[@]}
  if [ "$n" -eq 0 ]; then
    printf '%-22s no samples\n' "$label"
    return
  fi
  local p50 p95 mn mx
  p50="$(percentile 50 "${arr[@]}")"
  p95="$(percentile 95 "${arr[@]}")"
  mn="${arr[0]}"
  mx="${arr[$((n-1))]}"
  printf '%-22s n=%-3d p50=%-7s p95=%-7s min=%-7s max=%-7s (ms)\n' \
    "$label" "$n" "$p50" "$p95" "$mn" "$mx"
}

bench_main() {
  command -v "bp_setup" >/dev/null || { echo "platform hooks not loaded"; exit 1; }

  echo "== bench: PLATFORM=$PLATFORM MODE=$MODE COUNT=$COUNT READY_TIMEOUT=${READY_TIMEOUT}s =="

  local cdp_samples=() novnc_samples=() fails=0

  for i in $(seq 1 "$COUNT"); do
    # Put machine into the pre-trigger state.
    if [ "$MODE" = "resume" ]; then
      if ! bp_make_paused; then
        echo "resume mode unsupported on $PLATFORM (bp_make_paused returned non-zero); aborting"
        exit 2
      fi
    else
      bp_make_cold
    fi

    local t0 cdp_abs novnc_abs cdp_ready
    t0="$(now_ms)"
    bp_trigger

    # CDP (primary) — absolute latency from trigger.
    if _wait_ready bp_ready_cdp >/dev/null; then
      cdp_abs=$(( $(now_ms) - t0 ))
      cdp_samples+=("$cdp_abs")
      cdp_ready=1
    else
      echo "  run $i: CDP TIMEOUT"
      fails=$((fails+1))
      cdp_ready=0
    fi

    # noVNC (user-perceived) — absolute latency from same trigger.
    if _wait_ready bp_ready_novnc >/dev/null; then
      novnc_abs=$(( $(now_ms) - t0 ))
      novnc_samples+=("$novnc_abs")
    else
      echo "  run $i: noVNC TIMEOUT"
    fi

    if [ "$cdp_ready" = "1" ]; then
      printf '  run %-2d  cdp=%-6s ms  novnc=%-6s ms\n' "$i" "${cdp_abs:-NA}" "${novnc_abs:-NA}"
    fi
  done

  echo
  echo "== results: $PLATFORM / $MODE =="
  _summ "cdp ($MODE)" "${cdp_samples[@]}"
  _summ "novnc ($MODE)" "${novnc_samples[@]}"
  echo "failures: $fails"

  # Machine-readable line for aggregation.
  echo "RESULT $PLATFORM $MODE cdp_n=${#cdp_samples[@]} novnc_n=${#novnc_samples[@]} fails=$fails"
}
