# bench/lib.sh -- generic startup-latency harness shared across all platform benches.
#
# A platform script (e.g. daytona.sh) sources this file and implements the bp_* hooks
# below, then calls bench_main. The harness times wall-clock from bp_trigger to two
# readiness signals (CDP = primary, noVNC = secondary) over COUNT runs and prints
# p50/p95/min/max per signal.
#
# HOOK CONTRACT (implement these in the platform script):
#   bp_setup        create the instance + wait for first boot; idempotent-ish.
#   bp_make_cold    put instance into a cold state (e.g. stop + archive). Untimed.
#   bp_make_paused  put instance into a paused/suspended state. Return 1 if the
#                   platform cannot pause (harness then aborts MODE=resume). Untimed.
#   bp_trigger      start/resume the instance. TIMED: clock starts just before this.
#   bp_ready_cdp    return 0 once CDP /json/version answers 200. Polled.
#   bp_ready_novnc  return 0 once noVNC answers HTTP 200. Polled.
#   bp_teardown     delete the instance. Called once on exit.
#
# ENV: MODE={cold|resume} COUNT=10 READY_TIMEOUT=120 POLL_INTERVAL=0.2 PLATFORM=<name>

set -u

: "${MODE:=resume}"
: "${COUNT:=10}"
: "${READY_TIMEOUT:=120}"
: "${POLL_INTERVAL:=0.2}"
: "${PLATFORM:=unknown}"

# Milliseconds since epoch. Prefers bash EPOCHREALTIME, falls back to perl Time::HiRes.
now_ms() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    # "<seconds>.<6-digit-microseconds>"; strip the separator to get integer
    # microseconds and divide. Integer math only: awk's %.6g truncates the
    # ~13-digit ms value to 6 sig-figs, making 1s-apart timestamps subtract to 0.
    local us="${EPOCHREALTIME/[.,]/}"
    echo $(( us / 1000 ))
  else
    perl -MTime::HiRes=time -e 'printf "%.0f\n", time()*1000'
  fi
}

# percentile P v1 v2 ... -> nearest-rank percentile (P in 0..100). Values are integers.
percentile() {
  local p="$1"; shift
  [ "$#" -eq 0 ] && { echo "NA"; return; }
  printf '%s\n' "$@" | sort -n | awk -v p="$p" '
    {a[NR]=$0}
    END{
      n=NR
      r=int((p/100)*n)
      if (r < (p/100)*n) r++
      if (r<1) r=1
      if (r>n) r=n
      print a[r]
    }'
}

_min() { [ "$#" -eq 0 ] && { echo NA; return; }; printf '%s\n' "$@" | sort -n | head -1; }
_max() { [ "$#" -eq 0 ] && { echo NA; return; }; printf '%s\n' "$@" | sort -n | tail -1; }

# _wait_ready <ready-fn>: poll ready-fn every POLL_INTERVAL up to READY_TIMEOUT.
# Returns 0 on ready, 1 on timeout. (Generic single-signal waiter.)
_wait_ready() {
  local fn="$1" start_ms elapsed
  start_ms=$(now_ms)
  while :; do
    if "$fn"; then return 0; fi
    elapsed=$(( $(now_ms) - start_ms ))
    [ "$elapsed" -ge $(( READY_TIMEOUT * 1000 )) ] && return 1
    sleep "$POLL_INTERVAL"
  done
}

# Wait for both signals from a single trigger timestamp, recording each independently.
# Args: <trigger_ms>. Sets globals CDP_MS / NOVNC_MS ("" if timed out). Returns 0 if
# both ready, 1 if timeout reached with at least one missing.
_wait_both() {
  local start_ms="$1" elapsed
  CDP_MS=""; NOVNC_MS=""
  while :; do
    [ -z "$CDP_MS" ]   && bp_ready_cdp   && CDP_MS=$(( $(now_ms) - start_ms ))
    [ -z "$NOVNC_MS" ] && bp_ready_novnc && NOVNC_MS=$(( $(now_ms) - start_ms ))
    [ -n "$CDP_MS" ] && [ -n "$NOVNC_MS" ] && return 0
    elapsed=$(( $(now_ms) - start_ms ))
    [ "$elapsed" -ge $(( READY_TIMEOUT * 1000 )) ] && return 1
    sleep "$POLL_INTERVAL"
  done
}

_report() {
  local label="$1"; shift
  if [ "$#" -eq 0 ]; then
    printf '%-6s n=0  (no successful runs)\n' "$label"
    return
  fi
  printf '%-6s n=%-3d p50=%sms p95=%sms min=%sms max=%sms\n' \
    "$label" "$#" "$(percentile 50 "$@")" "$(percentile 95 "$@")" "$(_min "$@")" "$(_max "$@")"
}

bench_main() {
  echo "=== bench $PLATFORM mode=$MODE count=$COUNT (timeout=${READY_TIMEOUT}s poll=${POLL_INTERVAL}s) ==="
  bp_setup || { echo "bp_setup failed"; return 1; }
  trap 'bp_teardown' EXIT

  local cdp_times=() novnc_times=()
  local i start
  for i in $(seq 1 "$COUNT"); do
    if [ "$MODE" = "cold" ]; then
      bp_make_cold || { echo "bp_make_cold failed on run $i"; return 1; }
    else
      if ! bp_make_paused; then
        echo "MODE=resume: bp_make_paused unsupported on $PLATFORM; aborting."
        return 2
      fi
    fi

    start=$(now_ms)
    bp_trigger || { echo "bp_trigger failed on run $i"; return 1; }
    _wait_both "$start"

    [ -n "$CDP_MS" ]   && cdp_times+=("$CDP_MS")
    [ -n "$NOVNC_MS" ] && novnc_times+=("$NOVNC_MS")
    printf 'run %2d/%d  cdp=%-7s novnc=%-7s\n' "$i" "$COUNT" \
      "${CDP_MS:-TIMEOUT}ms" "${NOVNC_MS:-TIMEOUT}ms"
  done

  echo "--- results: $PLATFORM mode=$MODE ---"
  _report CDP   "${cdp_times[@]}"
  _report noVNC "${novnc_times[@]}"
}
