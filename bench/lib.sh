#!/usr/bin/env bash
# Shared, platform-agnostic timing harness for chrome-live startup benchmarks.
#
# A platform script sources this file, defines the bp_* hooks below, then calls
# bench_main. Per timed run, the harness:
#   1. Calls bp_make_cold or bp_make_paused (untimed prep).
#   2. Records start_ms and calls bp_trigger (the action whose latency we measure).
#   3. Concurrently polls every readiness hook the platform defined, from the
#      SAME start_ms, until each one succeeds or READY_TIMEOUT is reached.
#   4. Logs one "[run i/N] <signal> ready in Xms" line per signal as it succeeds.
#
# Readiness signals (each is independently polled and reported):
#   bp_ready_cdp_internal   in-container CDP probe (e.g. SDK exec runs curl
#                           against 127.0.0.1:9222 inside the sandbox). This is
#                           the PRIMARY signal for platform decisions, because
#                           it isolates real container readiness from any
#                           public-edge/proxy/tunnel overhead.
#   bp_ready_cdp_external   public-endpoint CDP probe (curl against the
#                           platform's public URL / IP). Reported for context;
#                           shows the latency a naive HTTP client over the
#                           public edge would actually see.
#   bp_ready_novnc          noVNC HTTP probe (informational only; never feeds
#                           the result summary).
#   bp_ready_cdp            deprecated alias for bp_ready_cdp_external.
#
# A platform must define bp_ready_cdp_internal AND/OR bp_ready_cdp_external (at
# least one). Any signal not defined is simply skipped.
#
# Required hooks:
#   bp_setup        one-time provisioning (build/push image, create instance)
#   bp_make_cold    put target into a stopped/destroyed state (pre-cold-boot)
#   bp_make_paused  put target into a paused/suspended state (pre-resume); may
#                   `return 1` if the platform has no snapshot/resume path
#   bp_trigger      the TIMED action: start or resume the instance
#   bp_teardown     destroy everything created by bp_setup
#
# Env knobs:
#   MODE=cold|resume   which start path to measure (default cold)
#   COUNT=N            number of timed runs (default 10)
#   READY_TIMEOUT=S    per-run timeout in seconds waiting for ready (default 90)
#   POLL_INTERVAL=S    seconds between readiness polls (default 0.2)
set -euo pipefail

