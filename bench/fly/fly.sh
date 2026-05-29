#!/usr/bin/env bash
# Fly.io platform hooks for the startup-latency bench.
# Usage:
#   bench/fly.sh setup
#   MODE=cold   COUNT=10 bench/fly.sh run
#   MODE=resume COUNT=10 bench/fly.sh run
#   bench/fly.sh teardown
#   bench/fly.sh rootfs    # one-off: report rootfs storage type
#
# State (machine id + IP) is persisted in bench/.fly-state so cold and resume
# passes reuse the SAME provisioned machine (image already host-local => no re-pull).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM=fly

# Auto-load bench/.env so FLY_API_TOKEN (and any overrides) come along without
# having to be exported separately. Override path with ENV_FILE=/path/to/.env.
ENV_FILE="${ENV_FILE:-$HERE/../.env}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

. "$HERE/../lib.sh"

# Local poll helper: lib.sh's _wait_ready has a 2-arg signature (check + start_ms);
# bp_setup just wants "wait until ready or timeout", so use this instead.
_await() {
  local fn="$1" timeout="${2:-90}" start
  start=$(now_ms)
  while :; do
    "$fn" >/dev/null 2>&1 && return 0
    [ $(( ($(now_ms) - start) / 1000 )) -ge "$timeout" ] && return 1
    sleep "${POLL_INTERVAL:-0.2}"
  done
}

APP="${APP:-${FLY_APP:-chrome-live-bench}}"
ORG="${ORG:-${FLY_ORG_SLUG:-remote-browsers-dev}}"
REGION="${REGION:-sjc}"
IMAGE="${IMAGE:-ghcr.io/remotebrowser/chrome-live:latest}"
VM_SIZE="${VM_SIZE:-shared-cpu-4x}"
VM_MEM="${VM_MEM:-2048}"
STATE="$HERE/.fly-state"

_save_state() { printf 'MID=%s\nIP4=%s\n' "$1" "$2" > "$STATE"; }
_load_state() { [ -f "$STATE" ] && . "$STATE"; }

_mstate() { flyctl machine list -a "$APP" --json 2>/dev/null | jq -r --arg id "$MID" '.[] | select(.id==$id) | .state // empty'; }

_wait_mstate() {
  # _wait_mstate <target> <timeout-s>
  local target="$1" to="${2:-60}" start
  start="$(date +%s)"
  while :; do
    [ "$(_mstate)" = "$target" ] && return 0
    [ $(( $(date +%s) - start )) -ge "$to" ] && { echo "  ! timeout waiting for state=$target (got '$(_mstate)')" >&2; return 1; }
    sleep 0.5
  done
}

bp_setup() {
  if flyctl status -a "$APP" >/dev/null 2>&1; then
    echo "[setup] app $APP exists, reusing"
  else
    echo "[setup] creating app $APP in org $ORG"
    flyctl apps create "$APP" --org "$ORG"
  fi

  # Dedicated v4 is required: shared IPv4 only serves :80/:443, but CDP is on :9222.
  if ! flyctl ips list -a "$APP" --json 2>/dev/null | jq -e '.[] | select(.Type=="v4")' >/dev/null; then
    echo "[setup] allocating dedicated IPv4 (needed for port 9222) + IPv6"
    flyctl ips allocate-v4 -a "$APP" --yes
    flyctl ips allocate-v6 -a "$APP"
  fi

  echo "[setup] running machine: $VM_SIZE / ${VM_MEM}MB / $REGION (image pull happens HERE, untimed)"
  flyctl machine run "$IMAGE" -a "$APP" \
    --region "$REGION" --vm-size "$VM_SIZE" --vm-memory "$VM_MEM" \
    --port 80:80/tcp:http --port 9222:9222/tcp:http >/dev/null

  MID="$(flyctl machine list -a "$APP" --json | jq -r '.[0].id')"
  IP4="$(flyctl ips list -a "$APP" --json | jq -r '.[] | select(.Type=="v4") | .Address' | head -1)"
  [ -n "$MID" ] && [ -n "$IP4" ] || { echo "[setup] failed to capture MID/IP4"; exit 1; }
  _save_state "$MID" "$IP4"
  echo "[setup] machine=$MID ip4=$IP4"

  echo "[setup] waiting for first full boot (untimed)"
  _load_state
  _await bp_ready_cdp_external "${SETUP_TIMEOUT:-180}" || { echo "[setup] CDP never came up"; exit 1; }
  _await bp_ready_novnc "${SETUP_TIMEOUT:-180}" || { echo "[setup] noVNC never came up"; exit 1; }
  echo "[setup] first boot complete"

  echo "[setup] warm-up cold cycle (untimed; avoids cold-cache outlier on run 1)"
  bp_make_cold
  bp_trigger
  _await bp_ready_cdp_external "${SETUP_TIMEOUT:-180}" || echo "[setup] WARN warm-up CDP slow/failed (non-fatal)"
  echo "[setup] done"
}

