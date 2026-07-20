#!/usr/bin/env python3
"""Browser trace — monitors tab opens and navigations via CDP, and
forwards tinyproxy log lines from stdin to Logfire when invoked in
`tinyproxy` mode."""

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import logfire
import websockets

import recording as rec


@dataclass
class Config:
    service_name: str = "browser-trace"
    environment: str = "local"
    logfire_token: str = ""
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    traceparent: str | None = None
    # Tinyproxy-mode tee threshold. Mirrors `logfire`'s `min_log_level`
    # console semantics: lines whose mapped Logfire severity is below this
    # are still sent to Logfire (so the UI sees them) but are not tee'd to
    # stdout / Fly logs. Hot-reloadable via the config-file watcher.
    log_level: str = "INFO"
    # Recording
    recording_dir: str = ""  # defaults to /tmp/recordings

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """Load config from a key=value file. Returns defaults if file missing."""
        values: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        values[key.strip()] = value.strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        tp = values.get("LOGFIRE_TRACEPARENT", "")
        return cls(
            service_name=values.get("SERVICE_NAME", "browser-trace"),
            environment=values.get("ENVIRONMENT", "local"),
            logfire_token=values.get("LOGFIRE_TOKEN", ""),
            cdp_host=values.get("CDP_HOST", "127.0.0.1"),
            cdp_port=int(values.get("CDP_PORT", "9222")),
            traceparent=tp if tp else None,
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
            recording_dir=values.get("RECORDING_DIR", ""),
        )


# Per-session state: maps sessionId -> {target_id, main_frame_id, pending}
sessions: dict[str, dict] = {}

# Reverse map: target_id -> session_id (for recording lookups)
target_sessions: dict[str, str] = {}

# Maps CDP message ID -> (method, sessionId) for response correlation
pending_commands: dict[int, tuple[str, str | None]] = {}

# Maps Network.requestId -> {url, frame_id, session_id} for Document-type requests.
# Populated on Network.requestWillBeSent, consumed on Network.responseReceived
# (success) or Network.loadingFailed (failure). Bounded because every entry is
# either matched within a few seconds or the session goes away.
network_requests: dict[str, dict] = {}

# Auto-incrementing CDP message ID
_msg_id = 0

# Active config, updated by the file watcher
_config = Config()

# Prefix prepended to every stdout line this process writes (tee output) and
# to every Logfire message body. Set once in `main()` from `args.cmd` so the
# subcommand-mode is visible at-a-glance and never gets mixed up — `[cdp-log]`
# in CDP mode, `[tinyproxy-log]` in tinyproxy mode. The `-log` suffix makes
# clear these come from the browser-trace shipper, not from chrome's CDP or
# the underlying tinyproxy service.
_log_prefix: str = "[browser-trace]"



def _emit_with_traceparent(log_func, msg: str, attrs: dict) -> None:
    if _config.traceparent:
        with logfire.attach_context({"traceparent": _config.traceparent}):
            log_func(msg, **attrs)
    else:
        log_func(msg, **attrs)


def emit_cdp_event(
    event: str,
    *,
    tab_id: str | None = None,
    tab_url: str | None = None,
    status_code: int | None = None,
    error_text: str | None = None,
    is_main_frame: bool | None = None,
    event_timestamp: str | None = None,
) -> None:
    attrs = {
        "tab_id": tab_id,
        "tab_url": tab_url,
        "status_code": status_code,
        "error_text": error_text,
        "is_main_frame": is_main_frame,
        "event_timestamp": event_timestamp,
    }
    attrs = {k: v for k, v in attrs.items() if v is not None}
    log_func = (
        logfire.error
        if error_text is not None
        or (status_code is not None and status_code >= 400)
        else logfire.info
    )
    msg = f"{_log_prefix} {event}"
    if tab_url:
        msg += f": {tab_url}"
    if status_code:
        msg += f": {status_code}"
    if error_text:
        msg += f": {error_text}"
    _emit_with_traceparent(log_func, msg, attrs)


