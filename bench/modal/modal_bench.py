#!/usr/bin/env python3
"""Modal driver for the chrome-live startup bench.

Runs as a persistent daemon (`serve`) so the Modal SDK import + client
connection cost is paid once, not per timed iteration. bench/lib.sh owns all
wall-clock timing; this process only creates / snapshots / restores sandboxes
and prints tunnel URLs back over stdout.

Protocol (one command per stdin line -> one status line on stdout):
  setup              -> SETUP_OK cmd=<json> cpu=<n> mem=<MiB> arch=<m> nproc=<n> memkb=<n>
  trigger-cold       -> TRIGGER_OK <cdp_url> <novnc_url>
  snapshot           -> SNAPSHOT_OK  | SNAPSHOT_UNSUPPORTED <reason>
  trigger-resume     -> TRIGGER_OK <cdp_url> <novnc_url>
  wait-cdp-internal  -> CDP_OK if `sb.exec(curl 127.0.0.1:9222/json/version)` exits 0;
                       CDP_NOT_READY otherwise. One-shot probe; the harness polls.
  teardown           -> TEARDOWN_OK
  quit               -> (exits)
Any failure prints:  ERR <message>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

import modal

APP_NAME = "chrome-live-bench"
IMAGE_REF = "ghcr.io/remotebrowser/chrome-live:latest"
PORTS = [80, 9222]
CPU = 2.0
MEMORY_MIB = 2048
SANDBOX_TIMEOUT = 600  # max lifetime per sandbox (s); bench tears down sooner

# The stock image boots via s6-overlay (ENTRYPOINT start-init.sh -> /init), which
# refuses to run unless it is PID 1. Modal runs the sandbox command as PID 2 under
# its own `dumb-init`, and gVisor denies unshare(CLONE_NEWPID) (no CAP_SYS_ADMIN),
# so s6-overlay cannot start here. We therefore clear the image ENTRYPOINT and
# launch the same services directly, mirroring the s6 run scripts:
#   cont-init (hosts/adblock filter) -> Xvnc + tinyproxy -> Chrome -> socat CDP
#   proxy (:9222<-:9221) ; websockify serves noVNC on :80.
# XFCE and browser-trace are omitted: neither is on the CDP or noVNC-HTTP readiness
# path. This is a Modal-specific adapted boot, documented in the report.
CHROME_FLAGS = (
    "--start-maximized --no-sandbox --no-first-run --disable-default-apps "
    "--no-default-browser-check --remote-debugging-port=9221 --disable-dev-shm-usage "
    "--disable-gpu --disable-software-rasterizer "
    "--disable-features=OptimizationGuideModelDownloading,OptimizationHints,"
    "OptimizationTargetPrediction --disable-background-networking "
    "--disable-component-update --disable-domain-reliability --disable-sync --no-pings "
    "--user-data-dir=/home/user/chrome-profile --proxy-server=http://127.0.0.1:8119 "
    "--enable-logging=stderr --log-level=3 about:blank"
)
BOOT_SCRIPT = rf"""
set -u
export DISPLAY=:99 HOME=/home/user NO_AT_BRIDGE=1 SESSION_MANAGER=""
sh /etc/cont-init.d/00-entrypoint.sh >/tmp/cont-init.log 2>&1 || true
Xvnc -alwaysshared :99 -geometry 1920x1080 -depth 24 -rfbport 5900 -SecurityTypes None >/tmp/xvnc.log 2>&1 &
tinyproxy -d -c /app/tinyproxy.conf >/tmp/tinyproxy.log 2>&1 &
websockify --web /usr/share/novnc/ 80 localhost:5900 >/tmp/novnc.log 2>&1 &
i=0; while [ $i -lt 50 ]; do su user -s /bin/sh -c "DISPLAY=:99 xrdb -query" >/dev/null 2>&1 && break; i=$((i+1)); sleep 0.2; done
su user -s /bin/sh -c 'HOME=/home/user DISPLAY=:99 exec google-chrome-stable {CHROME_FLAGS}' >/tmp/chrome.log 2>&1 &
( while ! socat -T1 -u OPEN:/dev/null TCP:127.0.0.1:9221 >/dev/null 2>&1; do sleep 0.2; done; exec socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9221 ) >/tmp/cdp.log 2>&1 &
wait
"""
BOOT_CMD = ["sh", "-c", BOOT_SCRIPT]

_state: dict = {
    "app": None,
    "image": None,
    "cmd": BOOT_CMD,
    "env": {},
    "current": None,    # live Sandbox
    "snapshot": None,    # SandboxSnapshot for resume mode
}


def _log(msg: str) -> None:
    print(f"# {msg}", file=sys.stderr, flush=True)


def _reply(line: str) -> None:
    print(line, flush=True)


def _secrets(env: dict):
    return [modal.Secret.from_dict(env)] if env else []


def _create(cmd: list[str], env: dict, snapshot: bool = False) -> "modal.Sandbox":
    return modal.Sandbox.create(
        *cmd,
        image=_state["image"],
        app=_state["app"],
        encrypted_ports=PORTS,
        cpu=CPU,
        memory=MEMORY_MIB,
        timeout=SANDBOX_TIMEOUT,
        secrets=_secrets(env),
        _experimental_enable_snapshot=snapshot,
    )


def _tunnels(sb: "modal.Sandbox") -> tuple[str, str]:
    t = sb.tunnels()
    return t[9222].url, t[80].url


def _cdp_ok(cdp_url: str, timeout: float = 3.0) -> bool:
    # Chrome rejects /json unless Host is localhost/IP; SNI still routes by URL host.
    req = urllib.request.Request(f"{cdp_url}/json/version", headers={"Host": "localhost"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_cdp(cdp_url: str, deadline_s: float) -> bool:
    while time.time() < deadline_s:
        if _cdp_ok(cdp_url):
            return True
        time.sleep(0.5)
    return False


def _terminate_current() -> None:
    sb = _state.get("current")
    if sb is not None:
        try:
            sb.terminate()
        except Exception as e:  # noqa: BLE001
            _log(f"terminate failed: {e}")
    _state["current"] = None


def _probe_spec(sb: "modal.Sandbox") -> dict:
    def run(*args: str) -> str:
        p = sb.exec(*args)
        out = p.stdout.read()
        p.wait()
        return out.strip()

    spec = {}
    try:
        spec["arch"] = run("uname", "-m")
        spec["nproc"] = run("nproc")
        spec["memkb"] = run("sh", "-c", "grep MemTotal /proc/meminfo | awk '{print $2}'")
    except Exception as e:  # noqa: BLE001
        _log(f"spec probe failed: {e}")
    return spec


def cmd_setup() -> None:
    _state["app"] = modal.App.lookup(APP_NAME, create_if_missing=True)
    # Clear the image ENTRYPOINT (s6 /init) so our adapted boot runs directly.
    _state["image"] = modal.Image.from_registry(IMAGE_REF).entrypoint([])

    # Warm-up create: pulls + caches the image and validates the adapted boot,
    # so the timed cold runs measure container start, not image download.
    sb = _create(_state["cmd"], _state["env"])
    cdp_url, _ = _tunnels(sb)
    spec = _probe_spec(sb)
    ready = _wait_cdp(cdp_url, time.time() + 120)
    sb.terminate()
    if not ready:
        _reply("ERR warm-up boot never exposed CDP")
        return

    _reply(
        "SETUP_OK "
        f"cmd=adapted-boot "
        f"cpu={CPU} mem={MEMORY_MIB} "
        f"arch={spec.get('arch', '?')} "
        f"nproc={spec.get('nproc', '?')} "
        f"memkb={spec.get('memkb', '?')}"
    )


def cmd_trigger_cold() -> None:
    _terminate_current()
    sb = _create(_state["cmd"], _state["env"])
    _state["current"] = sb
    cdp_url, novnc_url = _tunnels(sb)
    _reply(f"TRIGGER_OK {cdp_url} {novnc_url}")


def cmd_snapshot() -> None:
    if _state.get("snapshot") is not None:
        _reply("SNAPSHOT_OK")  # already have one
        return
    sb = None
    try:
        sb = _create(_state["cmd"], _state["env"], snapshot=True)
        cdp_url, _ = _tunnels(sb)
        if not _wait_cdp(cdp_url, time.time() + 120):
            _reply("ERR snapshot source never exposed CDP")
            return
        snap = sb._experimental_snapshot()
    except Exception as e:  # noqa: BLE001
        _reply(f"SNAPSHOT_UNSUPPORTED {type(e).__name__}:{e!r}")
        return
    finally:
        if sb is not None:
            try:
                sb.terminate()
            except Exception:  # noqa: BLE001
                pass
    _state["snapshot"] = snap
    _reply("SNAPSHOT_OK")


def cmd_trigger_resume() -> None:
    _terminate_current()
    try:
        sb = modal.Sandbox._experimental_from_snapshot(_state["snapshot"])
    except Exception as e:  # noqa: BLE001
        _reply(f"ERR restore:{e}")
        return
    _state["current"] = sb
    cdp_url, novnc_url = _tunnels(sb)
    _reply(f"TRIGGER_OK {cdp_url} {novnc_url}")


def cmd_wait_cdp_internal() -> None:
    # Blocking internal CDP probe. Runs a tight curl loop INSIDE the sandbox
    # via Modal sb.exec(), pays the SDK round-trip cost once per timed run
    # instead of per poll iteration. Caps at ~30s inner so we don't hang.
    sb = _state.get("current")
    if sb is None:
        _reply("CDP_NOT_READY no_sandbox")
        return
    script = (
        "i=0; "
        "while ! curl -fsS -o /dev/null --max-time 1 "
        "http://127.0.0.1:9222/json/version; do "
        "i=$((i+1)); [ $i -gt 600 ] && exit 1; sleep 0.05; "
        "done"
    )
    try:
        p = sb.exec("sh", "-c", script)
        p.wait()
        if p.returncode == 0:
            _reply("CDP_OK")
        else:
            _reply(f"CDP_NOT_READY exit={p.returncode}")
    except Exception as e:  # noqa: BLE001
        _reply(f"CDP_NOT_READY {type(e).__name__}:{e}")


def cmd_teardown() -> None:
    _terminate_current()
    _reply("TEARDOWN_OK")


HANDLERS = {
    "setup": cmd_setup,
    "trigger-cold": cmd_trigger_cold,
    "snapshot": cmd_snapshot,
    "trigger-resume": cmd_trigger_resume,
    "wait-cdp-internal": cmd_wait_cdp_internal,
    "teardown": cmd_teardown,
}


def serve() -> None:
    _log("daemon ready")
    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        if cmd == "quit":
            _terminate_current()
            return
        handler = HANDLERS.get(cmd)
        if handler is None:
            _reply(f"ERR unknown command: {cmd}")
            continue
        try:
            handler()
        except Exception as e:  # noqa: BLE001
            _reply(f"ERR {cmd}:{e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        print(__doc__)
        sys.exit(1)
