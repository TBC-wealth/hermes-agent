from __future__ import annotations

import asyncio
from types import SimpleNamespace

from hermes_cli.dashboard_auth.live_authority import (
    LIVE_AUTHORITY,
    LiveAuthority,
    LiveOwner,
    WebSocketAuthorityMiddleware,
    _LiveSocket,
)
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.ws_tickets import mint_ticket


class FakeClient:
    def __init__(self, events=(), *, ack=0):
        self.events = list(events)
        self.ack = ack

    def health(self):
        return {"revocation_ack": self.ack}

    def revocations(self, *, after, limit):
        return {
            "events": [event for event in self.events if event["sequence"] > after][:limit]
        }

    def acknowledge(self, *, sequence):
        self.ack = sequence
        return {"acknowledged": sequence}


class FakePtys:
    def __init__(self, owners=()):
        self.owners = list(owners)
        self.closed_all = 0

    async def close_matching(self, predicate):
        self.owners = [owner for owner in self.owners if not predicate(owner)]

    async def close_all(self):
        self.closed_all += 1
        self.owners.clear()


def _owner(session="session-a", principal="alice"):
    return LiveOwner(session, "tenant", principal, "teams")


def test_session_revocation_closes_only_matching_socket_and_pty():
    async def scenario():
        sent_a, sent_b = [], []
        socket_a = _LiveSocket(sent_a.append, _owner())
        socket_b = _LiveSocket(sent_b.append, _owner("session-b", "bob"))
        ptys = FakePtys([socket_a.owner, socket_b.owner])
        client = FakeClient([{
            "sequence": 1, "kind": "session", "subject": "session-a", "created_at": 1,
        }])
        authority = LiveAuthority()
        authority.configure(client, ptys)
        authority._sockets.update({socket_a, socket_b})
        await authority.start()
        assert authority.synchronized
        assert sent_a and sent_a[0]["code"] == 4401
        assert not sent_b
        assert ptys.owners == [socket_b.owner]
        assert client.ack == 1
        await authority.stop()

    asyncio.run(scenario())


def test_revocation_gap_fails_closed_and_refuses_provisional_registration():
    async def scenario():
        sent = []
        socket = _LiveSocket(sent.append, _owner())
        ptys = FakePtys([socket.owner])
        client = FakeClient([{
            "sequence": 2, "kind": "global", "subject": "2", "created_at": 1,
        }])
        authority = LiveAuthority()
        authority.configure(client, ptys)
        authority._sockets.add(socket)
        await authority.start()
        assert not authority.synchronized
        assert sent and sent[0]["code"] == 4401
        assert ptys.closed_all >= 1
        assert not await authority.provisional(_LiveSocket(sent.append))
        await authority.stop()

    asyncio.run(scenario())


def test_principal_and_global_revocations_match_expected_owners():
    async def scenario():
        alice = _owner("a", "alice")
        bob = _owner("b", "bob")
        ptys = FakePtys([alice, bob])
        client = FakeClient([
            {"sequence": 1, "kind": "principal", "subject": "tenant:alice", "created_at": 1},
            {"sequence": 2, "kind": "global", "subject": "2", "created_at": 2},
        ])
        authority = LiveAuthority()
        authority.configure(client, ptys)
        await authority.start()
        assert ptys.owners == []
        assert client.ack == 2
        await authority.stop()

    asyncio.run(scenario())


def test_asgi_gate_consumes_revalidates_and_attaches_exact_owner():
    class Provider:
        name = "teams"
        display_name = "Teams"
        supports_session = True

        def start_login(self, **_kwargs):
            raise NotImplementedError

        def complete_login(self, **_kwargs):
            raise NotImplementedError

        def verify_session(self, *, access_token):
            assert access_token == "access-secret"
            return Session(
                user_id="alice", email="", display_name="alice",
                org_id="tenant", provider="teams", expires_at=2_000_000_000,
                access_token=access_token, refresh_token="", session_key="session-a",
            )

        def refresh_session(self, **_kwargs):
            raise NotImplementedError

        def revoke_session(self, **_kwargs):
            return None

    async def scenario():
        observed, sent = {}, []

        async def inner(scope, receive, send):
            observed.update(scope["state"]["ws_authority"])

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "websocket.disconnect"}

        clear_providers()
        register_provider(Provider())
        old_sync = LIVE_AUTHORITY._synchronized
        LIVE_AUTHORITY._synchronized = True
        ticket = mint_ticket(
            user_id="alice", provider="teams", session_key="session-a",
            org_id="tenant", access_token="access-secret",
        )
        scope = {
            "type": "websocket", "path": "/api/events",
            "query_string": f"ticket={ticket}".encode(), "headers": [],
            "state": {},
            "app": SimpleNamespace(state=SimpleNamespace(live_authority_required=True)),
        }
        try:
            await WebSocketAuthorityMiddleware(inner)(scope, receive, send)
        finally:
            LIVE_AUTHORITY._synchronized = old_sync
            LIVE_AUTHORITY._sockets.clear()
            clear_providers()
        assert observed["credential"] == "ticket"
        assert observed["owner"] == _owner()
        assert sent == []

    asyncio.run(scenario())
