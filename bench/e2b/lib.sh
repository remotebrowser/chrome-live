#!/usr/bin/env bash
# Shared, platform-agnostic timing harness for chrome-live startup benchmarks.
#
# A platform script sources this file, defines the bp_* hooks below, then calls
# bench_main. The harness times, per run, the wall-clock from the trigger action
# until CDP is ready and until the first noVNC frame is served, repeats COUNT
# times, and prints p50/p95 for both signals.
#
# Hooks a platform script must define (each returns 0 on success):
#   bp_setup        one-time provisioning (build/push image, create instance)
#   bp_make_cold    put target into a stopped/destroyed state (pre-cold-boot)
#   bp_make_paused  put target into a paused/suspended state (pre-resume); may be
#                   a no-op + `return 1` if the platform has no snapshot/resume
#   bp_trigger      the TIMED action: start or resume the instance
#   bp_ready_cdp    return 0 once CDP responds (the primary signal)
#   bp_ready_novnc  return 0 once noVNC serves a frame (user-perceived signal)
#   bp_teardown     destroy everything created by bp_setup
#
# Env knobs:
#   MODE=cold|resume   which start path to measure (default cold)
#   COUNT=N            number of timed runs (default 10)
#   READY_TIMEOUT=S    per-run timeout in seconds waiting for ready (default 90)
#   POLL_INTERVAL=S    seconds between readiness polls (default 0.2)
set -euo pipefail

now_ms() {
  # Bash 5 exposes microsecond wall-clock without a subprocess. EPOCHREALTIME is
  # "<seconds>.<6-digit-microseconds>" (separator is locale-dependent), so drop
  # the separator to get integer microseconds and divide. Integer math only:
  # routing through awk/printf floats loses precision to %.6g (~10^6 ms).
  if [[ -n "${EPOCHREALTIME:-}" ]]; then
    local us="${EPOCHREALTIME/[.,]/}"
    echo $(( us / 1000 ))
  else
    perl -MTime::HiRes=time -e 'printf "%.0f", time*1000'
  fi
}

# percentile <pct> <sorted-space-separated-values>
percentile() {
  local pct="$1"; shift
  local -a v=("$@")
  local n=${#v[@]}
  [[ $n -eq 0 ]] && { echo "n/a"; return; }
  # nearest-rank
  local rank=$(awk "BEGIN{r=int(($pct/100.0)*$n + 0.999999); if(r<1)r=1; if(r>$n)r=$n; print r}")
  echo "${v[$((rank-1))]}"
}

_wait_ready() {
  # _wait_ready <check-fn> -> echoes elapsed ms since $1 start_ms, or "TIMEOUT"
  local check="$1" start_ms="$2"
  local deadline=$(( start_ms + READY_TIMEOUT * 1000 ))
  while :; do
    if "$check" >/dev/null 2>&1; then
      echo $(( $(now_ms) - start_ms ))
      return 0
    fi
    [[ $(now_ms) -ge $deadline ]] && { echo "TIMEOUT"; return 1; }
    sleep "$POLL_INTERVAL"
  done
}

bench_main() {
  : "${MODE:=cold}"
  : "${COUNT:=10}"
  : "${READY_TIMEOUT:=90}"
  : "${POLL_INTERVAL:=0.2}"
  local platform="${PLATFORM:-unknown}"

  echo "=== chrome-live startup benchmark: platform=$platform mode=$MODE count=$COUNT ==="
  echo "-- setup --"
  bp_setup

  local -a cdp_ms=() novnc_ms=()
  local i prep start cdp novnc
  for (( i=1; i<=COUNT; i++ )); do
    echo "-- run $i/$COUNT ($MODE) --"
    if [[ "$MODE" == "resume" ]]; then
      bp_make_paused || { echo "ERROR: platform has no resume path; use MODE=cold"; exit 2; }
    else
      bp_make_cold
    fi

    start=$(now_ms)
    bp_trigger
    cdp=$(_wait_ready bp_ready_cdp "$start") || true
    novnc=$(_wait_ready bp_ready_novnc "$start") || true
    echo "   cdp=${cdp}ms novnc=${novnc}ms"
    [[ "$cdp" != "TIMEOUT" ]] && cdp_ms+=("$cdp")
    [[ "$novnc" != "TIMEOUT" ]] && novnc_ms+=("$novnc")
  done

  echo "-- teardown --"
  bp_teardown || true

  _report "CDP ready" "${cdp_ms[@]}"
  _report "noVNC frame" "${novnc_ms[@]}"
}

_report() {
  local label="$1"; shift
  local -a v
  IFS=$'\n' v=($(printf '%s\n' "$@" | sort -n)); unset IFS
  local n=${#v[@]}
  if [[ $n -eq 0 ]]; then
    echo "RESULT $label: no successful runs (all timed out)"
    return
  fi
  echo "RESULT $label (n=$n, mode=$MODE, platform=${PLATFORM:-unknown}): p50=$(percentile 50 "${v[@]}")ms p95=$(percentile 95 "${v[@]}")ms min=${v[0]}ms max=${v[$((n-1))]}ms"
}
