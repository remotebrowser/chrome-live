#!/usr/bin/env python3
# E2B sandbox lifecycle driver for the chrome-live startup benchmark.
#
# The bp_* hooks in e2b.sh shell out to this script because sandbox lifecycle
# (create / pause / resume / kill / port exposure) is only available through the
# E2B SDK. Each invocation is a fresh process, so the live sandbox id and its
# public noVNC URL are persisted to a state file the bash hooks read.
#
# Two E2B-specific facts shape this driver (see bench/README.md "E2B notes"):
#   1. The chrome-live image's s6 entrypoint needs PID 1, which it gets via
#      `unshare --pid`. E2B's *start-command* context lacks CAP_SYS_ADMIN, so
#      unshare is denied there; but `commands.run` (root) has full caps. So init
#      is launched AFTER create/resume via commands.run, not as a start command.
#   2. Chrome's DevTools HTTP endpoint rejects a non-localhost Host header, and
#      E2B's host-based proxy must forward `<port>-<id>.e2b.app` as Host, so CDP
#      is unreachable through the public proxy (500). CDP readiness is therefore
#      measured intra-sandbox (curl 127.0.0.1:9222). noVNC :80 has no such check
#      and is reached through the public proxy.
#
# Subcommands:
#   setup    create baseline sandbox, launch init, wait until CDP answers
#   cold     kill the current sandbox so `trigger` spawns a fresh one
#   paused   beta_pause the current (running) sandbox
#   trigger  TIMED action: cold -> create + launch init; resume -> connect
#   wait_cdp block (one round trip) until intra-sandbox CDP answers; exit 0/1
#   teardown kill the current sandbox
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from e2b import Sandbox

STATE_PATH = Path(os.environ.get("E2B_STATE", Path(__file__).with_name(".e2b_state")))
TEMPLATE = os.environ.get("E2B_TEMPLATE", "")
SANDBOX_TIMEOUT = int(os.environ.get("E2B_SANDBOX_TIMEOUT", "900"))
READY_TIMEOUT = int(float(os.environ.get("READY_TIMEOUT", "180")))
CDP_PORT = 9222
NOVNC_PORT = 80
# Launched with commands.run(background=True): the SDK ties the process lifetime
# to the sandbox (not to a shell that exits), so init survives reliably. A bare
# `nohup ... &` is racy — E2B sometimes reaps the child when the shell returns.
INIT_CMD = "/usr/bin/unshare --pid --fork --mount-proc /init"
# curl that never exits non-zero (so commands.run doesn't raise); prints status.
CDP_PROBE = "curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://localhost:9222/json/version || true"


def _load() -> dict[str, str]:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def _save(state: dict[str, str]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _record(sbx: Sandbox) -> dict[str, str]:
    state = {"sandbox_id": sbx.sandbox_id, "novnc_url": f"https://{sbx.get_host(NOVNC_PORT)}"}
    _save(state)
    return state


def _connect() -> Sandbox:
    sid = _load().get("sandbox_id")
    if not sid:
        sys.exit("no current sandbox id in state")
    return Sandbox.connect(sid, timeout=SANDBOX_TIMEOUT)


def _launch_init(sbx: Sandbox) -> None:
    sbx.commands.run(INIT_CMD, user="root", background=True)


def _wait_cdp_in(sbx: Sandbox) -> bool:
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        try:
            r = sbx.commands.run(CDP_PROBE, user="root", timeout=10, request_timeout=10)
            if r.stdout.strip() == "200":
                return True
        except Exception:
            pass  # transient envd/RPC hiccup; keep polling until the deadline
        time.sleep(0.2)
    return False


def cmd_setup() -> None:
    if not TEMPLATE:
        sys.exit("E2B_TEMPLATE not set")
    sbx = Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT)
    _launch_init(sbx)
    state = _record(sbx)
    if not _wait_cdp_in(sbx):
        sys.exit("baseline sandbox never reached CDP-ready")
    print(f"sandbox={state['sandbox_id']} novnc={state['novnc_url']}")


def cmd_cold() -> None:
    sid = _load().get("sandbox_id")
    if sid:
        try:
            Sandbox.connect(sid, timeout=SANDBOX_TIMEOUT).kill()
        except Exception:
            pass
    state = _load()
    state.pop("sandbox_id", None)
    _save(state)


def cmd_paused() -> None:
    _connect().beta_pause()


def cmd_trigger() -> None:
    mode = os.environ.get("MODE", "cold")
    if mode == "resume":
        sbx = _connect()  # auto-resumes a paused sandbox
    else:
        if not TEMPLATE:
            sys.exit("E2B_TEMPLATE not set")
        sbx = Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT)
        _launch_init(sbx)
    _record(sbx)


def cmd_wait_cdp() -> None:
    sys.exit(0 if _wait_cdp_in(_connect()) else 1)


def cmd_teardown() -> None:
    sid = _load().get("sandbox_id")
    if sid:
        try:
            Sandbox.connect(sid, timeout=SANDBOX_TIMEOUT).kill()
        except Exception:
            pass
    if STATE_PATH.exists():
        STATE_PATH.unlink()


def main() -> None:
    cmds = {
        "setup": cmd_setup,
        "cold": cmd_cold,
        "paused": cmd_paused,
        "trigger": cmd_trigger,
        "wait_cdp": cmd_wait_cdp,
        "teardown": cmd_teardown,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(cmds)}}}")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
