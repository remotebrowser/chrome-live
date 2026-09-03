"""A small JPEG of what the browser is showing right now."""

import asyncio
import base64
import logging
import time
from typing import NamedTuple

import cdp

_logger = logging.getLogger("browser-trace.thumbnail")

_TARGET_WIDTH = 320
_JPEG_QUALITY = 70

CACHE_SECONDS = 5.0


class ThumbnailError(Exception):
    """No thumbnail can be produced right now."""


class _Source(NamedTuple):
    conn: cdp.Connection
    # Held by reference: `main` mutates this as tabs open and close.
    sessions: dict[str, dict]


class _Cached(NamedTuple):
    session_id: str
    expires_at: float  # monotonic
    jpeg: bytes


class Thumbnailer:
    """Screenshots the frontmost tab of one CDP connection, with a short cache."""

    def __init__(self) -> None:
        self._source: _Source | None = None
        self._cache: _Cached | None = None
        self._lock = asyncio.Lock()

    def attach(self, conn: cdp.Connection, sessions: dict[str, dict]) -> None:
        """Serve thumbnails from `conn` until the next `detach`."""
        self._source = _Source(conn, sessions)
        self._cache = None

    def detach(self) -> None:
        """Drop the connection. A cached frame goes with it: session ids do not
        survive a reconnect, so it could never be served again anyway."""
        self._source = None
        self._cache = None

    async def capture(self) -> bytes:
        """JPEG bytes of the current page, at most `CACHE_SECONDS` old."""
        source = self._source
        if source is None:
            raise ThumbnailError("not connected to Chrome")

        session_id = _frontmost_session_id(source.sessions)

        async with self._lock:
            cached = self._cache
            # Session is part of the key: switching tabs must not serve the old one.
            if (
                cached
                and cached.session_id == session_id
                and cached.expires_at > time.monotonic()
            ):
                return cached.jpeg

            try:
                jpeg = await _screenshot(source.conn, session_id)
            except cdp.CdpError as e:
                _logger.warning(f"[thumbnail] capture failed for {session_id[:8]}: {e}")
                raise ThumbnailError(f"screenshot failed: {e}") from e

            # Reconnected while the screenshot was in flight: the frame belongs
            # to a connection nobody can ask for any more, so serve it once
            # without caching it against the new one's session ids.
            if self._source is source:
                self._cache = _Cached(session_id, time.monotonic() + CACHE_SECONDS, jpeg)
            return jpeg


# One browser, one connection at a time. Module-level so the HTTP server, which
# starts before CDP is up and survives reconnects, has a stable handle.
thumbnailer = Thumbnailer()


def _frontmost_session_id(sessions: dict[str, dict]) -> str:
    """The most recently attached page target.

    Chrome-live runs one tab, and a newly opened tab is the one in front, so
    insertion order into `sessions` is the best available stand-in for focus —
    nothing in this service tracks which tab Chrome considers active.
    """
    if not sessions:
        raise ThumbnailError("no open tabs")
    return next(reversed(sessions))


async def _screenshot(conn: cdp.Connection, session_id: str) -> bytes:
    metrics = await conn.call("Page.getLayoutMetrics", session_id=session_id)
    # cssVisualViewport is the visible area in CSS pixels, so it already accounts
    # for the 125% desktop scaling chrome-live runs Chrome at.
    viewport = metrics.get("cssVisualViewport") or metrics.get("cssLayoutViewport") or {}
    width = viewport.get("clientWidth") or 0
    height = viewport.get("clientHeight") or 0
    if not width or not height:
        raise ThumbnailError("Chrome reported an empty viewport")

    scale = _TARGET_WIDTH / width
    result = await conn.call(
        "Page.captureScreenshot",
        {
            "format": "jpeg",
            "quality": _JPEG_QUALITY,
            "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": scale},
        },
        session_id=session_id,
    )
    data = result.get("data")
    if not data:
        raise ThumbnailError("Chrome returned an empty screenshot")
    return base64.b64decode(data)
