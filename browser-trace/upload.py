"""PUT one finalized recording at a pre-signed URL.

This container holds no bucket credentials. The caller signs a PUT for the key it wants
(flyfleet `src/recordings.py`) and passes only the URL in; this streams the file at it.

Why a subcommand rather than a bare `curl`: the caller is a one-shot machine exec with no
queue behind it, so a transient 5xx would surface as a failed upload it has to notice and
repeat. Retrying here keeps that in the container.
"""

import asyncio
from pathlib import Path

import aiohttp

# recording.py's RECORDING_DIR default, which chrome-live never overrides.
RECORDINGS_DIR = Path("/tmp/recordings")

# Must equal the ContentType the URL was signed with, or S3 answers SignatureDoesNotMatch
# (flyfleet src/recordings.py CONTENT_TYPE).
CONTENT_TYPE = "video/mp4"

_ATTEMPTS = 3
_BACKOFF_SECONDS = 2.0
_TIMEOUT = aiohttp.ClientTimeout(total=600, connect=15)


class UploadError(Exception):
    """Fatal: the upload cannot succeed by retrying."""


def newest_recording() -> Path:
    """The most recently finalized recording. Ids sort chronologically, so name order is time
    order (recording.py builds them from a UTC timestamp)."""
    videos = sorted(RECORDINGS_DIR.glob("*.mp4"), reverse=True)
    if not videos:
        raise UploadError(f"no recordings in {RECORDINGS_DIR}")
    return videos[0]


def _retriable(status: int) -> bool:
    """A 4xx means the URL itself is wrong — expired, mis-signed, wrong Content-Type."""
    return status == 429 or status >= 500


async def put_recording(path: Path, url: str) -> int:
    """Stream `path` to `url` with a PUT. Returns the byte count, or raises UploadError.

    Content-Length is set explicitly: S3 rejects a chunked body on a pre-signed PUT, and
    aiohttp would otherwise pick chunked for a file object.
    """
    size = path.stat().st_size
    if size == 0:
        raise UploadError(f"{path} is empty")

    last = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                # Reopened per attempt so a retry restarts at byte zero.
                with path.open("rb") as body:
                    async with session.put(
                        url,
                        data=body,
                        headers={"Content-Type": CONTENT_TYPE, "Content-Length": str(size)},
                    ) as response:
                        if response.status < 300:
                            return size
                        detail = (await response.text())[:300].replace("\n", " ")
                        last = f"HTTP {response.status}: {detail}"
                        if not _retriable(response.status):
                            raise UploadError(last)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = repr(exc)

        if attempt < _ATTEMPTS:
            # stdout, not stderr: the caller reads any stderr output as a failed exec, so a
            # run that recovers on attempt 2 must stay silent there.
            print(f"[upload] attempt {attempt}/{_ATTEMPTS} failed ({last}), retrying", flush=True)
            await asyncio.sleep(_BACKOFF_SECONDS * attempt)

    raise UploadError(f"giving up after {_ATTEMPTS} attempts — {last}")
