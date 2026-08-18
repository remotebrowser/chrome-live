#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.35", "httpx>=0.28"]
# ///
"""Check that a set of Tigris credentials can actually read and write the bucket.

Independent of the container, which holds no credentials at all any more. This is the
flyfleet side (src/recordings.py): same client, same pre-signed PUT, minus the browser. It
round-trips a small object directly, then again through a pre-signed URL — the second one is
what actually ships, and it is the step that catches a Content-Type or endpoint mismatch.

Reads credentials from a key=value file, quotes stripped.

    ./scripts/check_tigris_creds.py                 # reads ./.env
    ./scripts/check_tigris_creds.py --env-file /path/to/other.env

Secrets are never printed; the access key id is shown masked.
"""

import argparse
import sys
from pathlib import Path

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

DEFAULT_ENDPOINT = "https://t3.storage.dev"
DEFAULT_REGION = "auto"
PROBE_KEY = "_creds_check/probe.txt"
PRESIGN_PROBE_KEY = "_creds_check/probe.mp4"

# Must match what flyfleet signs and what the container's curl sends, or S3 answers
# SignatureDoesNotMatch (flyfleet src/recordings.py CONTENT_TYPE).
VIDEO_CONTENT_TYPE = "video/mp4"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse key=value lines the way browser-trace's Config.from_file does."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def mask(secret: str) -> str:
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:6]}...{secret[-4:]} (len {len(secret)})"


def presigned_put(client, bucket: str) -> None:
    """Mint a PUT URL and use it, the way flyfleet and the container's curl do."""
    url = client.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": bucket, "Key": PRESIGN_PROBE_KEY, "ContentType": VIDEO_CONTENT_TYPE},
        ExpiresIn=3600,
    )
    response = httpx.put(url, content=b"not really an mp4", headers={"Content-Type": VIDEO_CONTENT_TYPE}, timeout=30)
    response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", type=Path, help="file holding TIGRIS_* values (default: .env)")
    parser.add_argument("--prefix", default="", help="also list objects under this key prefix")
    parser.add_argument("--endpoint", default="", help="override the S3 endpoint (default: the file's, or Tigris')")
    args = parser.parse_args()

    if not args.env_file.exists():
        print(f"no such file: {args.env_file}")
        return 2

    values = read_env_file(args.env_file)
    bucket = values.get("TIGRIS_BUCKET", "")
    access_key_id = values.get("TIGRIS_ACCESS_KEY_ID", "")
    secret_access_key = values.get("TIGRIS_SECRET_ACCESS_KEY", "")
    endpoint_url = args.endpoint or values.get("TIGRIS_ENDPOINT_URL") or DEFAULT_ENDPOINT
    region = values.get("TIGRIS_REGION") or DEFAULT_REGION

    missing = [
        name
        for name, value in (
            ("TIGRIS_BUCKET", bucket),
            ("TIGRIS_ACCESS_KEY_ID", access_key_id),
            ("TIGRIS_SECRET_ACCESS_KEY", secret_access_key),
        )
        if not value
    ]
    if missing:
        print(f"missing in {args.env_file}: {', '.join(missing)}")
        return 2

    print(f"bucket:     {bucket}")
    print(f"endpoint:   {endpoint_url} (region {region})")
    print(f"access key: {mask(access_key_id)}")
    print(f"secret:     {mask(secret_access_key)}")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        # Tigris only serves virtual-hosted-style addressing.
        config=BotoConfig(s3={"addressing_style": "virtual"}),
    )

    def step(label: str, call) -> bool:
        try:
            call()
        except ClientError as exc:
            error = exc.response.get("Error", {})
            print(f"  {label}: FAILED {error.get('Code')} — {error.get('Message')}")
            return False
        except Exception as exc:
            print(f"  {label}: FAILED {exc!r}")
            return False
        print(f"  {label}: ok")
        return True

    print("checks:")
    # Not fatal: a key scoped to a prefix is refused here but can still write its own objects,
    # which is all an upload needs.
    step("head bucket", lambda: client.head_bucket(Bucket=bucket))
    if not step(
        "put object",
        lambda: client.put_object(Bucket=bucket, Key=PROBE_KEY, Body=b"creds check", ContentType="text/plain"),
    ):
        return 1
    if not step("get object", lambda: client.get_object(Bucket=bucket, Key=PROBE_KEY)["Body"].read()):
        return 1
    step("delete object", lambda: client.delete_object(Bucket=bucket, Key=PROBE_KEY))

    if not step("presigned put", lambda: presigned_put(client, bucket)):
        return 1
    step("delete presigned object", lambda: client.delete_object(Bucket=bucket, Key=PRESIGN_PROBE_KEY))

    if args.prefix:
        listing = client.list_objects_v2(Bucket=bucket, Prefix=args.prefix, MaxKeys=20)
        contents = listing.get("Contents", [])
        print(f"objects under {args.prefix!r}: {len(contents)}")
        for item in contents:
            print(f"  {item['Key']} ({item['Size']} bytes)")

    print("\ncredentials work for read, write, and pre-signed PUT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
