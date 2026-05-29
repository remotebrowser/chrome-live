#!/usr/bin/env python3
# Persistent controller for the Daytona bench hooks.
#
# Runs as a long-lived coprocess so the heavy `daytona` SDK import + auth happens once,
# not on every timed bp_trigger (which would add interpreter-boot noise to each sample).
# Reads one command per line on stdin, writes exactly one response line per command on
# stdout ("OK ..." / "ERR ..."), flushing each time. Readiness polling is done in bash
# via curl against the preview URLs, so the timed path never re-enters Python.
#
# Commands:
#   create <image> <cpu> <mem> <disk>   create sandbox (public), wait first boot -> OK <id>
#   adopt  <id>                         attach to an existing sandbox id
#   installfwd                          install noVNC :80 -> :8080 forwarder (s6 + now)
#   info   <outfile>                    probe arch/storage, write to outfile -> OK
#   preview <port>                      -> OK <url> <token>
#   state                               -> OK <state>
#   stop                                stop (blocks until stopped) -> OK
#   archive                             archive (must be stopped) -> OK
#   start                               start (blocks until started) -> OK
#   wait-cdp                            sandbox.process.exec(curl 127.0.0.1:9222/json/version)
#                                       -> OK if exit_code==0, ERR otherwise (one-shot probe)
#   delete                              delete -> OK
#   quit                                exit

import sys
import os

from daytona import Daytona, CreateSandboxFromImageParams, Resources, Image

STATE_FILE = os.environ.get("BENCH_STATE", os.path.join(os.path.dirname(__file__), ".daytona_state"))

# Daytona overrides the image ENTRYPOINT with its daemon, so we must declare the entrypoint
# that brings up the s6 stack. s6-overlay refuses to run unless it is PID 1, so we give it a
# nested PID namespace via unshare (the sandbox has CAP_SYS_ADMIN). This mirrors chrome-live's
# own Fly.io code path in start-init.sh.
ENTRYPOINT = ["/usr/bin/unshare", "--pid", "--fork", "--mount-proc", "/init"]

# noVNC listens on :80, which is outside Daytona's previewable 3000-9999 range. Bake an s6
# longrun that forwards :8080 -> :80 so it comes up natively on every boot/resume and :8080
# can be previewed. s6 compiles s6-rc.d at every container start, so adding the service dir +
# the user-bundle contents entry is enough.
FWD_BUILD = (
    "mkdir -p /etc/s6-overlay/s6-rc.d/novnc-fwd /etc/s6-overlay/s6-rc.d/user/contents.d && "
    "printf 'longrun\\n' > /etc/s6-overlay/s6-rc.d/novnc-fwd/type && "
    "printf '#!/command/with-contenv sh\\nexec socat TCP-LISTEN:8080,fork,reuseaddr "
    "TCP:127.0.0.1:80\\n' > /etc/s6-overlay/s6-rc.d/novnc-fwd/run && "
    "chmod +x /etc/s6-overlay/s6-rc.d/novnc-fwd/run && "
    "touch /etc/s6-overlay/s6-rc.d/user/contents.d/novnc-fwd"
)

INFO_CMD = r"""
echo "== uname -m =="; uname -m
echo "== nproc =="; nproc
echo "== mem (MiB) =="; free -m 2>/dev/null | awk '/Mem:/{print $2}'
echo "== df -T / =="; df -T / 2>/dev/null
echo "== rootfs mount (/proc/mounts) =="; awk '$2=="/"{print}' /proc/mounts
echo "== lsblk =="; lsblk -o NAME,ROTA,TYPE,MOUNTPOINT 2>/dev/null || echo "no lsblk"
echo "== /sys rotational =="; for d in /sys/block/*/queue/rotational; do echo "$d=$(cat $d 2>/dev/null)"; done 2>/dev/null
"""


def save_id(sid: str) -> None:
    with open(STATE_FILE, "w") as f:
        f.write(sid)


def main() -> None:
    daytona = Daytona()
    sandbox = None

    def reply(s: str) -> None:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        parts = line.strip().split()
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "create":
                image, cpu, mem, disk = args[0], int(args[1]), int(args[2]), int(args[3])
                img = Image.base(image).run_commands(FWD_BUILD).entrypoint(ENTRYPOINT)
                params = CreateSandboxFromImageParams(
                    image=img,
                    resources=Resources(cpu=cpu, memory=mem, disk=disk),
                    public=True,
                    auto_stop_interval=0,
                )
                sandbox = daytona.create(params, timeout=400)
                save_id(sandbox.id)
                reply(f"OK {sandbox.id}")
            elif cmd == "adopt":
                sandbox = daytona.get(args[0])
                reply(f"OK {sandbox.id}")
            elif cmd == "installfwd":
                # noVNC :8080 forwarder is baked into the image now; nothing to do at runtime.
                reply("OK baked")
            elif cmd == "info":
                r = sandbox.process.exec(INFO_CMD, timeout=60)
                with open(args[0], "w") as f:
                    f.write(r.result)
                reply(f"OK {r.exit_code}")
            elif cmd == "preview":
                pl = sandbox.get_preview_link(int(args[0]))
                reply(f"OK {pl.url} {pl.token}")
            elif cmd == "state":
                sandbox.refresh_data()
                reply(f"OK {getattr(sandbox, 'state', '?')}")
            elif cmd == "stop":
                daytona.stop(sandbox)
                reply("OK")
            elif cmd == "archive":
                sandbox.archive()
                reply("OK")
            elif cmd == "start":
                daytona.start(sandbox)
                reply("OK")
            elif cmd == "wait-cdp":
                # Blocking internal CDP probe. Runs a tight curl loop INSIDE the
                # sandbox and only returns when curl succeeds. This pays the SDK
                # process.exec round-trip cost once per timed run, not once per
                # harness poll iteration. Inner loop caps at ~30s to avoid
                # hanging if Chrome never comes up.
                script = (
                    "i=0; "
                    "while ! curl -fsS -o /dev/null --max-time 1 "
                    "http://127.0.0.1:9222/json/version; do "
                    "i=$((i+1)); [ $i -gt 600 ] && exit 1; sleep 0.05; "
                    "done"
                )
                r = sandbox.process.exec(script, timeout=45)
                if r.exit_code == 0:
                    reply("OK")
                else:
                    reply(f"ERR exit={r.exit_code}")
            elif cmd == "delete":
                daytona.delete(sandbox)
                reply("OK")
            elif cmd == "quit":
                reply("OK bye")
                return
            else:
                reply(f"ERR unknown cmd {cmd}")
        except Exception as e:  # noqa: BLE001 - relay any SDK error to the bash side
            reply(f"ERR {type(e).__name__}: {str(e)[:300]}".replace("\n", " "))


if __name__ == "__main__":
    main()
