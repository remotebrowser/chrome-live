"""Durable edits to browser-trace's key=value config file.

The conf is templated from the container's env at boot and re-read by a ~1s
watcher, so the file — not process memory — is the only place a setting survives
a browser-trace restart or a machine stop/start. Settings toggled at runtime
(`UPLOAD_ENABLED`, `BROWSER_ID`) are written back here for that reason.

Writes go through a temp file in the same directory plus `os.replace`, so the
watcher can never read a half-written conf.
"""

import os
import tempfile


_path: str = ""


def configure(path: str) -> None:
    global _path
    _path = path


def path() -> str:
    return _path


def set_values(values: dict[str, str]) -> None:
    """Set each key in the conf, replacing existing lines and appending new ones.

    Raises OSError if the conf can't be written; callers persist before applying
    so a failed write leaves the running config alone.
    """
    if not _path:
        raise OSError("config file path not configured")

    try:
        with open(_path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())

    directory = os.path.dirname(os.path.abspath(_path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".browser-trace.conf.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, _path)
    except Exception:
        os.unlink(tmp)
        raise
