#!/usr/bin/env bash
# Northflank implementation of the bench bp_* hook contract (see ../lib.sh).
#
#   MODE=cold COUNT=10 READY_TIMEOUT=180 bash bench/northflank/northflank.sh
#
# Northflank runs plain Kubernetes pods. There is NO memory-snapshot resume
# (unlike Fly suspend / E2B pause / Modal checkpoint): scaling a deployment
# service 0->1 is a cold pod restart + Chrome relaunch. So Northflank is a
# direct/cold platform like Daytona, Hetzner, and Cloudflare. Only MODE=cold is
# meaningful; MODE=resume returns 1 from bp_make_paused and the harness exits.
#
# ADAPTED BOOT (the headline integration finding): Northflank always runs the
# container as PID 2 under its own `env-injector` PID-1 shim (mandatory secret
# injection, no opt-out) and grants only the default Docker capability set
# (CapEff=0xa80425fb, no CAP_SYS_ADMIN). s6-overlay refuses to run unless PID 1,
# and the unshare(CLONE_NEWPID) workaround that chrome-live uses on Fly/Daytona
# needs CAP_SYS_ADMIN, so it fails here with "unshare: Operation not permitted"
# and the stock/daytona images crash-loop. This is the same blocker as Modal
# (there gVisor denies unshare). We therefore clear the entrypoint and launch the
# chrome-live services directly, byte-for-byte the same adapted boot the Modal
# bench uses (Xvnc + tinyproxy + websockify noVNC + Chrome + socat CDP proxy
# :9222<-:9221). XFCE / browser-trace are omitted (not on the CDP/noVNC path).
#
# READINESS SIGNAL: Northflank public ports are HTTP/HTTP2 only (no L4/TCP), and
# the *.code.run ingress forwards the public Host header, which Chrome's CDP
# rejects ("Host header is specified and is not an IP address or localhost",
# HTTP 500) -- the same Host-header limitation seen on E2B/Modal. There is no
# usable external /json/version 200 and no usable internal probe either
# (Northflank's exec backend returns HTTP 500 for this service). BUT the cdp-proxy
# socat only starts listening once Chrome's CDP (:9221) is up, so the ingress
# returns 503 (no upstream) while CDP is down and flips to a Chrome HTTP response
# (500 Host-reject, or 200) the instant CDP is ready. We measure that flip: it is
# CDP-HTTP readiness as seen through the public edge.
#
# Hook mapping:
#   bp_make_cold   -> scale to 0 instances, wait until .status.deployment == null
#   bp_make_paused -> return 1 (no memory resume)
#   bp_trigger     -> scale to 1 instance (TIMED)
#   bp_ready_cdp_external -> ingress CDP returns a Chrome HTTP response (PRIMARY)
#   bp_ready_novnc        -> ingress noVNC :80 returns 200 (informational)
#
# One service is created once in bp_setup and reused (scale 0<->1) so the ~2GB
# image is pulled once and excluded from the timed path. Caveat: a scale 0->1 may
# reschedule onto a node without the image cached -> cross-node variance.
#
# Region/rootfs: project remote-browser is in us-central (GCP nf-us-central).
# Rootfs storage type is not probed (exec is unavailable on this platform).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/../lib.sh"
PLATFORM=northflank
: "${READY_TIMEOUT:=180}"   # cold pod schedule + Chrome boot on Northflank is slow

# --- config -----------------------------------------------------------------
# The paid token lives in the repo-root .env, NOT bench/.env (which still holds
# the old free-tier token). In a git worktree the root .env is absent; pass
# ENV_FILE=/abs/path/to/.env explicitly there.
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../../.env}"
API="${API:-https://api.northflank.com/v1}"
PROJECT="${PROJECT:-remote-browser}"
PLAN="${PLAN:-nf-compute-200-4}"   # 2 vCPU / 4 GiB, $0.067/hr (no 2vCPU/2GiB tier)
SERVICE="${SERVICE:-chrome-live-bench}"
# Base chrome-live amd64 image. The adapted boot ignores the image ENTRYPOINT, so
# the base image (not the daytona variant) is used; it carries every binary the
# boot script invokes.
IMAGE="${IMAGE:-ghcr.io/remotebrowser/chrome-live@sha256:08c3b92806d704f77aef19465ae69e83efa2ff0bf4eb9b7835b055d4e4f65a49}"
CURL_MAXTIME="${CURL_MAXTIME:-5}"

