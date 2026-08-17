#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28", "websockets>=16.0"]
# ///
"""Drive one browser through a recordable session, so recording + upload can be checked.

Claims a browser from flyfleet, opens a page, scrolls it for a few seconds, then closes the
tab. Closing the tab is the part that matters: browser-trace only finalizes (and uploads) a
recording when its tab goes away.

CDP goes through flyfleet's proxy rather than straight at the browser's port 9222, because
the proxy is what triggers `prepare_browser_trace` — that is what switches uploads on for
this browser and tells it which id to file the object under.

Run it against a fleet you can reach; flycast needs the org WireGuard tunnel up:

    ./scripts/recording_smoke_test.py
    ./scripts/recording_smoke_test.py --fleet http://localhost:8300 --browser-id rec-test-1
"""

import argparse
import asyncio
import json
import sys
from typing import Any
from urllib.parse import quote

import httpx
import websockets

DEFAULT_FLEET = "http://flyfleet-dev.flycast"
DEFAULT_PAGE = "https://news.ycombinator.com"

# Chrome's screencast emits a frame when the page repaints, so a static page yields almost
# nothing to record. This one never stops repainting, and being a data: URL it needs no
# network — the browser's tinyproxy/hblock setup can't block it and no consent banner can
# cover it, which keeps frame counts comparable between runs.
ANIMATED_PAGE = """<!doctype html>
<title>recording smoke test</title>
<style>
  body { margin: 0; height: 100vh; display: grid; place-items: center;
         background: linear-gradient(120deg, #1a1a2e, #16213e); color: #eee;
         font: 600 5vw/1.4 system-ui, sans-serif; }
  .bar { width: 60vw; height: 6vh; border-radius: 999px; background: #eee;
         animation: slide 1.4s ease-in-out infinite alternate; }
  @keyframes slide { from { transform: translateX(-20vw) scaleX(.4); }
                     to   { transform: translateX(20vw) scaleX(1); } }
</style>
<div>
  <div class="bar"></div>
  <div id="n">0</div>
</div>
<script>
  let n = 0;
  const el = document.getElementById('n');
  (function tick() { el.textContent = ++n; requestAnimationFrame(tick); })();
</script>"""


class Cdp:
    """Minimal CDP client: send a command, skip events, return the matching reply."""

    def __init__(self, socket: Any):
        self.socket = socket
        self._next_id = 0

    async def send(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> Any:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        await self.socket.send(json.dumps(message))

        while True:
            reply = json.loads(await self.socket.recv())
            if reply.get("id") != self._next_id:
                continue  # an event, or a reply to something we already returned
            if "error" in reply:
                raise RuntimeError(f"{method} failed: {reply['error']}")
            return reply.get("result", {})


async def claim_browser(fleet: str, browser_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        response = await client.post(f"{fleet}/api/v1/browsers/{browser_id}")
        response.raise_for_status()
        return response.json()


async def read_upload_config(fleet: str, browser_id: str) -> dict[str, Any]:
    # flyfleet relays anything under /trace/ straight to browser-trace's own API.
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        response = await client.get(f"{fleet}/api/v1/browsers/{browser_id}/trace/recordings/config")
        response.raise_for_status()
        return response.json()


async def stop_browser(fleet: str, browser_id: str) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        response = await client.delete(f"{fleet}/api/v1/browsers/{browser_id}")
        response.raise_for_status()


async def record_a_page(ws_url: str, page_url: str, scroll_seconds: float, step: int, animated: bool) -> None:
    # The machine may be cold: fly-proxy has to resume it before the CDP handshake completes.
    async with websockets.connect(ws_url, open_timeout=120, ping_interval=20, max_size=None) as socket:
        cdp = Cdp(socket)

        version = await cdp.send("Browser.getVersion")
        print(f"  connected to {version.get('product', 'unknown')}")

        target = await cdp.send("Target.createTarget", {"url": page_url})
        # flyfleet's proxy hands back `<browser_id>@<targetId>` (see patch_cdp_target in its
        # src/cdp.py) and only strips that prefix again for Target.getTargetInfo, so attach and
        # close have to be given the bare id.
        namespaced_id = target["targetId"]
        target_id = namespaced_id.split("@", 1)[-1]
        session = await cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = session["sessionId"]
        label = page_url if len(page_url) < 80 else f"{page_url[:60]}… ({len(page_url)} chars)"
        print(f"  opened {label} (target {namespaced_id})")

        await cdp.send("Page.enable", session_id=session_id)
        await asyncio.sleep(2)  # let the page paint before moving, so the frames differ

        loop = asyncio.get_running_loop()
        deadline = loop.time() + scroll_seconds
        if animated:
            print(f"  letting it animate for {scroll_seconds:.0f}s")
            await asyncio.sleep(scroll_seconds)
        else:
            print(f"  scrolling for {scroll_seconds:.0f}s")
            while loop.time() < deadline:
                # A real wheel event, not window.scrollBy: it drives the compositor the way a
                # user would, which is what makes the page repaint and emit screencast frames.
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": 300, "y": 300, "deltaX": 0, "deltaY": step},
                    session_id=session_id,
                )
                await asyncio.sleep(0.2)

        # Finalizes the recording, and queues the upload if it is switched on for this browser.
        await cdp.send("Target.closeTarget", {"targetId": target_id})
        print("  closed the tab")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fleet", default=DEFAULT_FLEET, help=f"flyfleet base URL (default: {DEFAULT_FLEET})")
    parser.add_argument("--browser-id", default="rec-smoke-1", help="browser id to claim (default: rec-smoke-1)")
    parser.add_argument("--url", default=DEFAULT_PAGE, help=f"page to record (default: {DEFAULT_PAGE})")
    parser.add_argument(
        "--animated",
        action="store_true",
        help="record a built-in always-animating page instead of --url (no network, no scrolling)",
    )
    parser.add_argument("--scroll-seconds", type=float, default=5.0, help="how long to record (default: 5)")
    parser.add_argument("--scroll-step", type=int, default=120, help="pixels per wheel event (default: 120)")
    parser.add_argument("--stop", action="store_true", help="delete the browser when done")
    args = parser.parse_args()

    fleet = args.fleet.rstrip("/")
    ws_base = fleet.replace("https://", "wss://").replace("http://", "ws://")

    print(f"claiming browser {args.browser_id} from {fleet}")
    info = await claim_browser(fleet, args.browser_id)
    print(f"  app {info.get('hostname')} state={info.get('app_state')}")

    page_url = f"data:text/html,{quote(ANIMATED_PAGE)}" if args.animated else args.url
    await record_a_page(
        f"{ws_base}/cdp/{args.browser_id}",
        page_url,
        args.scroll_seconds,
        args.scroll_step,
        animated=args.animated,
    )

    # Uploads are detached from tab close, so the sidecar's upload_key lands a moment later.
    await asyncio.sleep(5)

    config = await read_upload_config(fleet, args.browser_id)
    print(f"upload config: {json.dumps(config)}")

    if args.stop:
        await stop_browser(fleet, args.browser_id)
        print(f"stopped browser {args.browser_id}")

    # Both gates have to be open, and upload_enabled alone does not mean anything was sent.
    if not config.get("storage_configured"):
        print("\nno storage credentials on this browser — it recorded locally and uploaded nothing")
        return 1
    if not config.get("upload_enabled"):
        print("\nuploads were off for this browser — set RECORDING_UPLOAD_ENABLED, or POST the toggle")
        return 1

    key_prefix = config.get("browser_id") or "(flat, no browser_id)"
    print(f"\nlook for the object under {config.get('bucket')}/{key_prefix}/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
