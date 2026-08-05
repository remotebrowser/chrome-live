"""HTTP server for retrieving browser-session recordings.

Recordings are written to disk by `recording.py` (an `<id>.mp4` video plus an
`<id>.json` metadata sidecar). Nothing served them until now; this aiohttp app
exposes a small read-only API over the same recordings dir so the videos can be
listed and downloaded.

Endpoints:
    GET  /health                    Liveness probe → {"status": "ok"}.
    GET  /recordings                JSON array of recording metadata.
    GET  /recordings/{id}/video     The MP4 (streamed, supports Range requests so
                                    browsers can seek).
    POST /recordings/upload         Store every finalized recording in object storage;
                                    see `upload.py`.
    GET  /traffic                   Live byte totals per tab and per host, from
                                   `traffic.py`. `?hosts=N` caps the process-wide
                                   host list (default 20).

The recordings dir is read from `recording.get_recordings_dir()` on every
request rather than captured at startup, so it tracks config hot-reloads.
"""

import json
from pathlib import Path

from aiohttp import web

import recording as rec
import traffic
import upload

_DEFAULT_HOST_LIMIT = 20
_MAX_HOST_LIMIT = 1000


def _list_recordings() -> list[dict]:
    """Return metadata for every recording in the recordings dir, newest first.

    Built from the `.mp4` files present (a recording only has a playable video
    once finalized). Merges the `.json` sidecar when present; falls back to a
    minimal record derived from the filename otherwise.
    """
    recordings_dir = rec.get_recordings_dir()
    items: list[dict] = []
    if not recordings_dir.exists():
        return items

    for mp4 in sorted(recordings_dir.glob("*.mp4"), reverse=True):
        recording_id = mp4.stem
        meta_path = recordings_dir / f"{recording_id}.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
        else:
            meta = {}
        meta.setdefault("recording_id", recording_id)
        meta.setdefault("storage_key", mp4.name)
        try:
            meta["size_bytes"] = mp4.stat().st_size
        except OSError:
            meta["size_bytes"] = None
        meta["video_url"] = f"/recordings/{recording_id}/video"
        items.append(meta)
    return items


def _safe_recording_path(recording_id: str, suffix: str) -> Path | None:
    """Resolve `<recordings_dir>/<recording_id><suffix>`, rejecting traversal.

    Returns None if `recording_id` escapes the recordings dir (e.g. contains
    `..` or a slash) or the file does not exist.
    """
    recordings_dir = rec.get_recordings_dir()
    candidate = (recordings_dir / f"{recording_id}{suffix}").resolve()
    try:
        candidate.relative_to(recordings_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_list(request: web.Request) -> web.Response:
    return web.json_response({"recordings": _list_recordings()})


async def handle_video(request: web.Request) -> web.StreamResponse:
    recording_id = request.match_info["recording_id"]
    video_path = _safe_recording_path(recording_id, ".mp4")
    if video_path is None:
        raise web.HTTPNotFound(text=f"no video for {recording_id!r}")
    # FileResponse handles Range requests, Content-Length, and streaming so the
    # browser <video> element can seek without downloading the whole file.
    return web.FileResponse(
        video_path,
        headers={"Content-Type": "video/mp4"},
    )


async def handle_upload(request: web.Request) -> web.Response:
    """Store every finalized recording on disk in object storage.

    Takes no parameters, and re-uploads unconditionally: keys are the local filenames, so a
    repeat call overwrites rather than duplicating. A recording only appears on disk once its
    tab has closed and ffmpeg has encoded it, so one triggered immediately after a tab closes
    can race the encoder and miss it — the next call picks it up.
    """
    if not upload.enabled():
        # Not an error: an image running without storage configured simply has nowhere
        # to put recordings.
        return web.json_response({"status": "disabled", "uploaded": []}, status=200)

    results: list[dict[str, object]] = []
    for mp4 in sorted(rec.get_recordings_dir().glob("*.mp4")):
        results.append(await upload.upload_recording(mp4.stem, mp4))

    failed = [r for r in results if r.get("status") == "failed"]
    return web.json_response(
        {
            "status": "ok" if not failed else "partial",
            "uploaded": [r for r in results if r.get("status") == "uploaded"],
            "failed": failed,
        },
        # 207: some recordings are stored, some aren't, and the caller may want to retry.
        status=200 if not failed else 207,
    )


async def handle_traffic(request: web.Request) -> web.Response:
    raw = request.query.get("hosts")
    if raw is None:
        host_limit = _DEFAULT_HOST_LIMIT
    else:
        try:
            host_limit = int(raw)
        except ValueError:
            raise web.HTTPBadRequest(text=f"hosts must be an integer, got {raw!r}")
        host_limit = max(0, min(host_limit, _MAX_HOST_LIMIT))
    return web.json_response(traffic.snapshot(host_limit=host_limit))


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/health", handle_health),
            web.get("/recordings", handle_list),
            web.get("/recordings/{recording_id}/video", handle_video),
            web.post("/recordings/upload", handle_upload),
            web.get("/traffic", handle_traffic),
        ]
    )
    return app


async def start_server(host: str, port: int) -> web.AppRunner:
    """Start the aiohttp server and return its runner (for later cleanup).

    Runs inside the caller's already-running asyncio event loop; the returned
    runner must be `.cleanup()`d on shutdown.
    """
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