# Chrome flags + adapted-boot script, byte-identical to bench/modal/modal_bench.py
# (websockify serves noVNC on :80).
CHROME_FLAGS='--start-maximized --no-sandbox --no-first-run --disable-default-apps --no-default-browser-check --remote-debugging-port=9221 --disable-dev-shm-usage --disable-gpu --disable-software-rasterizer --disable-features=OptimizationGuideModelDownloading,OptimizationHints,OptimizationTargetPrediction --disable-background-networking --disable-component-update --disable-domain-reliability --disable-sync --no-pings --user-data-dir=/home/user/chrome-profile --proxy-server=http://127.0.0.1:8119 --enable-logging=stderr --log-level=3 about:blank'

_boot_script() {
  cat <<BOOT
set -u
export DISPLAY=:99 HOME=/home/user NO_AT_BRIDGE=1 SESSION_MANAGER=""
sh /etc/cont-init.d/00-entrypoint.sh >/tmp/cont-init.log 2>&1 || true
Xvnc -alwaysshared :99 -geometry 1920x1080 -depth 24 -rfbport 5900 -SecurityTypes None >/tmp/xvnc.log 2>&1 &
tinyproxy -d -c /app/tinyproxy.conf >/tmp/tinyproxy.log 2>&1 &
websockify --web /usr/share/novnc/ 80 localhost:5900 >/tmp/novnc.log 2>&1 &
i=0; while [ \$i -lt 50 ]; do su user -s /bin/sh -c "DISPLAY=:99 xrdb -query" >/dev/null 2>&1 && break; i=\$((i+1)); sleep 0.2; done
su user -s /bin/sh -c 'HOME=/home/user DISPLAY=:99 exec google-chrome-stable $CHROME_FLAGS' >/tmp/chrome.log 2>&1 &
( while ! socat -T1 -u OPEN:/dev/null TCP:127.0.0.1:9221 >/dev/null 2>&1; do sleep 0.2; done; exec socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9221 ) >/tmp/cdp.log 2>&1 &
wait
BOOT
}

CDP_URL=""; NOVNC_URL=""; CDP_HOST=""; NOVNC_HOST=""; LB_IP=""

# The per-port *.code.run DNS is flaky from some resolvers (observed NXDOMAIN /
# negative-caching after a delete+recreate, e.g. via a Tailscale resolver). Both
# hostnames are just CNAMEs to the project load balancer, so we pin curl to the
# LB IP with --resolve and let the ingress route by the (real) Host header.
_resolve_opt() { [[ -n "$LB_IP" && -n "$1" ]] && printf -- '--resolve %s:443:%s' "$1" "$LB_IP"; }

# --- REST helpers ------------------------------------------------------------
# api METHOD PATH [json-body] -> prints body; returns nonzero on HTTP >=400.
api() {
  local method="$1" path="$2" body="${3:-}"
  local out code
  if [[ -n "$body" ]]; then
    out=$(curl -sS --max-time 60 -w $'\n%{http_code}' -X "$method" \
      -H "Authorization: Bearer $NORTHFLANK_API_TOKEN" \
      -H "Content-Type: application/json" -d "$body" "$API$path")
  else
    out=$(curl -sS --max-time 60 -w $'\n%{http_code}' -X "$method" \
      -H "Authorization: Bearer $NORTHFLANK_API_TOKEN" "$API$path")
  fi
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  printf '%s' "$body"
  [[ "$code" =~ ^2 ]]
}

# .status.deployment becomes null shortly after scaling to 0 -> our down signal.
nf_is_down() {
  local j; j=$(api GET "/projects/$PROJECT/services/$SERVICE") || return 1
  [[ -n "${NF_DEBUG:-}" ]] && printf '%s\n' "$j" >&2
  printf '%s' "$j" | jq -e '.data.status.deployment == null' >/dev/null 2>&1
}

_wait_down() {
  local t="${1:-$READY_TIMEOUT}" deadline
  deadline=$(( $(now_ms) + t * 1000 ))
  while :; do
    nf_is_down && return 0
    [[ $(now_ms) -ge $deadline ]] && return 1
    sleep 0.5
  done
}

