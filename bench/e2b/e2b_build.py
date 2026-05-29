#!/usr/bin/env python3
# One-off: build the E2B template for chrome-live from the public image using
# Build System 2.0 (API-key only, no CLI access token needed). Prints the
# resulting template id to stdout as `TEMPLATE_ID=<id>` and streams build logs.
from __future__ import annotations

import os
import sys

from e2b import Template, wait_for_timeout

IMAGE = os.environ.get("E2B_IMAGE", "ghcr.io/remotebrowser/chrome-live:latest")
NAME = os.environ.get("E2B_TEMPLATE_NAME", "chrome-live-bench")
# The chrome-live s6 entrypoint needs PID 1 via `unshare --pid`, but E2B's
# start-command context lacks CAP_SYS_ADMIN so unshare is denied there. So the
# start command just keeps the microVM alive; the benchmark launches the real
# init via commands.run (which has full caps) right after create/resume.
START_CMD = os.environ.get("E2B_START_CMD", "sleep infinity")


def main() -> None:
    tmpl = Template().from_image(IMAGE).set_start_cmd(START_CMD, wait_for_timeout(1000))
    info = Template.build(
        tmpl,
        name=NAME,
        cpu_count=2,
        memory_mb=2048,
        on_build_logs=lambda e: print(e, flush=True),
    )
    tid = getattr(info, "template_id", None) or getattr(info, "templateID", None) or info
    print(f"TEMPLATE_ID={tid}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface build failures verbatim for the log
        print(f"BUILD_ERROR: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
