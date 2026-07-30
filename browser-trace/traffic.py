"""Per-tab / per-host traffic accounting from CDP Network events.

Sums `encodedDataLength` (wire bytes after content-encoding, 0 for cache hits)
per tab and per host, so the data a browser pulls can be attributed to the pages
that caused it. In chrome-live every request leaves through tinyproxy, so these
totals approximate what the upstream residential proxy bills. They are a floor,
not the bill. What CDP does not report:

  - TLS handshakes and certificate chains, which are per-connection and so hit
    hardest on hosts a page touches once
  - request/upload bytes (CDP exposes no wire size for the request side)
  - TCP/IP header overhead and retransmits
  - WebSocket payloads (frames emit no dataReceived / loadingFinished)
  - fetches made by service-worker and worker targets, which are never attached
    (main.py filters Target.attachedToTarget to `type == "page"`)
  - Chrome's own traffic, which belongs to no tab: safebrowsing, GCM (mtalk),
    autofill, and accounts still reach out despite --disable-background-networking

Measured against a byte-counting proxy under Chrome, one Wikipedia article in a
fresh profile: 573,305 bytes counted here against 697,683 on the wire, so 82% of
the real cost. Per-host coverage tracked transfer size, 97% on the main document
down to 19% on a host contacted once for 1.5 KB. Treat the ratio as
workload-dependent and calibrate against the provider's usage API.

State is keyed by CDP session_id, one entry per open tab, mirroring recording.py.
A tab is finalized when it detaches or the CDP connection drops; the process-wide
totals and the host rollup keep accumulating across tab churn.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit


# Hosts reported per tab, biggest first. Caps the size of a rollup event without
# losing the hosts that actually cost money.
_TOP_HOSTS = 10

# Closed tabs retained for the /traffic snapshot. Process totals and the host
# rollup count past this, so eviction only drops per-tab detail.
_CLOSED_TAB_HISTORY = 50

# Cap on the size of any host dict. A long-running scrape can touch an unbounded
# number of hosts; past the cap, bytes fold into one bucket rather than growing
# the dict for the life of the process.
_MAX_TRACKED_HOSTS = 2000
_OVERFLOW_HOST = "(overflow)"

# Bytes for a request whose URL was never seen (already in flight when the tab
# attached, so no requestWillBeSent reached us).
_UNKNOWN_HOST = "(unknown)"

_UNITS = ("B", "KB", "MB", "GB", "TB")


@dataclass
class HostTotals:
    bytes_received: int = 0
    requests: int = 0


@dataclass
class TabTraffic:
    target_id: str
    session_id: str
    url: str
    started_at: str  # ISO 8601
    bytes_received: int = 0
    requests: int = 0
    by_host: dict[str, HostTotals] = field(default_factory=dict)
    closed_at: str | None = None
    # Cumulative total as of the last rollup event, so each event can carry the
    # delta since the previous one. Deltas sum correctly over a session's
    # events; repeated cumulative values do not.
    reported_bytes: int = 0


@dataclass
class _InflightRequest:
    session_id: str
    host: str
    # Bytes already added from dataReceived chunks. loadingFinished reports a
    # total for the whole request, so only the remainder gets added there.
    counted: int = 0


_tab_by_session: dict[str, TabTraffic] = {}
_closed_tabs: deque[TabTraffic] = deque(maxlen=_CLOSED_TAB_HISTORY)
_inflight: dict[str, _InflightRequest] = {}

# Process-wide rollups. Kept as explicit counters rather than derived from the
# tab lists, which lose closed tabs once _closed_tabs evicts them.
_host_totals: dict[str, HostTotals] = {}
_total_bytes = 0
_total_requests = 0
_closed_tab_count = 0


def open_tab(session_id: str, target_id: str, url: str) -> None:
    """Start (or re-bind) accounting for a tab. Idempotent on re-attach."""
    if session_id in _tab_by_session:
        return
    _tab_by_session[session_id] = TabTraffic(
        target_id=target_id,
        session_id=session_id,
        url=url,
        started_at=datetime.now(timezone.utc).isoformat(),
    )


def set_tab_url(session_id: str, url: str) -> None:
    tab = _tab_by_session.get(session_id)
    if tab is not None and url:
        tab.url = url


def bind_request_host(session_id: str, request_id: str, url: str) -> None:
    """Bind a request to the host it is being fetched from.

    Called from both Network.requestWillBeSent and Network.responseReceived.
    Binding on the response as well matters for the request a tab is already
    making when it attaches: its requestWillBeSent fired before Network.enable
    reached Chrome, so the response is the first place its URL shows up. For a
    freshly opened tab that request is the main document, i.e. the largest one.

    Re-binding is also how redirects are attributed: a redirect reuses the same
    requestId, and each hop's dataReceived arrives before the next hop's event,
    so bytes land on the host that actually served them.
    """
    if not request_id:
        return
    inflight = _inflight.get(request_id)
    if inflight is None:
        _inflight[request_id] = _InflightRequest(
            session_id=session_id, host=_host_of(url)
        )
    else:
        inflight.host = _host_of(url)


def on_data_received(session_id: str, request_id: str, encoded_length: int) -> None:
    tab = _tab_by_session.get(session_id)
    if tab is None:
        return
    inflight = _inflight.get(request_id)
    if inflight is None:
        inflight = _InflightRequest(session_id=session_id, host=_fallback_host(tab))
        _inflight[request_id] = inflight
    inflight.counted += max(0, encoded_length)
    _add_bytes(tab, inflight.host, encoded_length)


def on_loading_finished(session_id: str, request_id: str, encoded_total: int) -> None:
    """Settle a finished request against the chunks already counted for it.

    `encodedDataLength` here is the request's total, not a delta, and it covers
    header bytes that never appeared in a dataReceived chunk. Clamped at zero
    because a redirect chain can leave `counted` above the final total.
    """
    inflight = _inflight.pop(request_id, None)
    tab = _tab_by_session.get(session_id)
    if tab is None:
        return
    host = inflight.host if inflight is not None else _fallback_host(tab)
    counted = inflight.counted if inflight is not None else 0
    _add_bytes(tab, host, max(0, encoded_total - counted))
    _count_request(tab, host)


def on_loading_failed(session_id: str, request_id: str) -> None:
    """Retire a failed request, keeping whatever bytes already arrived."""
    inflight = _inflight.pop(request_id, None)
    tab = _tab_by_session.get(session_id)
    if tab is None:
        return
    _count_request(tab, inflight.host if inflight is not None else _fallback_host(tab))


def close_tab(session_id: str) -> TabTraffic | None:
    """Finalize a tab and return its totals, or None if it was never tracked."""
    tab = _tab_by_session.pop(session_id, None)
    # Drop in-flight requests scoped to this session so _inflight can't grow
    # across tab churn (a request whose loadingFinished never arrives would
    # otherwise stay forever).
    for request_id in [
        rid for rid, info in _inflight.items() if info.session_id == session_id
    ]:
        _inflight.pop(request_id, None)
    if tab is None:
        return None
    global _closed_tab_count
    tab.closed_at = datetime.now(timezone.utc).isoformat()
    _closed_tabs.append(tab)
    _closed_tab_count += 1
    return tab


def close_all() -> list[TabTraffic]:
    return [
        tab
        for tab in (close_tab(sid) for sid in list(_tab_by_session))
        if tab is not None
    ]


def open_tabs() -> list[TabTraffic]:
    return list(_tab_by_session.values())


def top_hosts(tab: TabTraffic, limit: int = _TOP_HOSTS) -> list[tuple[str, int]]:
    ranked = sorted(
        tab.by_host.items(), key=lambda kv: kv[1].bytes_received, reverse=True
    )
    return [(host, totals.bytes_received) for host, totals in ranked[:limit]]


def snapshot(host_limit: int = 20) -> dict:
    """Current totals, for the /traffic endpoint."""
    open_list = sorted(
        _tab_by_session.values(), key=lambda t: t.bytes_received, reverse=True
    )
    return {
        "totals": {
            "bytes_received": _total_bytes,
            "bytes_received_human": human_bytes(_total_bytes),
            "requests": _total_requests,
            "hosts": len(_host_totals),
            "tabs_open": len(_tab_by_session),
            "tabs_closed": _closed_tab_count,
            "requests_in_flight": len(_inflight),
        },
        "hosts": [
            {
                "host": host,
                "bytes_received": totals.bytes_received,
                "bytes_received_human": human_bytes(totals.bytes_received),
                "requests": totals.requests,
            }
            for host, totals in sorted(
                _host_totals.items(),
                key=lambda kv: kv[1].bytes_received,
                reverse=True,
            )[:host_limit]
        ],
        "tabs": {
            "open": [_tab_dict(tab) for tab in open_list],
            # Newest first, matching /recordings.
            "closed": [_tab_dict(tab) for tab in reversed(_closed_tabs)],
        },
    }


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in _UNITS[:-1]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def _tab_dict(tab: TabTraffic) -> dict:
    return {
        "target_id": tab.target_id,
        "session_id": tab.session_id,
        "url": tab.url,
        "started_at": tab.started_at,
        "closed_at": tab.closed_at,
        "bytes_received": tab.bytes_received,
        "bytes_received_human": human_bytes(tab.bytes_received),
        "requests": tab.requests,
        "hosts": [
            {
                "host": host,
                "bytes_received": totals.bytes_received,
                "requests": totals.requests,
            }
            for host, totals in sorted(
                tab.by_host.items(),
                key=lambda kv: kv[1].bytes_received,
                reverse=True,
            )
        ],
    }


def _add_bytes(tab: TabTraffic, host: str, n: int) -> None:
    if n <= 0:
        return
    global _total_bytes
    tab.bytes_received += n
    _bucket(tab.by_host, host).bytes_received += n
    _bucket(_host_totals, host).bytes_received += n
    _total_bytes += n


def _count_request(tab: TabTraffic, host: str) -> None:
    """Count a completed request against the host it finished on.

    Counted at completion rather than at send time so a request's bytes and its
    request count always land on the same host, even across redirects.
    """
    global _total_requests
    tab.requests += 1
    _bucket(tab.by_host, host).requests += 1
    _bucket(_host_totals, host).requests += 1
    _total_requests += 1


def _bucket(totals: dict[str, HostTotals], host: str) -> HostTotals:
    entry = totals.get(host)
    if entry is not None:
        return entry
    if len(totals) >= _MAX_TRACKED_HOSTS:
        host = _OVERFLOW_HOST
        entry = totals.get(host)
        if entry is not None:
            return entry
    entry = HostTotals()
    totals[host] = entry
    return entry


def _fallback_host(tab: TabTraffic) -> str:
    """Host for bytes on a request whose URL never reached us.

    A tab opened straight onto a URL is already streaming its main document by
    the time Target.attachedToTarget lets us send Network.enable, so neither
    requestWillBeSent nor responseReceived fires for it — only dataReceived and
    loadingFinished, which carry no URL. That request is the document itself
    (measured: exactly one such request per freshly opened tab, and it is the
    largest one), so the tab's own URL names its host. Tabs that attach on
    about:blank and navigate afterwards are bound normally and never land here.
    """
    hostname = urlsplit(tab.url).hostname if tab.url else None
    return hostname.lower() if hostname else _UNKNOWN_HOST


def _host_of(url: str) -> str:
    if not url:
        return _UNKNOWN_HOST
    parts = urlsplit(url)
    if parts.hostname:
        return parts.hostname.lower()
    # data:, blob:, about: … carry no host. Bucket them by scheme so a tab's
    # total still equals the sum of its hosts.
    return f"{parts.scheme}:" if parts.scheme else _UNKNOWN_HOST
