"""One collaborator's typing reaches another collaborator's open socket.

Two real ``/api/ws`` connections behind the real token middleware. The actor of
each upgrade is published by an inner middleware the way the auth middleware
publishes a verified tailnet person, so the two sockets belong to two people.
Alice's socket reports ``typing``; Bob's socket must receive the presence frame
naming Alice as typing, and Alice's report must carry no principal of its own.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth, ws_event_scope
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)

_SLOT = "typing-slot"


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


PEOPLE = {"alice": _person("alice@example.test"), "bob": _person("bob@example.test")}


@pytest.fixture
def ws_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"typing-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    for name in ("_declared_cache", "_exposeto_cache", "_sel_last_audit"):
        monkeypatch.setattr(ws_event_scope, name, {})
    for name in ("_declared_refreshing", "_exposeto_refreshing"):
        monkeypatch.setattr(ws_event_scope, name, set())
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    state = _make_state(tmp_path)
    state.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    import time as _time

    from kiro_crew.dashboard import status_counts as _sc

    monkeypatch.setattr(_sc, "_counts_cache", (0, 0))
    monkeypatch.setattr(_sc, "_counts_cache_ts", _time.monotonic())
    monkeypatch.setattr(_sc, "_counts_cache_failures", 0)
    monkeypatch.setattr(_sc, "_counts_refresh_inflight", False)
    yield state
    stopped.set()


@web.middleware
async def _as_person(request: web.Request, handler):
    who = request.query.get("as", "")
    if who in PEOPLE:
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=PEOPLE[who], request_id=who, transport=Transport.WEBSOCKET
        )
    return await handler(request)


async def _presence_naming(socket, login: str, activity: str) -> dict:
    while True:
        frame = await socket.receive_json()
        if frame.get("type") != "shared_crew.presence":
            continue
        entries = frame["data"].get("entries", [])
        for entry in entries:
            if entry["principal"]["display"]["login"] == login and entry["activity"] == activity:
                return frame["data"]


@pytest.mark.asyncio
async def test_alice_typing_reaches_bobs_socket(ws_state):
    from kiro_crew.dashboard.ws import api_ws

    app = _make_app(ws_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app.middlewares.insert(1, _as_person)
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)

    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        alice = await client.ws_connect(
            "/api/ws", params={"token": token_auth.generate_token("local-app"), "as": "alice"}
        )
        bob = await client.ws_connect(
            "/api/ws", params={"token": token_auth.generate_token("local-app"), "as": "bob"}
        )
        await asyncio.wait_for(asyncio.gather(alice.receive_json(), bob.receive_json()), timeout=30)

        await alice.send_json(
            {
                "type": "shared_crew.activity",
                "session_key": _SLOT,
                "activity": "typing",
                "principal": {"id": "forged"},
            }
        )
        seen = await asyncio.wait_for(
            _presence_naming(bob, "alice@example.test", "typing"), timeout=30
        )
        assert seen["session_key"] == _SLOT
        assert [e["principal"]["id"] for e in seen["entries"]] == [PEOPLE["alice"].id]

        await alice.send_json(
            {"type": "shared_crew.activity", "session_key": _SLOT, "activity": "viewing"}
        )
        await bob.close()
        await alice.close()