bp_make_cold() {
  _load_state
  flyctl machine stop "$MID" -a "$APP" >/dev/null 2>&1
  _wait_mstate stopped 60
}

bp_make_paused() {
  _load_state
  # suspend => resume path. Returns non-zero if Fly rejects suspend (e.g. >2GB ceiling).
  if ! flyctl machine suspend "$MID" -a "$APP" >/dev/null 2>&1; then
    return 1
  fi
  _wait_mstate suspended 60 || return 1
}

bp_trigger() {
  _load_state
  flyctl machine start "$MID" -a "$APP" >/dev/null 2>&1
}

bp_ready_cdp_external() {
  _load_state
  # Fly machines get a dedicated public IPv4; no shared proxy in the path, so
  # the "external" probe here is just direct TCP to the machine port.
  curl -fsS --max-time 3 "http://$IP4:9222/json/version" >/dev/null 2>&1
}

# Internal: shell into the machine and curl 127.0.0.1:9222 from inside it.
# Adds a flyctl-exec round trip; useful as an apples-to-apples comparison with
# Daytona / Modal / E2B (which only have an internal path).
bp_ready_cdp_internal() {
  _load_state
  flyctl machine exec "$MID" -a "$APP" \
    'curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:9222/json/version' \
    >/dev/null 2>&1
}

bp_ready_novnc() {
  _load_state
  curl -fsS --max-time 3 -o /dev/null "http://$IP4/" 2>&1
}

bp_teardown() {
  echo "[teardown] destroying app $APP"
  flyctl apps destroy "$APP" --yes
  rm -f "$STATE"
}

bp_rootfs() {
  _load_state
  echo "== rootfs probe on machine $MID =="
  flyctl machine exec "$MID" -a "$APP" \
    'sh -lc "echo ROOT_MOUNT:; grep \" / \" /proc/mounts; echo; echo LSBLK:; lsblk -o NAME,ROTA,TYPE,SIZE 2>/dev/null || true; echo; echo ROTATIONAL:; for d in /sys/block/*/queue/rotational; do echo \$d=\$(cat \$d); done; echo; echo DF:; df -h /; echo; echo READTEST:; dd if=/usr/bin/google-chrome of=/dev/null bs=1M 2>&1 | tail -1"'
}

case "${1:-}" in
  setup)    bp_setup ;;
  run)
    # Fly's workflow is multi-step: setup once, run many times, teardown explicit.
    # Disable the harness's auto-teardown-on-EXIT so the app survives until the
    # explicit `teardown` subcommand. The original bp_teardown is still invoked
    # by the `teardown` case below in a fresh process.
    bp_teardown() { echo "[teardown] (skipped; run '$0 teardown' to destroy)"; }
    bench_main
    ;;
  teardown) bp_teardown ;;
  rootfs)   bp_rootfs ;;
  *) echo "usage: $0 {setup|run|teardown|rootfs}"; exit 1 ;;
esac