now_ms() {
  # Bash 5 exposes microsecond wall-clock without a subprocess.
  if [[ -n "${EPOCHREALTIME:-}" ]]; then
    # "<seconds>.<6-digit-microseconds>"; strip the separator to get integer
    # microseconds and divide. Integer math only: routing through awk/printf
    # floats loses precision to %.6g.
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
  # _wait_ready <check-fn> <start_ms> -> echoes elapsed ms since start_ms, or "TIMEOUT"
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

# Result accumulators (populated by _poll_signals across COUNT runs).
declare -a INT_MS=() EXT_MS=() NOVNC_MS=()

# Backward compat: bp_ready_cdp (no suffix) is treated as bp_ready_cdp_external.
_resolve_hooks() {
  HAS_INT=0; HAS_EXT=0; HAS_NOVNC=0
  declare -F bp_ready_cdp_internal >/dev/null && HAS_INT=1
  if declare -F bp_ready_cdp_external >/dev/null; then
    HAS_EXT=1
  elif declare -F bp_ready_cdp >/dev/null; then
    # legacy single hook -> external
    bp_ready_cdp_external() { bp_ready_cdp "$@"; }
    HAS_EXT=1
  fi
  declare -F bp_ready_novnc >/dev/null && HAS_NOVNC=1
  if (( ! HAS_INT && ! HAS_EXT )); then
    echo "[bench] ERROR: platform script must define bp_ready_cdp_internal or bp_ready_cdp_external" >&2
    exit 2
  fi
}

# _poll_signals <run-i> <count> <start_ms>
# Polls every defined readiness hook from the same start_ms.
#
# External + noVNC probes are pure curl loops -> run them in BACKGROUND
# subshells (concurrent with everything else).
#
# Internal probe may rely on a bash coproc owned by the parent shell
# (Daytona/Modal). Coproc FDs are not safe to share with backgrounded
# subshells, so the internal probe runs in the FOREGROUND. Since the platform
# implementations make bp_ready_cdp_internal a single blocking SDK call that
# returns when CDP is reachable from inside the container, this is naturally
# concurrent with the curl probes: the foreground blocks on the SDK, the
# background subshells poll curl, all measure from the same start_ms.
# _spawn_probe <outfile> <check-fn> <start_ms>
# Fork a background subshell that runs _wait_ready, writing result to outfile.
# Closes coproc FDs declared via BENCH_COPROC_FDS (space-separated FD numbers)
# so the subshell does not hold the parent's coproc pipe open. Returns the
# subshell PID via stdout so the caller can `wait` on it explicitly (using
# `wait` with no args has been observed to deadlock when a bash coproc is
# present, even after subshells have exited).
_spawn_probe() {
  local out="$1" check="$2" start_ms="$3"
  local close_cmd=""
  if [[ -n "${BENCH_COPROC_FDS:-}" ]]; then
    local fd
    for fd in $BENCH_COPROC_FDS; do
      close_cmd+="exec ${fd}>&- ${fd}<&-; "
    done
  fi
  (
    eval "$close_cmd"
    _wait_ready "$check" "$start_ms" > "$out"
  ) &
  echo $!
}

_poll_signals() {
  local i="$1" count="$2" start_ms="$3"
  local tmp
  tmp=$(mktemp -d)

  # Order matters when the internal probe goes through a bash coproc
  # (Daytona, Modal). If we forked the ext/novnc subshells first, they would
  # inherit the parent's coproc FDs, and bash's read on the parent side
  # deadlocks with the multiple FD holders. Run the internal probe FIRST,
  # alone, while the parent is still the only holder of the coproc. Then
  # spawn the curl-based ext/novnc probes. _wait_ready uses start_ms for
  # elapsed math, so running ext/novnc later doesn't bias their numbers.
  if (( HAS_INT )); then
    _wait_ready bp_ready_cdp_internal "$start_ms" > "$tmp/int" || true
  fi

  # Background subshells for curl-only probes. Two coproc-related gotchas:
  # 1. Inherited coproc FDs in subshells → _spawn_probe closes them.
  # 2. `wait` with no args can deadlock when a coproc is present, even after
  #    subshells exited → we wait on each PID explicitly with a small poll
  #    loop instead. Poll uses `kill -0` (cheap, doesn't actually signal).
  local ext_pid="" novnc_pid=""
  (( HAS_EXT ))   && ext_pid=$(_spawn_probe   "$tmp/ext"   bp_ready_cdp_external "$start_ms")
  (( HAS_NOVNC )) && novnc_pid=$(_spawn_probe "$tmp/novnc" bp_ready_novnc        "$start_ms")
  local wait_deadline=$(( $(now_ms) + (READY_TIMEOUT + 5) * 1000 ))
  while :; do
    local alive=0
    [[ -n "$ext_pid"   ]] && kill -0 "$ext_pid"   2>/dev/null && alive=1
    [[ -n "$novnc_pid" ]] && kill -0 "$novnc_pid" 2>/dev/null && alive=1
    (( alive == 0 )) && break
    [[ $(now_ms) -ge $wait_deadline ]] && {
      echo "[bench] WARN: probe subshells didn't exit within deadline; abandoning them" >&2
      [[ -n "$ext_pid"   ]] && kill -KILL "$ext_pid"   2>/dev/null
      [[ -n "$novnc_pid" ]] && kill -KILL "$novnc_pid" 2>/dev/null
      break
    }
    sleep 0.1
  done

  local v
  if (( HAS_INT )) && v=$(cat "$tmp/int" 2>/dev/null); then
    if [[ "$v" == "TIMEOUT" || -z "$v" ]]; then
      echo "[run $i/$count] cdp internal TIMEOUT"
    else
      echo "[run $i/$count] cdp internal ready in ${v}ms"
      INT_MS+=("$v")
    fi
  fi
  local v
  if (( HAS_EXT )) && v=$(cat "$tmp/ext" 2>/dev/null); then
    if [[ "$v" == "TIMEOUT" ]]; then
      echo "[run $i/$count] cdp external TIMEOUT"
    else
      echo "[run $i/$count] cdp external ready in ${v}ms"
      EXT_MS+=("$v")
    fi
  fi
  if (( HAS_NOVNC )) && v=$(cat "$tmp/novnc" 2>/dev/null); then
    if [[ "$v" == "TIMEOUT" ]]; then
      echo "[run $i/$count] novnc TIMEOUT (informational)"
    else
      echo "[run $i/$count] novnc ready in ${v}ms (informational)"
    fi
  fi

  rm -rf "$tmp"
}

_BENCH_PID=""
_WATCHDOG_PID=""

# Hard wall-clock watchdog. Some platform SDKs hang silently (observed:
# daytona.start sometimes never returns even though the sandbox actually
# started), so per-op timeouts inside the SDK are not enough. This watchdog
# is the one timeout that always fires, regardless of any platform behavior:
# a background subshell sleeps BENCH_TIMEOUT seconds, then signals the bench
# process. SIGUSR1 first (interrupts read/wait, lets the EXIT trap run
# teardown). After a grace period, SIGTERM / SIGKILL against bench + direct
# children as a last resort.
_install_watchdog() {
  local secs="${BENCH_TIMEOUT:-300}"
  [[ "$secs" =~ ^[0-9]+$ ]] || { echo "[bench] BENCH_TIMEOUT must be a positive integer (got '$secs')" >&2; exit 2; }
  [[ "$secs" -le 0 ]] && return 0
  _BENCH_PID=$$
  trap '_on_watchdog_fire' USR1
  (
    sleep "$secs"
    echo "[bench] BENCH_TIMEOUT=${secs}s exceeded; aborting" >&2
    kill -USR1 "$_BENCH_PID" 2>/dev/null
    sleep 30
    pkill -P "$_BENCH_PID" 2>/dev/null
    kill -TERM "$_BENCH_PID" 2>/dev/null
    sleep 5
    pkill -9 -P "$_BENCH_PID" 2>/dev/null
    kill -KILL "$_BENCH_PID" 2>/dev/null
  ) &
  _WATCHDOG_PID=$!
  disown "$_WATCHDOG_PID" 2>/dev/null || true
}

_on_watchdog_fire() {
  echo "[bench] watchdog fired; running teardown and exiting 124" >&2
  exit 124
}

_shutdown_watchdog() {
  [[ -n "$_WATCHDOG_PID" ]] && kill "$_WATCHDOG_PID" 2>/dev/null
  _WATCHDOG_PID=""
}

bench_main() {
  : "${MODE:=cold}"
  : "${COUNT:=10}"
  : "${READY_TIMEOUT:=90}"
  : "${POLL_INTERVAL:=0.2}"
  : "${BENCH_TIMEOUT:=300}"
  local platform="${PLATFORM:-unknown}"

  echo "[bench] platform=$platform mode=$MODE count=$COUNT timeout=${READY_TIMEOUT}s poll=${POLL_INTERVAL}s wall=${BENCH_TIMEOUT}s"
  _install_watchdog
  echo "[setup] running bp_setup"
  bp_setup
  _resolve_hooks
  trap 'echo "[teardown] running bp_teardown"; bp_teardown || true; _shutdown_watchdog' EXIT

  local i start
  for (( i=1; i<=COUNT; i++ )); do
    if [[ "$MODE" == "resume" ]]; then
      echo "[run $i/$COUNT] prep: paused"
      bp_make_paused || { echo "[run $i/$COUNT] platform has no resume path; use MODE=cold"; exit 2; }
      echo "[run $i/$COUNT] trigger (resume)"
    else
      echo "[run $i/$COUNT] prep: cold-stopped"
      bp_make_cold
      echo "[run $i/$COUNT] trigger (cold start)"
    fi

    start=$(now_ms)
    bp_trigger
    _poll_signals "$i" "$COUNT" "$start"
  done

  (( HAS_INT )) && _report "CDP internal" "${INT_MS[@]}"
  (( HAS_EXT )) && _report "CDP external" "${EXT_MS[@]}"
}

_report() {
  local label="$1"; shift
  local -a v
  IFS=$'\n' v=($(printf '%s\n' "$@" | sort -n)); unset IFS
  local n=${#v[@]}
  if [[ $n -eq 0 ]]; then
    echo "[result] $label n=0 (no successful runs)"
    return
  fi
  echo "[result] $label n=$n p50=$(percentile 50 "${v[@]}")ms p95=$(percentile 95 "${v[@]}")ms min=${v[0]}ms max=${v[$((n-1))]}ms"
}
