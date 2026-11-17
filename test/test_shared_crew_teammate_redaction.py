"""A Shared Crew teammate passes the owner gate but reads redacted.

The owner's credential-redaction switch, off by default, covers only what the owner
alone sees (the recorded credential-redaction decision). A teammate's socket gets
every owner frame and their requests clear owner-gated routes, yet the SSE stream,
the WebSocket fan-out, the HTTP audience and the reveal route treat them as any
non-owner reader.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew import redaction_seams
from kiro_crew.dashboard import redaction_audience
from kiro_crew.dashboard.kiro_relay.caller import CrewKind, HostBinding
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew.teammate_access import TEAMMATE_WS_FLAG
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import finished

TEAMMATE = "kiro-user.teammate-0002"


def _team() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    return ingress


def _relay_app(state, ingress: RelayIngress) -> web.Application:
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            token_auth_middleware(port=5476),
        ]
    )
    app["state"] = state
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    return app


@pytest.mark.asyncio
async def test_the_sse_stream_redacts_what_a_teammate_reads_with_redaction_off(
    tmp_path, monkeypatch
):
    """A note built inside the owner's seam reaches a teammate redacted in full."""
    from kiro_crew.dashboard.handlers import updates

    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    state = _make_state(tmp_path)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    ingress = _team()
    app = _relay_app(state, ingress)
    app.router.add_get("/api/stream", updates.api_stream)
    token = github_classic(seed=171)
    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/stream", headers=ingress.stamp_headers([], relay_user=TEAMMATE)
            )
            assert resp.status == 200
            first = await finished(resp.content.readuntil(b"\n\n"), "the first SSE frame")
            assert first.startswith(b"event: dashboard")
            state._broadcast({"source": "system", "body": f"key {token} end"})
            state._broadcast({"source": "system", "body": "fence"})
            blocks: list[bytes] = []
            while b"fence" not in (blocks[-1] if blocks else b""):
                blocks.append(await finished(resp.content.readuntil(b"\n\n"), "an SSE frame"))
            resp.close()
    finally:
        stopped.set()
    assert any(b"key " in block for block in blocks)
    assert not [block for block in blocks if token.encode() in block]


@pytest.mark.asyncio
async def test_a_teammates_request_is_a_non_owner_audience(tmp_path) -> None:
    """The owner gate admits the teammate; the redaction audience does not read them as the owner."""
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    seen: dict[str, object] = {}

    async def probe(request: web.Request) -> web.Response:
        seen["gate"] = is_owner_dashboard_request(request)
        seen["audience"] = redaction_audience.request_audience(request)
        return web.json_response({})

    state = _make_state(tmp_path)
    ingress = _team()
    app = _relay_app(state, ingress)
    app.router.add_get("/api/members", probe)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/members", headers=ingress.stamp_headers([], relay_user=TEAMMATE)
        )
        assert resp.status == 200
    assert seen == {"gate": True, "audience": redaction_seams.NON_OWNER}


class _WS:
    def __init__(self, flags: dict) -> None:
        self.closed = False
        self._flags = flags

    def get(self, key, default=None):
        return self._flags.get(key, default)


def test_a_teammates_owner_socket_reads_as_a_non_owner() -> None:
    owner, teammate, other = _WS({}), _WS({TEAMMATE_WS_FLAG: True}), _WS({})
    owners = {owner, teammate}
    assert redaction_audience.socket_reads_as_owner(owner, owners)
    assert not redaction_audience.socket_reads_as_owner(teammate, owners)
    assert not redaction_audience.socket_reads_as_owner(other, owners)


