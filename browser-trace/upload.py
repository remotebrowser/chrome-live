"""Upload finalized recordings to S3-compatible object storage (Tigris).

This container owns the upload: it holds the bucket credentials, names the keys and
streams the files itself. Callers never see a recording id or an encoder state — they
only flip `upload_enabled` over HTTP (see `server.py`).

Two independent gates:

  * credentials — `TIGRIS_BUCKET` + both keys, templated into browser-trace.conf from
    the container's env. Missing any of them makes uploading impossible, so an image
    deployed without storage behaves exactly as it did before.
  * `UPLOAD_ENABLED` — the per-browser runtime toggle, default off. Recording itself is
    always on and always local; this only decides whether the finished MP4 leaves the
    container.
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

_config: "UploadConfig"


@dataclass
class UploadConfig:
    bucket: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    endpoint_url: str = "https://t3.storage.dev"
    region: str = "auto"
    # Runtime toggle + key namespace, both hot-reloadable and persisted in the conf.
    # browser_id is the *client's* id, which the container cannot know on its own:
    # SERVICE_NAME is the internal fly app name (chrome-<random>), so flyfleet has to
    # tell us. Without it, keys fall back to being flat.
    enabled: bool = False
    browser_id: str = ""

    @property
    def storage_configured(self) -> bool:
        return bool(self.bucket and self.access_key_id and self.secret_access_key)


_config = UploadConfig()


def configure(config: UploadConfig) -> None:
    """Apply a full config (credentials + runtime toggle), rebuilding the client."""
    global _client, _config
    credentials_changed = (
        config.bucket,
        config.access_key_id,
        config.secret_access_key,
        config.endpoint_url,
        config.region,
    ) != (
        _config.bucket,
        _config.access_key_id,
        _config.secret_access_key,
        _config.endpoint_url,
        _config.region,
    )
    _config = config

    if not credentials_changed and _client is not None:
        return

    if not config.storage_configured:
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


def set_runtime(enabled: bool | None = None, browser_id: str | None = None) -> None:
    """Update just the runtime knobs, leaving credentials and the client alone."""
    if enabled is not None:
        _config.enabled = enabled
    if browser_id is not None:
        _config.browser_id = browser_id
    print(
        f"[upload] uploads {'enabled' if enabled_for_recordings() else 'disabled'} "
        f"browser_id={_config.browser_id or '-'}",
        flush=True,
    )


def enabled_for_recordings() -> bool:
    return _config.enabled and _client is not None


def state() -> dict[str, Any]:
    return {
        "upload_enabled": _config.enabled,
        "browser_id": _config.browser_id,
        "storage_configured": _config.storage_configured,
        "bucket": _config.bucket,
    }


async def upload_recording(recording_id: str, video_path: Path) -> str | None:
    """Upload one recording's MP4. Returns the object key, or None on failure.

    The object keeps the filename recording.py gave it, namespaced by browser so a
    shared bucket stays attributable: `<browser_id>/<recording_id>.mp4`.

    Never raises: a failed upload leaves the local file in place and is only logged,
    because the caller is a background task at tab close with nowhere to report to.
    """
    if not enabled_for_recordings():
        return None

    key = f"{_config.browser_id}/{video_path.name}" if _config.browser_id else video_path.name
    try:
        size = video_path.stat().st_size
        await asyncio.to_thread(
            _client.upload_file,
            str(video_path),
            _config.bucket,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
    except Exception as e:
        print(f"[upload] failed {recording_id}: {e!r}", flush=True)
        return None

    print(f"[upload] stored {recording_id} ({size} bytes) at {key}", flush=True)
    return key