def apply_config(new: Config) -> None:
    """Apply a new config, reconfiguring logfire if needed."""
    global _config
    old = _config
    _config = new

    if (
        new.logfire_token != old.logfire_token
        or new.service_name != old.service_name
        or new.log_level != old.log_level
    ):
        logfire.configure(
            token=new.logfire_token,
            environment=new.environment,
            send_to_logfire=bool(new.logfire_token),
            service_name=new.service_name,
            inspect_arguments=False,
            # We tee directly to stdout via `{_log_prefix} …` prints — prefix is
            # `[cdp-log]` or `[tinyproxy-log]` depending on the subcommand.
            # Disabling Logfire's console output prevents every emitted record
            # from being printed a second time, halving Fly-log volume.
            console=False,
            # Unify the tee threshold (Fly logs) and the Logfire emission
            # threshold under a single LOG_LEVEL knob. At `LOG_LEVEL=INFO`
            # (default) the tinyproxy `CONNECT` / `INFO` lines — mapped to
            # `logfire.debug` — are dropped before being sent to Logfire, so
            # the UI isn't billed for per-subresource noise. Set
            # `LOG_LEVEL=DEBUG` in the config file to surface them.
            min_level=_logfire_min_level(new.log_level),
        )
        if new.logfire_token:
            print(
                f"{_log_prefix} Logfire configured: service={new.service_name} environment={new.environment}",
                flush=True,
            )
        else:
            print(f"{_log_prefix} Logfire token not configured", flush=True)

    if new.traceparent != old.traceparent:
        if new.traceparent:
            print(f"{_log_prefix} Updated traceparent: {new.traceparent[:20]}...", flush=True)
        else:
            print(f"{_log_prefix} Traceparent cleared", flush=True)

    recordings_dir = (
        Path(new.recording_dir).resolve()
        if new.recording_dir
        else Path("/tmp/recordings")
    )
    rec.configure(recordings_dir=recordings_dir)
    print(f"{_log_prefix} Recordings dir: {recordings_dir}", flush=True)


def get_browser_ws_url(host: str = "127.0.0.1", port: int = 9222) -> str:
    """Fetch the browser websocket URL from CDP /json/version endpoint."""
    with urlopen(f"http://{host}:{port}/json/version") as resp:
        data = json.loads(resp.read())
    return data["webSocketDebuggerUrl"]


async def send_cdp(
    ws, method: str, params: dict | None = None, session_id: str | None = None
) -> int:
    """Send a CDP command over the websocket. Returns the message ID."""
    global _msg_id
    _msg_id += 1
    msg: dict = {"id": _msg_id, "method": method, "params": params or {}}
    if session_id is not None:
        msg["sessionId"] = session_id
    pending_commands[_msg_id] = (method, session_id)
    await ws.send(json.dumps(msg))
    return _msg_id


