"""Upload finalized recordings to S3-compatible object storage (Tigris).

This container owns the upload: it holds the bucket credentials, names the keys, and streams
the files itself. Callers only trigger it over HTTP — they never see a recording id, a key,
or an encoder state.

Credentials come from browser-trace.conf, which s6 templates from the container's env.
Uploading is disabled (a no-op) whenever the bucket or either key is missing, so an image
running without storage configured behaves exactly as it did before.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

# boto3 ships no type information, so everything crossing that boundary is Any.
_boto3: Any = boto3
_client: Any = None
_bucket: str = ""


@dataclass
class UploadConfig:
    bucket: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    endpoint_url: str = "https://t3.storage.dev"
    region: str = "auto"

    @property
    def enabled(self) -> bool:
        return bool(self.bucket and self.access_key_id and self.secret_access_key)


def configure(config: UploadConfig) -> None:
    """Build the storage client, or clear it if storage is not configured."""
    global _client, _bucket
    _bucket = config.bucket

    if not config.enabled:
        _client = None
        print("[upload] storage not configured, uploads disabled", flush=True)
        return

    _client = _boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        # Tigris only serves virtual-hosted-style addressing (bucket.t3.storage.dev).
        config=BotoConfig(s3={"addressing_style": "virtual"}),
    )
    print(f"[upload] storage configured: bucket={config.bucket}", flush=True)


def enabled() -> bool:
    return _client is not None


async def upload_recording(recording_id: str, video_path: Path) -> dict[str, Any]:
    """Upload one recording's MP4. Returns a result record.

    The object keeps the name recording.py already gave it — `<recording_id>.mp4` — so the
    bucket mirrors the recordings dir.

    Never raises: a failed upload is reported so the caller can move on to the next
    recording, and the local file is left in place for a retry.
    """
    if _client is None:
        return {"recording_id": recording_id, "status": "disabled"}

    key = video_path.name
    try:
        size = video_path.stat().st_size
        await asyncio.to_thread(
            _client.upload_file,
            str(video_path),
            _bucket,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
    except Exception as e:
        print(f"[upload] failed {recording_id}: {e!r}", flush=True)
        return {"recording_id": recording_id, "status": "failed", "error": repr(e)}

    print(f"[upload] stored {recording_id} ({size} bytes) at {key}", flush=True)
    return {
        "recording_id": recording_id,
        "status": "uploaded",
        "key": key,
        "bytes": size,
    }
