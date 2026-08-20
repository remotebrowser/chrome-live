"""JSONL sink for the CDP events `main.py` emits, read back by `GET /logs`."""

import json
from pathlib import Path

_DEFAULT_PATH = Path("/tmp/browser-trace-events.jsonl")

_path: Path = _DEFAULT_PATH


def configure(path: Path | None) -> None:
    global _path
    _path = path or _DEFAULT_PATH


def get_path() -> Path:
    return _path


def record(event: dict) -> None:
    """Append one event. Best-effort: a failure here must never take down the
    CDP receive loop, so it is reported and swallowed."""
    line = json.dumps(event, default=str)
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        with open(_path, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[event-log] failed to write {_path}: {e}", flush=True)


def read_all() -> list[dict]:
    """Every event in the file, in write order. A missing file is empty, and a
    line that doesn't parse is skipped — the last line can be torn if the
    process died mid-write."""
    try:
        with open(_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        print(f"[event-log] failed to read {_path}: {e}", flush=True)
        return []

    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