def emit_navigation(session: dict, url: str, status_code: int) -> None:
    emit_cdp_event(
        "navigation",
        tab_id=session.get("target_id", ""),
        tab_url=url,
        status_code=status_code,
        event_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    print(
        f"{_log_prefix} navigation: tab={session.get('target_id', '')[:8]} status={status_code} url={url}",
        flush=True,
    )


def emit_navigation_failed(
    session: dict, url: str, error_text: str, is_main_frame: bool
) -> None:
    emit_cdp_event(
        "navigation_failed",
        tab_id=session.get("target_id", ""),
        tab_url=url,
        error_text=error_text,
        is_main_frame=is_main_frame,
        event_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    frame_tag = "main" if is_main_frame else "iframe"
    print(
        f"{_log_prefix} navigation_failed: tab={session.get('target_id', '')[:8]} frame={frame_tag} error={error_text} url={url}",
        flush=True,
    )


def flush_pending(session: dict) -> None:
    """Check pending responses against the now-known main frame ID and emit matches."""
    main_frame_id = session.get("main_frame_id")
    if not main_frame_id:
        return
    for pending in session.pop("pending", []):
        if pending["frame_id"] == main_frame_id:
            emit_navigation(session, pending["url"], pending["status_code"])


async def handle_response(event: dict) -> None:
    """Handle CDP command responses (e.g., Page.getFrameTree result)."""
    msg_id = event.get("id")
    if msg_id not in pending_commands:
        return

    method, session_id = pending_commands.pop(msg_id)
    result = event.get("result", {})

    if method == "Page.getFrameTree" and session_id and session_id in sessions:
        # Extract main frame ID from the frame tree
        frame_tree = result.get("frameTree", {})
        frame = frame_tree.get("frame", {})
        frame_id = frame.get("id")
        if frame_id:
            sessions[session_id]["main_frame_id"] = frame_id
            flush_pending(sessions[session_id])


async def handle_event(ws, event: dict) -> None:
    """Process a CDP event."""
    method = event.get("method", "")
    params = event.get("params", {})
    session_id = event.get("sessionId")

    if method == "Target.targetCreated":
        target_info = params.get("targetInfo", {})
        if target_info.get("type") == "page":
            emit_cdp_event(
                "tab_opened",
                tab_id=target_info.get("targetId", ""),
                tab_url=target_info.get("url", ""),
                event_timestamp=datetime.now(timezone.utc).isoformat(),
            )
            print(
                f"{_log_prefix} tab_opened: id={target_info.get('targetId', '')[:8]} url={target_info.get('url', '')}",
                flush=True,
            )

    elif method == "Target.attachedToTarget":
        target_info = params.get("targetInfo", {})
        sid = params.get("sessionId", "")
        if target_info.get("type") == "page" and sid:
            target_id = target_info.get("targetId", "")
            url = target_info.get("url", "")
            sessions[sid] = {
                "target_id": target_id,
                "url": url,
                "main_frame_id": None,
                "pending": [],
            }
            target_sessions[target_id] = sid
            await send_cdp(ws, "Page.enable", session_id=sid)
            await send_cdp(ws, "Network.enable", session_id=sid)
            await send_cdp(ws, "Page.getFrameTree", session_id=sid)
            try:
                await rec.start_recording(sid, target_id, ws, send_cdp, url)
            except Exception as e:
                print(
                    f"{_log_prefix} failed to record tab {sid[:8]}: {e}",
                    flush=True,
                )

    elif method == "Target.detachedFromTarget":
        sid = params.get("sessionId", "")
        session = sessions.pop(sid, None)
        if session:
            target_sessions.pop(session.get("target_id", ""), None)
        # Drop any in-flight Document requests scoped to this dying session
        # so network_requests can't grow unbounded across tab churn.
        stale = [rid for rid, info in network_requests.items() if info.get("session_id") == sid]
        for rid in stale:
            network_requests.pop(rid, None)
        await rec.stop_recording(sid)

    elif method == "Page.screencastFrame" and session_id:
        rec.handle_screencast_frame(params, session_id, ws, send_cdp)

    elif method == "Page.frameNavigated" and session_id:
        frame = params.get("frame", {})
        if "parentId" not in frame and session_id in sessions:
            sessions[session_id]["main_frame_id"] = frame.get("id")
            flush_pending(sessions[session_id])

    elif method == "Network.requestWillBeSent" and session_id:
        request_id = params.get("requestId", "")
        resource_type = params.get("type", "")
        # Track only top-level Document requests; everything else is
        # sub-resource noise we don't surface as a navigation event.
        if request_id and resource_type == "Document":
            request = params.get("request", {})
            network_requests[request_id] = {
                "url": request.get("url", ""),
                "frame_id": params.get("frameId", ""),
                "session_id": session_id,
            }

    elif method == "Network.responseReceived" and session_id:
        resp = params.get("response", {})
        frame_id = params.get("frameId", "")
        resource_type = params.get("type", "")
        request_id = params.get("requestId", "")
        # Successful response — drop the in-flight entry.
        network_requests.pop(request_id, None)

        if session_id in sessions and resource_type == "Document":
            session = sessions[session_id]
            status_code = resp.get("status", 0)
            url = resp.get("url", "")

            if session.get("main_frame_id"):
                # We know the main frame — emit if it matches
                if frame_id == session["main_frame_id"]:
                    emit_navigation(session, url, status_code)
            else:
                # Main frame ID not yet known — buffer for later
                session.setdefault("pending", []).append(
                    {
                        "frame_id": frame_id,
                        "url": url,
                        "status_code": status_code,
                    }
                )

    elif method == "Network.loadingFailed":
        # Tunnel failures (proxy returns non-200 to CONNECT), DNS errors,
        # cert errors, etc. all surface here — Network.responseReceived does
        # NOT fire for these, so this is the only CDP path that catches them.
        # We emit for every failed Document load (main frame OR iframe),
        # tagging which it was via `is_main_frame` so the dashboard can split
        # them; sub-resources are already excluded upstream because we only
        # populate `network_requests` for `type=Document`.
        request_id = params.get("requestId", "")
        req_info = network_requests.pop(request_id, None)
        if req_info is None:
            return  # Sub-resource or already cleaned up
        if params.get("canceled", False):
            return  # User-initiated abort, not a real failure
        sess = sessions.get(req_info["session_id"])
        if sess is None:
            return
        is_main_frame = req_info["frame_id"] == sess.get("main_frame_id")
        emit_navigation_failed(
            sess,
            req_info["url"],
            params.get("errorText", ""),
            is_main_frame,
        )


async def watch_config(config_path: str, interval: float = 1.0) -> None:
    """Watch the config file for changes and reload when it appears or changes."""
    last_mtime: float | None = None
    while True:
        try:
            mtime = os.path.getmtime(config_path)
            if mtime != last_mtime:
                last_mtime = mtime
                apply_config(Config.from_file(config_path))
                print(f"{_log_prefix} Config file updated: {config_path}", flush=True)
        except FileNotFoundError:
            if last_mtime is not None:
                last_mtime = None
                apply_config(Config())
                print(f"{_log_prefix} Config file removed, reverted to defaults", flush=True)
        await asyncio.sleep(interval)


async def connect_cdp(poll_interval: float = 5.0) -> None:
    """Block until CDP is reachable, then run the session. Retries on failure."""
    while True:
        host, port = _config.cdp_host, _config.cdp_port
        try:
            ws_url = get_browser_ws_url(host=host, port=port)
        except (OSError, Exception):
            print(f"{_log_prefix} CDP not reachable at {host}:{port} — retrying in {poll_interval}s", flush=True)
            await asyncio.sleep(poll_interval)
            continue

        print(f"{_log_prefix} Connecting to {ws_url}", flush=True)
        try:
            async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
                print(f"{_log_prefix} Connected to CDP", flush=True)

                await send_cdp(ws, "Target.setDiscoverTargets", {"discover": True})
                await send_cdp(
                    ws,
                    "Target.setAutoAttach",
                    {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": False,
                        "flatten": True,
                    },
                )

                async for raw_msg in ws:
                    try:
                        event = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    if "id" in event and "method" not in event:
                        await handle_response(event)
                        continue

                    await handle_event(ws, event)
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            print(f"{_log_prefix} CDP connection lost ({exc}) — retrying in {poll_interval}s", flush=True)
            await rec.stop_all()
            sessions.clear()
            target_sessions.clear()
            pending_commands.clear()
            network_requests.clear()
            await asyncio.sleep(poll_interval)


async def run(config_path: str) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop_event.set)

    watcher = asyncio.create_task(watch_config(config_path))
    cdp_task = asyncio.create_task(connect_cdp())
    try:
        # Wait until a signal fires or cdp_task ends on its own
        stop_future = asyncio.ensure_future(stop_event.wait())
        await asyncio.wait([cdp_task, stop_future], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (watcher, cdp_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await rec.stop_all()
        print(f"{_log_prefix} Shutting down", flush=True)


# Tinyproxy emits lines like:
#   CONNECT   May 12 20:25:37 [123]: Connection from 127.0.0.1
#   ERROR     May 12 20:25:38 [123]: HTTP 407 from upstream
# The first whitespace-delimited token is the level. Anything we don't
# recognize falls back to `info`.
TINYPROXY_LEVEL_TO_LOGFIRE_METHOD: dict[str, str] = {
    "CRITICAL": "error",
    "ERROR": "error",
    "WARNING": "warn",
    "NOTICE": "notice",
    # CONNECT + INFO are per-request volume noise (every HTTPS subresource emits
    # 2–3 lines). They still go to Logfire at debug severity so the UI keeps
    # them, but at `LOG_LEVEL=INFO` (default) they don't tee to stdout / Fly
    # logs — see `_should_tee`.
    "CONNECT": "debug",
    "INFO": "debug",
}

# Logfire severity ranks (matching the OTel severityNumber buckets) so the tee
# threshold can compare across method names and the user-facing LOG_LEVEL.
_LOGFIRE_METHOD_RANK: dict[str, int] = {
    "debug": 5,
    "info": 9,
    "notice": 10,
    "warn": 13,
    "error": 17,
    "fatal": 21,
}
_LOG_LEVEL_RANK: dict[str, int] = {
    "DEBUG": 5,
    "INFO": 9,
    "NOTICE": 10,
    "WARN": 13,
    "WARNING": 13,
    "ERROR": 17,
    "FATAL": 21,
    "CRITICAL": 21,
}


def _should_tee(method_name: str, log_level: str) -> bool:
    line_rank = _LOGFIRE_METHOD_RANK.get(method_name, 9)
    threshold = _LOG_LEVEL_RANK.get(log_level.upper(), 9)
    return line_rank >= threshold


# Map our LOG_LEVEL value to the lowercase `LevelName` strings that
# `logfire.configure(min_level=...)` accepts. Unknown values default to `info`.
_LOG_LEVEL_TO_LOGFIRE_NAME: dict[str, str] = {
    "DEBUG": "debug",
    "INFO": "info",
    "NOTICE": "notice",
    "WARN": "warn",
    "WARNING": "warn",
    "ERROR": "error",
    "FATAL": "fatal",
    "CRITICAL": "fatal",
}


def _logfire_min_level(log_level: str) -> str:
    return _LOG_LEVEL_TO_LOGFIRE_NAME.get(log_level.upper(), "info")


def parse_tinyproxy_level(line: str) -> str:
    parts = line.split(None, 1)
    if not parts:
        return ""
    return parts[0].upper()


# Tinyproxy emits lines like:
#   ERROR     May 12 22:46:31.766 [609]: read_request_line: Client closed socket
# Strip the date + pid prefix — Logfire stores its own start_timestamp and Fly
# logs prepend a timestamp too. Result: `ERROR read_request_line: Client closed
# socket`. The level stays in the body for grep convenience; the structured
# value is also in the `tinyproxy_level` attribute.
_TINYPROXY_PREFIX_RE = re.compile(
    r"^(\S+)\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\[\d+\]:\s*(.*)$"
)


def strip_tinyproxy_timestamp(line: str) -> str:
    m = _TINYPROXY_PREFIX_RE.match(line)
    if not m:
        return line
    return f"{m.group(1)} {m.group(2)}"


# Tinyproxy emits some lines at ERROR severity that are operationally noise
# (Chrome opens speculative TCP connections it never writes a request on, etc.).
# These get demoted to `logfire.debug` so they don't show at LOG_LEVEL=INFO but
# remain available at DEBUG for triage.
_TINYPROXY_NOISE_PATTERNS: tuple[str, ...] = (
    "read_request_line: Client",
)


def _is_tinyproxy_noise(body: str) -> bool:
    return any(pat in body for pat in _TINYPROXY_NOISE_PATTERNS)


def classify_tinyproxy_line(line: str) -> tuple[str, str, str]:
    body = strip_tinyproxy_timestamp(line)
    level = parse_tinyproxy_level(body)
    method_name = TINYPROXY_LEVEL_TO_LOGFIRE_METHOD.get(level, "info")
    if _is_tinyproxy_noise(body):
        method_name = "debug"
    return body, level, method_name


def emit_tinyproxy_event(body: str, level: str, method_name: str) -> None:
    log_func = getattr(logfire, method_name, logfire.info)
    attrs = {
        "tinyproxy_level": level or "UNKNOWN",
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _emit_with_traceparent(log_func, f"{_log_prefix} {body}", attrs)


def watch_config_thread(config_path: str, interval: float = 1.0) -> None:
    last_mtime: float | None = None
    while True:
        try:
            mtime = os.path.getmtime(config_path)
            if mtime != last_mtime:
                last_mtime = mtime
                apply_config(Config.from_file(config_path))
                print(f"{_log_prefix} Config file updated: {config_path}", flush=True)
        except FileNotFoundError:
            if last_mtime is not None:
                last_mtime = None
                apply_config(Config())
                print(f"{_log_prefix} Config file removed, reverted to defaults", flush=True)
        time.sleep(interval)


def run_tinyproxy(config_path: str) -> None:
    print(f"{_log_prefix} Starting tinyproxy log shipper, reading from stdin", flush=True)
    watcher = threading.Thread(
        target=watch_config_thread, args=(config_path,), daemon=True
    )
    watcher.start()

    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        body, level, method_name = classify_tinyproxy_line(line)
        # Tee to stdout (Fly logs) only when the line's severity meets the
        # configured LOG_LEVEL threshold. Logfire emission below honours the
        # same threshold via `logfire.configure(min_level=...)`.
        if _should_tee(method_name, _config.log_level):
            print(f"{_log_prefix} {body}", flush=True)
        try:
            emit_tinyproxy_event(body, level, method_name)
        except Exception as e:
            print(f"{_log_prefix} failed to emit tinyproxy event: {e}", flush=True)
    print(f"{_log_prefix} stdin closed, shutting down", flush=True)


def main() -> None:
    # Legacy invocation `browser-trace <config>` is sugar for the `cdp`
    # subcommand. If the first arg is not a known subcommand (and isn't a
    # flag), insert `cdp` so the existing chrome-live deployment keeps
    # working unchanged.
    known_cmds = {"cdp", "tinyproxy"}
    if (
        len(sys.argv) >= 2
        and sys.argv[1] not in known_cmds
        and not sys.argv[1].startswith("-")
    ):
        sys.argv.insert(1, "cdp")

    parser = argparse.ArgumentParser(
        description="Browser trace + tinyproxy log shipper"
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_cdp = subparsers.add_parser(
        "cdp",
        help="Watch browser tabs via CDP and emit navigation events to Logfire",
    )
    p_cdp.add_argument("config", help="Path to the config file")

    p_tp = subparsers.add_parser(
        "tinyproxy",
        help="Read tinyproxy log lines from stdin and forward them to Logfire",
    )
    p_tp.add_argument("config", help="Path to the config file")

    args = parser.parse_args()

    # Pin the stdout / Logfire-message prefix to the actual subcommand so the
    # CDP and tinyproxy modes never get mixed up.
    global _log_prefix
    _log_prefix = f"[{args.cmd}-log]"

    config = Config.from_file(args.config)
    if not os.path.exists(args.config):
        print(
            f"{_log_prefix} Config file not found: {args.config} — starting with defaults, watching for file",
            flush=True,
        )
    apply_config(config)

    if args.cmd == "cdp":
        print(f"{_log_prefix} Starting browser trace service (CDP mode)", flush=True)
        try:
            asyncio.run(run(args.config))
        except KeyboardInterrupt:
            pass
    elif args.cmd == "tinyproxy":
        try:
            run_tinyproxy(args.config)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
