"""Fail-closed ownership and revocation for browser WebSocket authority.

HTTP session middleware does not run for WebSocket upgrades.  This ASGI
middleware consumes the native one-time ticket, revalidates its browser
session before the application accepts the socket, and records the stable
session/principal owner.  A durable auth-service outbox subscriber then closes
owned sockets and PTYs when that authority is revoked.

Loopback dashboard mode is deliberately untouched.  In gated mode every
browser WebSocket route is pinned in :data:`BROWSER_WEBSOCKET_PATHS`; an
unreviewed WebSocket route fails closed instead of silently bypassing this
boundary.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from hermes_cli.dashboard_auth import get_provider
from hermes_cli.dashboard_auth.ws_tickets import (
    TicketInvalid,
    consume_internal_credential,
    consume_ticket,
)

BROWSER_WEBSOCKET_PATHS = frozenset({
    "/api/audio/speak-stream",
    "/api/console",
    "/api/pty",
    "/api/ws",
    "/api/pub",
    "/api/events",
    "/api/plugins/kanban/events",
})
INTERNAL_WEBSOCKET_PATHS = frozenset({"/api/ws", "/api/pub"})
REVOCATION_CLOSE_CODE = 4401


@dataclass(frozen=True)
class LiveOwner:
    session_key: str
    tenant_id: str
    principal: str
    provider: str


@dataclass(eq=False)
class _LiveSocket:
    send: Callable[[dict[str, Any]], Awaitable[None]]
    owner: LiveOwner | None = None
    closed: bool = False

    async def close(self, reason: str = "session revoked") -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.send({
                "type": "websocket.close",
                "code": REVOCATION_CLOSE_CODE,
                "reason": reason[:123],
            })


def _stable_session_key(session: Any, access_token: str) -> str:
    key = str(getattr(session, "session_key", "") or "")
    return key or hashlib.sha256(access_token.encode("utf-8")).hexdigest()


class LiveAuthority:
    """Own live browser sockets and consume the durable revocation outbox."""

    def __init__(self) -> None:
        self._client = None
        self._pty_registry = None
        self._sockets: set[_LiveSocket] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_sequence = 0
        self._synchronized = False

    @property
    def synchronized(self) -> bool:
        return self._synchronized

    def configure(self, client: Any, pty_registry: Any) -> None:
        self._client = client
        self._pty_registry = pty_registry

    async def start(self) -> None:
        if self._client is None or self._task is not None:
            return
        try:
            health = await asyncio.to_thread(self._client.health)
            self._last_sequence = int(health.get("revocation_ack", 0))
            await self._drain_outbox()
        except Exception:
            await self._fail_closed("revocation subscriber unavailable")
        self._task = asyncio.create_task(
            self._poll_loop(), name="dashboard-auth-revocations"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._fail_closed("dashboard stopping")

    async def provisional(self, socket: _LiveSocket) -> bool:
        async with self._lock:
            if not self._synchronized:
                return False
            self._sockets.add(socket)
            return True

    async def promote(self, socket: _LiveSocket, owner: LiveOwner) -> bool:
        async with self._lock:
            if not self._synchronized or socket not in self._sockets:
                return False
            socket.owner = owner
            return True

    async def unregister(self, socket: _LiveSocket | None) -> None:
        if socket is None:
            return
        async with self._lock:
            self._sockets.discard(socket)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._drain_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._fail_closed("revocation subscriber unavailable")
            await asyncio.sleep(0.2)

    async def _drain_outbox(self) -> None:
        while True:
            result = await asyncio.to_thread(
                self._client.revocations, after=self._last_sequence, limit=100
            )
            events = list(result.get("events") or [])
            for event in events:
                sequence = int(event["sequence"])
                if sequence != self._last_sequence + 1:
                    await self._fail_closed("revocation sequence gap")
                    raise RuntimeError("revocation sequence gap")
                await self._apply_event(event)
                self._last_sequence = sequence
            if events:
                await asyncio.to_thread(
                    self._client.acknowledge, sequence=self._last_sequence
                )
            if len(events) < 100:
                self._synchronized = True
                return

    async def _apply_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        subject = str(event.get("subject") or "")
        if kind == "session":
            predicate = lambda owner: owner.session_key == subject
        elif kind == "principal":
            try:
                tenant, principal = subject.split(":", 1)
            except ValueError:
                await self._fail_closed("invalid revocation event")
                raise RuntimeError("invalid revocation event")
            predicate = lambda owner: (
                owner.tenant_id == tenant and owner.principal == principal
            )
        elif kind == "global":
            predicate = lambda _owner: True
        else:
            await self._fail_closed("unknown revocation event")
            raise RuntimeError("unknown revocation event")
        await self._close_matching(predicate, "session revoked")

    async def _close_matching(self, predicate, reason: str) -> None:
        async with self._lock:
            doomed = [
                socket for socket in self._sockets
                if socket.owner is None or predicate(socket.owner)
            ]
            for socket in doomed:
                self._sockets.discard(socket)
        await asyncio.gather(
            *(socket.close(reason) for socket in doomed), return_exceptions=True
        )
        if self._pty_registry is not None:
            await self._pty_registry.close_matching(predicate)

    async def _fail_closed(self, reason: str) -> None:
        self._synchronized = False
        async with self._lock:
            doomed = list(self._sockets)
            self._sockets.clear()
        await asyncio.gather(
            *(socket.close(reason) for socket in doomed), return_exceptions=True
        )
        if self._pty_registry is not None:
            await self._pty_registry.close_all()


LIVE_AUTHORITY = LiveAuthority()


async def _authorize_gated(scope: dict[str, Any], socket: _LiveSocket) -> tuple[bool, str]:
    from starlette.datastructures import QueryParams

    path = str(scope.get("path") or "")
    if path not in BROWSER_WEBSOCKET_PATHS:
        return False, "untracked websocket route"
    query = QueryParams(scope.get("query_string", b"").decode("latin-1"))

    internal = query.get("internal", "")
    if internal:
        if path not in INTERNAL_WEBSOCKET_PATHS:
            return False, "internal credential denied"
        try:
            consume_internal_credential(internal)
        except TicketInvalid:
            return False, "internal credential invalid"
        scope.setdefault("state", {})["ws_authority"] = {
            "credential": "internal", "owner": None,
        }
        return True, ""

    ticket = query.get("ticket", "")
    if not ticket:
        return False, "credential required"
    try:
        info = consume_ticket(ticket)
    except TicketInvalid:
        return False, "ticket invalid"
    if not await LIVE_AUTHORITY.provisional(socket):
        return False, "revocation state unsynchronized"

    access_token = str(info.get("access_token") or "")
    provider_name = str(info.get("provider") or "")
    provider = get_provider(provider_name)
    if not access_token or provider is None:
        await LIVE_AUTHORITY.unregister(socket)
        return False, "ticket session unavailable"
    try:
        session = await asyncio.to_thread(
            provider.verify_session, access_token=access_token
        )
    except Exception:
        await LIVE_AUTHORITY.unregister(socket)
        return False, "session verification unavailable"
    if session is None:
        await LIVE_AUTHORITY.unregister(socket)
        return False, "session invalid"

    owner = LiveOwner(
        session_key=_stable_session_key(session, access_token),
        tenant_id=str(getattr(session, "org_id", "") or "").lower(),
        principal=str(getattr(session, "user_id", "") or "").lower(),
        provider=str(getattr(session, "provider", "") or ""),
    )
    expected = (
        str(info.get("session_key") or ""),
        str(info.get("org_id") or "").lower(),
        str(info.get("user_id") or "").lower(),
        provider_name,
    )
    actual = (owner.session_key, owner.tenant_id, owner.principal, owner.provider)
    if expected != actual or not await LIVE_AUTHORITY.promote(socket, owner):
        await LIVE_AUTHORITY.unregister(socket)
        return False, "ticket binding mismatch"
    scope.setdefault("state", {})["ws_authority"] = {
        "credential": "ticket", "owner": owner,
    }
    return True, ""


class WebSocketAuthorityMiddleware:
    """Pure ASGI gate so plugin and core WebSockets share one boundary."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "websocket":
            await self.app(scope, receive, send)
            return
        application = scope.get("app")
        if not bool(
            getattr(getattr(application, "state", None), "live_authority_required", False)
        ):
            await self.app(scope, receive, send)
            return

        socket = _LiveSocket(send=send)
        allowed, reason = await _authorize_gated(scope, socket)
        if not allowed:
            await socket.close(reason)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await LIVE_AUTHORITY.unregister(socket)


def websocket_owner(ws: Any) -> LiveOwner | None:
    state = ws.scope.get("state") or {}
    authority = state.get("ws_authority") or {}
    owner = authority.get("owner")
    return owner if isinstance(owner, LiveOwner) else None