@pytest.mark.asyncio
async def test_the_hub_sends_a_teammates_owner_socket_the_redacted_frame(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    state = _make_state(tmp_path)
    owner = _SendingWS({"_is_dashboard_user": True})
    teammate = _SendingWS({"_is_dashboard_user": True, TEAMMATE_WS_FLAG: True})
    state.register_ws(owner, owner=True)
    state.register_ws(teammate, owner=True)
    token = github_classic(seed=172)
    data = {"source": "system", "body": f"key {token}"}
    state._send_ws_all("notification", data, json.dumps({"type": "notification", "data": data}))
    await finished(asyncio.gather(owner.got.wait(), teammate.got.wait()), "both sends")
    assert token in owner.sent[0]
    assert "key " in teammate.sent[0]
    assert token not in teammate.sent[0]


class _SendingWS(_WS):
    def __init__(self, flags: dict) -> None:
        super().__init__(flags)
        self.sent: list[str] = []
        self.got = asyncio.Event()

    def __setitem__(self, key, value):
        self._flags[key] = value

    def pop(self, key, default=None):
        return self._flags.pop(key, default)

    async def send_str(self, text: str) -> None:
        self.sent.append(text)
        self.got.set()


@pytest.mark.asyncio
async def test_an_owner_delivery_reaches_a_teammate_redacted(tmp_path, monkeypatch) -> None:
    """``deliver_ws_owners`` (question, change and guide cards) awaits each owner send
    itself, outside the fan-out; a teammate's socket still reads it redacted."""
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    state = _make_state(tmp_path)
    owner = _SendingWS({"_is_dashboard_user": True})
    teammate = _SendingWS({"_is_dashboard_user": True, TEAMMATE_WS_FLAG: True})
    state.register_ws(owner, owner=True)
    state.register_ws(teammate, owner=True)
    token = github_classic(seed=173)
    delivered = await finished(
        state.deliver_ws_owners("question_card", {"question": f"use {token}?"}),
        "the owner delivery",
    )
    assert delivered == 2
    assert token in owner.sent[0]
    assert "use " in teammate.sent[0]
    assert token not in teammate.sent[0]


def test_a_teammate_never_gets_the_raw_model_context(monkeypatch) -> None:
    """Layer B is the byte-exact, never-redacted model context: the machine owner's alone."""
    import inspect

    from kiro_crew.dashboard import session_export

    source = inspect.getsource(session_export)
    assert "and is_host_owner_dashboard_request(request)" in source
    assert "is_owner_dashboard_request(" not in source.replace(
        "is_host_owner_dashboard_request(", ""
    )


@pytest.mark.parametrize("module_attr", ["outbox", "agents", "agent_catalog"])
def test_every_unmasked_view_asks_the_machine_owner(module_attr: str) -> None:
    """The raw-versus-masked choice on these views reads the machine owner, never the
    owner gate a teammate passes."""
    import inspect

    from kiro_crew.dashboard.file_api import outbox
    from kiro_crew.dashboard.handlers import agent_catalog, agents

    fn = {
        "outbox": outbox._caller_is_owner,
        "agents": agents.api_kirocrew_agents,
        "agent_catalog": agent_catalog.api_agent_catalog,
    }[module_attr]
    assert "is_host_owner_dashboard_request(request)" in inspect.getsource(fn)


def test_a_teammates_outbox_download_is_scanned() -> None:
    from types import SimpleNamespace

    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.file_api import outbox
    from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
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
    from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO

    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="U0OWNER")
    request = make_mocked_request("GET", "/api/outbox/f.txt", app=app)
    request["app"] = ""
    request["is_dashboard_user"] = True
    request["user"] = f"relay:{TEAMMATE}"
    request[RELAY_CALLER_KEY] = RelayCaller(kiro_user_id=TEAMMATE, kind=RelayCallerKind.TEAMMATE)
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=Principal(
            id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, TEAMMATE),
            kind=PrincipalKind.HUMAN,
            provider=PROVIDER_KIRO,
            issuer=KIRO_ISSUER,
            subject=TEAMMATE,
            provenance=Provenance.KIRO_RELAY,
            display=DisplaySnapshot(name="Ana", login=""),
        ),
        request_id="req",
        transport=Transport.HTTP,
    )
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    assert is_owner_dashboard_request(request)
    assert outbox._caller_is_owner(request) is False