# --- hooks -------------------------------------------------------------------
bp_setup() {
  [[ -f "$ENV_FILE" ]] || { echo "[setup] ENV_FILE not found: $ENV_FILE"; return 1; }
  set -a; . "$ENV_FILE"; set +a
  [[ -n "${NORTHFLANK_API_TOKEN:-}" ]] || { echo "[setup] NORTHFLANK_API_TOKEN not in $ENV_FILE"; return 1; }

  # Idempotent: remove any leftover service from a prior failed run (set -e skips
  # teardown if bp_setup fails), then wait for it to clear before recreating.
  if ! api GET "/projects/$PROJECT/services/$SERVICE" >/dev/null 2>&1; then :; else
    echo "[setup] removing leftover $SERVICE"
    api DELETE "/projects/$PROJECT/services/$SERVICE" >/dev/null 2>&1 || true
    local dl; dl=$(( $(now_ms) + 120000 ))
    until ! api GET "/projects/$PROJECT/services/$SERVICE" >/dev/null 2>&1; do
      [[ $(now_ms) -ge $dl ]] && { echo "[setup] leftover did not clear"; return 1; }
      sleep 3
    done
  fi

  LB_IP=$(api GET "/projects/$PROJECT" | jq -r '.data.cluster.loadBalancers[0]' 2>/dev/null \
            | { read -r h; [[ -n "$h" ]] && dig +short "$h" 2>/dev/null | grep -E '^[0-9]' | head -1; })
  echo "[setup] load balancer IP: ${LB_IP:-<unresolved>}"

  local b64 cmd body resp
  b64=$(_boot_script | base64 | tr -d '\n')
  # Pass the boot script base64-encoded to dodge all JSON/shell quoting; decode
  # and exec it inside the container. customCommand is space-split by Northflank
  # with quote support; the single-quoted arg contains only base64+pipe+redirect.
  cmd="-c 'echo $b64 | base64 -d > /tmp/boot.sh; exec sh /tmp/boot.sh'"

  echo "[setup] creating deployment service $SERVICE ($PLAN, adapted boot) from $IMAGE"
  body=$(jq -n --arg img "$IMAGE" --arg plan "$PLAN" --arg svc "$SERVICE" --arg cmd "$cmd" '{
    name:$svc, billing:{deploymentPlan:$plan},
    deployment:{instances:1, external:{imagePath:$img},
      docker:{configType:"customEntrypointCustomCommand", customEntrypoint:"/bin/sh", customCommand:$cmd}},
    ports:[{name:"cdp",internalPort:9222,public:true,protocol:"HTTP"},
           {name:"novnc",internalPort:80,public:true,protocol:"HTTP"}]
  }')
  resp=$(api POST "/projects/$PROJECT/services/deployment" "$body") || {
    echo "[setup] create failed: $(printf '%s' "$resp" | head -c 400)"; return 1; }

  CDP_HOST=$(printf '%s' "$resp" | jq -r '.data.ports[]? | select(.internalPort==9222) | .dns' 2>/dev/null | head -1)
  NOVNC_HOST=$(printf '%s' "$resp" | jq -r '.data.ports[]? | select(.internalPort==80) | .dns' 2>/dev/null | head -1)
  CDP_URL=${CDP_HOST:+https://$CDP_HOST}
  NOVNC_URL=${NOVNC_HOST:+https://$NOVNC_HOST}
  echo "[setup] CDP public URL:   ${CDP_URL:-<none>}"
  echo "[setup] noVNC public URL: ${NOVNC_URL:-<none>} (informational)"

  echo "[setup] waiting for first boot (CDP via ingress)"
  local d; d=$(( $(now_ms) + READY_TIMEOUT * 2 * 1000 ))
  until bp_ready_cdp_external; do
    [[ $(now_ms) -ge $d ]] && { echo "[setup] first boot did not reach CDP"; return 1; }
    sleep 1
  done
  echo "[setup] first boot OK (CDP responding through ingress)"
}

bp_make_cold() {
  api POST "/projects/$PROJECT/services/$SERVICE/scale" '{"instances":0}' >/dev/null \
    || { echo "[make_cold] scale-to-0 failed"; return 1; }
  _wait_down || echo "[make_cold] WARN: deployment still not null after timeout"
}

bp_make_paused() { return 1; }   # no memory-snapshot resume on Northflank

bp_trigger() {
  api POST "/projects/$PROJECT/services/$SERVICE/scale" '{"instances":1}' >/dev/null
}

# PRIMARY signal. Ready iff the public CDP ingress returns a Chrome HTTP response:
# a /json/version 200 (body has webSocketDebuggerUrl) OR the 500 Host-reject body.
# Both mean the cdp-proxy socat (gated on Chrome's :9221) is up == CDP ready. A
# 503 (no upstream) or connection error does NOT match.
bp_ready_cdp_external() {
  [[ -n "$CDP_URL" ]] || return 1
  curl -sS --max-time "$CURL_MAXTIME" $(_resolve_opt "$CDP_HOST") "$CDP_URL/json/version" 2>/dev/null \
    | grep -qE 'webSocketDebuggerUrl|Host header is specified'
}

bp_ready_novnc() {
  [[ -n "$NOVNC_URL" ]] || return 1
  curl -fsS --max-time "$CURL_MAXTIME" $(_resolve_opt "$NOVNC_HOST") -o /dev/null "$NOVNC_URL/"
}

bp_teardown() {
  api DELETE "/projects/$PROJECT/services/$SERVICE" >/dev/null 2>&1 \
    && echo "[teardown] deleted service $SERVICE" \
    || echo "[teardown] WARN: delete failed (check Northflank console for $SERVICE)"
}

# --- run ---------------------------------------------------------------------
bench_main
