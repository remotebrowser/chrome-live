#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28", "websockets>=16.0", "boto3>=1.35"]
# ///
"""Drive one browser through a recordable session and push the result to object storage.

Claims a browser from flyfleet, opens a page, animates or scrolls it, then closes the tab.
Closing the tab is the part that matters: browser-trace only finalizes a recording when its
tab goes away, which is also why nothing can be pre-signed in advance — until then there is
no recording id to sign a key for.

So the flow is list, then upload:

    GET  /api/v1/browsers/{id}/trace/recordings            what finished
    POST /api/v1/browsers/{id}/recordings/{rec}/upload     flyfleet signs a PUT and
                                                           runs the curl in the container

The container holds no bucket credentials; this script needs them only for the optional
`--verify-object` step, which confirms the bytes actually landed.

CDP goes through flyfleet's proxy rather than straight at port 9222, so the session looks
like a real one. flycast needs the org WireGuard tunnel up:

    ./scripts/recording_smoke_test.py --browser-id rec-mine-1 --animated --scroll-seconds 10
    ./scripts/recording_smoke_test.py --fleet http://localhost:8300 --no-verify-object
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets

DEFAULT_FLEET = "http://flyfleet-dev.flycast"
DEFAULT_PAGE = "https://news.ycombinator.com"
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

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


def _client(timeout: float = 180.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))


async def claim_browser(fleet: str, browser_id: str) -> dict[str, Any]:
    async with _client() as client:
        response = await client.post(f"{fleet}/api/v1/browsers/{browser_id}")
        response.raise_for_status()
        return response.json()


async def list_recordings(fleet: str, browser_id: str) -> list[dict[str, Any]]:
    # flyfleet relays anything under /trace/ straight to browser-trace's own API.
    async with _client(60.0) as client:
        response = await client.get(f"{fleet}/api/v1/browsers/{browser_id}/trace/recordings")
        response.raise_for_status()
        return response.json().get("recordings", [])


async def upload_recording(fleet: str, browser_id: str, recording_id: str) -> dict[str, Any]:
    async with _client() as client:
        response = await client.post(f"{fleet}/api/v1/browsers/{browser_id}/recordings/{recording_id}/upload")
        if response.status_code >= 400:
            raise RuntimeError(f"upload failed: HTTP {response.status_code} {response.text[:400]}")
        return response.json()


async def stop_browser(fleet: str, browser_id: str) -> None:
    async with _client() as client:
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

        # Finalizes the recording — nothing to upload until this happens.
        await cdp.send("Target.closeTarget", {"targetId": target_id})
        print("  closed the tab")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def verify_object(env: dict[str, str], bucket: str, key: str) -> int | None:
    """HEAD the uploaded object and return its size, or None if it can't be checked."""
    import boto3
    from botocore.exceptions import ClientError

    if not (env.get("TIGRIS_ACCESS_KEY_ID") and env.get("TIGRIS_SECRET_ACCESS_KEY")):
        print("  no local TIGRIS_* credentials, skipping the bucket check")
        return None

    client = boto3.client(
        "s3",
        endpoint_url=env.get("TIGRIS_ENDPOINT_URL") or "https://t3.storage.dev",
        region_name=env.get("TIGRIS_REGION") or "auto",
        aws_access_key_id=env["TIGRIS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["TIGRIS_SECRET_ACCESS_KEY"],
    )
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        print(f"  object NOT in the bucket: {exc.response.get('Error', {}).get('Code')}")
        return None
    return head["ContentLength"]


def probe_video(fleet: str, browser_id: str, recording_id: str) -> float | None:
    """Download the MP4 through the passthrough and ask ffprobe how long it plays.

    A file that exists proves nothing: a regressed encoder timeline still writes an MP4, it
    just plays for the wrong length.
    """
    if shutil.which("ffprobe") is None:
        print("  no ffprobe on PATH, skipping the playback check")
        return None

    url = f"{fleet}/api/v1/browsers/{browser_id}/trace/recordings/{recording_id}/video"
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
        path = Path(handle.name)
        with httpx.stream("GET", url, timeout=httpx.Timeout(300.0, connect=10.0)) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                handle.write(chunk)

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"  ffprobe could not read the video: {exc!r}")
        return None
    finally:
        path.unlink(missing_ok=True)


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
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, type=Path, help="TIGRIS_* values for --verify-object")
    parser.add_argument(
        "--no-verify-object",
        dest="verify_object",
        action="store_false",
        help="skip the bucket HEAD (which needs local credentials)",
    )
    parser.add_argument("--stop", action="store_true", help="delete the browser when done")
    args = parser.parse_args()

    fleet = args.fleet.rstrip("/")
    ws_base = fleet.replace("https://", "wss://").replace("http://", "ws://")

    print(f"claiming browser {args.browser_id} from {fleet}")
    info = await claim_browser(fleet, args.browser_id)
    print(f"  app {info.get('hostname')} state={info.get('app_state')}")

    before = {item["recording_id"] for item in await list_recordings(fleet, args.browser_id)}

    page_url = f"data:text/html,{quote(ANIMATED_PAGE)}" if args.animated else args.url
    await record_a_page(
        f"{ws_base}/cdp/{args.browser_id}",
        page_url,
        args.scroll_seconds,
        args.scroll_step,
        animated=args.animated,
    )

    # Encoding runs after the tab is gone, so the sidecar lands a moment later.
    await asyncio.sleep(5)

    recordings = [item for item in await list_recordings(fleet, args.browser_id) if item["recording_id"] not in before]
    if not recordings:
        print("\nno new recording — a static page can produce zero frames; try --animated")
        return 1

    recording = recordings[0]
    recording_id = recording["recording_id"]
    print(
        f"recorded {recording_id}: {recording.get('size_bytes')} bytes, "
        f"{recording.get('duration_seconds')}s session → {recording.get('video_seconds')}s video"
    )

    print("uploading via a pre-signed PUT minted by flyfleet")
    result = await upload_recording(fleet, args.browser_id, recording_id)
    print(f"  stored at {result['bucket']}/{result['key']}")

    exit_code = 0
    if args.verify_object:
        size = verify_object(read_env_file(args.env_file), result["bucket"], result["key"])
        if size is not None:
            local = recording.get("size_bytes")
            match = "matches" if local == size else f"MISMATCH (local {local})"
            print(f"  object in bucket: {size} bytes, {match}")
            if local != size:
                exit_code = 1

    duration = probe_video(fleet, args.browser_id, recording_id)
    if duration is not None:
        session = recording.get("duration_seconds") or 0
        print(f"  ffprobe: {duration:.1f}s of video for a {session}s session")
        # Idle stretches are capped, so video is shorter than the session but not wildly so.
        if session and not (0.5 * session <= duration <= session + 2):
            print("  playback length is well off the session length — check the encoder timeline")
            exit_code = 1

    if args.stop:
        await stop_browser(fleet, args.browser_id)
        print(f"stopped browser {args.browser_id}")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
