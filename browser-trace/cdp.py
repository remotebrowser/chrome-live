"""The CDP websocket link: message framing, reply correlation, teardown.

Two ways to issue a command:

    await conn.send(...)  fire-and-forget; the reply, if it matters, is matched
                          back to its method by `take_reply` in the caller's
                          response dispatcher.
    await conn.call(...)  wait for the reply and return its `result`.
"""

import asyncio
import json
from typing import NamedTuple


class CdpError(Exception):
    """Chrome answered a command with an `error`."""


class _Dispatched(NamedTuple):
    """A `send`, whose reply the caller dispatches on in its own handler."""

    method: str
    session_id: str | None


class _Awaited(NamedTuple):
    """A `call`, whose reply goes to the coroutine blocked on it."""

    method: str
    future: asyncio.Future


class Connection:
    def __init__(self, ws) -> None:
        self._ws = ws
        self._msg_id = 0
        self._dispatched: dict[int, _Dispatched] = {}
        self._awaited: dict[int, _Awaited] = {}

    async def send(
        self, method: str, params: dict | None = None, session_id: str | None = None
    ) -> int:
        """Issue a command without waiting for it. Returns its message id."""
        msg_id, frame = self._frame(method, params, session_id)
        self._dispatched[msg_id] = _Dispatched(method, session_id)
        await self._ws.send(frame)
        return msg_id

    async def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 5.0,
    ) -> dict:
        """Issue a command and return its `result`.

        Raises `CdpError` if Chrome reports an error or the connection closes
        first, and `TimeoutError` if no reply arrives — a command against a
        backgrounded tab can go unanswered indefinitely.
        """
        msg_id, frame = self._frame(method, params, session_id)
        future = asyncio.get_running_loop().create_future()
        self._awaited[msg_id] = _Awaited(method, future)
        try:
            await self._ws.send(frame)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._awaited.pop(msg_id, None)

    def take_reply(self, event: dict) -> tuple[str, str | None, dict] | None:
        """Match a command reply back to the command that produced it.

        Returns `(method, session_id, result)` for a `send`, whose caller
        dispatches on the method. Returns None when the reply was awaited by a
        `call` — delivered to its waiter here — or belongs to no command of ours.
        """
        msg_id = event.get("id")
        awaited = self._awaited.pop(msg_id, None)
        if awaited is not None:
            if not awaited.future.done():
                error = event.get("error")
                if error is None:
                    awaited.future.set_result(event.get("result", {}))
                else:
                    awaited.future.set_exception(CdpError(f"{awaited.method}: {error}"))
            return None

        dispatched = self._dispatched.pop(msg_id, None)
        if dispatched is None:
            return None
        return dispatched.method, dispatched.session_id, event.get("result", {})

    async def events(self):
        """Yield decoded CDP messages until the socket closes.

        A frame that isn't JSON is skipped rather than killing the session.
        """
        async for raw in self._ws:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue

    def close(self) -> None:
        """Abandon every command still awaiting a reply.

        Call this once the socket is gone: a `call` waiter would otherwise block
        for its full timeout, and both tables would keep session ids that are
        already invalid.
        """
        for awaited in self._awaited.values():
            if not awaited.future.done():
                awaited.future.set_exception(
                    CdpError(f"{awaited.method}: CDP connection closed")
                )
        self._awaited.clear()
        self._dispatched.clear()

    def _frame(
        self, method: str, params: dict | None, session_id: str | None
    ) -> tuple[int, str]:
        self._msg_id += 1
        msg: dict = {"id": self._msg_id, "method": method, "params": params or {}}
        if session_id is not None:
            msg["sessionId"] = session_id
        return self._msg_id, json.dumps(msg)
