"""App web origins on a Remote Crew pane's forward port.

A pane loads this dashboard on the hub's forward port P, which is not a dashboard
port here. A signed pane token (``embed_parent_port``, optionally bound by
``embed_forward_port``) records P, and the app web origin router then serves P
with exactly the pane's and the parent's loopback origins as frame ancestors.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.apps import app_web_origin as awo
from kiro_crew.apps import pane_ports as pp
from kiro_crew.dashboard.ssm_gate import GateRef, SsmGate
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware

_PORT = "5476"
_PANE = 7778
_HUB = 5500
#: A token lifetime, not a wait: the tests read claims, never the clock running out.
_TOKEN_TTL_SECS = 3600
#: The remote mint's ``--ttl`` argument, a credential lifetime string.
_MINT_TTL = "20h"
_ALLOWED = {
    f"http://localhost:{_PORT}",
    f"http://127.0.0.1:{_PORT}",
    f"http://[::1]:{_PORT}",
    f"http://kirocrew.localhost:{_PORT}",
}


def _pane_token(**extra: str) -> str:
    claims = {"embed_parent_port": str(_HUB), **extra}
    return generate_token(
        "owner", ttl_seconds=_TOKEN_TTL_SECS, extra=claims
    )  # flake-ok: a credential lifetime, not a wait


def _claims(**extra: str) -> str:
    """A session token as the exchange mints it, so the cookie path can carry it."""
    claims = {"embed_parent_port": str(_HUB), **extra}
    return generate_token(
        "owner",
        ttl_seconds=_TOKEN_TTL_SECS,  # flake-ok: a credential lifetime, not a wait
        extra=claims,
        register_nonce=False,
        session=True,
    )


def _app() -> web.Application:
    app = web.Application()
    app["allowed_origins"] = set(_ALLOWED)
    return app


def _request(app: web.Application, host: str, **headers: str) -> web.Request:
    # A loopback peer: the record wants a request straight from this machine.
    transport = mock.Mock()
    transport.get_extra_info.return_value = ("127.0.0.1", 50000)
    return make_mocked_request(
        "GET", "/", headers={"Host": host, **headers}, app=app, transport=transport
    )


# ── the record ───────────────────────────────────────────────────────────────


def test_record_keeps_a_union_of_parents_until_each_expires() -> None:
    record = pp.PanePorts()
    now = 1000.0
    assert record.record(_PANE, 5500, now + 10, now) is True
    assert record.record(_PANE, 5600, now + 20, now) is True
    # A repeat changes nothing, so it writes no second audit row.
    assert record.record(_PANE, 5500, now + 10, now) is False
    assert record.parents(_PANE, now) == [5500, 5600]
    assert record.parents(_PANE, now + 15) == [5600]
    assert record.parents(_PANE, now + 25) == []
    assert record.record(_PANE, 5500, now - 1, now) is False


def test_an_older_session_never_cuts_short_a_newer_one_on_the_same_ports() -> None:
    record = pp.PanePorts()
    now = 100.0
    # A newer session holds the pairing until 200; an older one on the same
    # forward and parent port signs a request that expires at 110.
    assert record.record(_PANE, _HUB, 200.0, now) is True
    assert record.record(_PANE, _HUB, 110.0, now + 9) is False
    assert record.parents(_PANE, 111.0) == [_HUB]
    # A later expiry still extends the grant, and says so for the audit row.
    assert record.record(_PANE, _HUB, 300.0, now + 10) is True
    assert record.parents(_PANE, 250.0) == [_HUB]


def test_record_is_bounded_least_recently_recorded_first_out() -> None:
    record = pp.PanePorts()
    now = 0.0
    for parent in range(pp.MAX_PARENTS_PER_PORT + 1):
        record.record(_PANE, 6000 + parent, 100.0, now)
    assert record.parents(_PANE, now) == [6000 + p for p in range(1, pp.MAX_PARENTS_PER_PORT + 1)]
    for port in range(pp.MAX_PANE_PORTS + 1):
        record.record(8000 + port, _HUB, 100.0, now)
    assert record.parents(_PANE, now) == []
    assert record.parents(8000, now) == []
    assert record.parents(8000 + pp.MAX_PANE_PORTS, now) == [_HUB]


@pytest.mark.asyncio
async def test_a_pane_token_records_its_port() -> None:
    app = _app()
    await pp.note_pane_request(_request(app, f"localhost:{_PANE}"), _claims())
    assert pp.pane_ports(app).parents(_PANE, time.time()) == [_HUB]


@pytest.mark.parametrize(
    ("host", "headers", "token_extra", "why"),
    [
        (f"crew.example.com:{_PANE}", {}, {}, "not a dashboard loopback name"),
        (f"[::1]:{_PANE}", {}, {}, "IPv6 loopback cannot be a frame ancestor"),
        (f"localhost:{_PANE}", {"X-Forwarded-For": "203.0.113.9"}, {}, "proxied"),
        (f"localhost:{_PANE}", {"Forwarded": "for=203.0.113.9"}, {}, "proxied"),
        (f"localhost:{_PANE}", {}, {"embed_forward_port": "7779"}, "bound to another port"),
    ],
)
@pytest.mark.asyncio
async def test_nothing_else_records(
    host: str, headers: dict[str, str], token_extra: dict[str, str], why: str
) -> None:
    app = _app()
    await pp.note_pane_request(_request(app, host, **headers), _claims(**token_extra))
    assert pp.pane_ports(app)._ports == {}, why


@pytest.mark.asyncio
async def test_a_token_without_a_parent_claim_records_nothing() -> None:
    app = _app()
    plain = generate_token(
        "owner", ttl_seconds=_TOKEN_TTL_SECS, register_nonce=False, session=True
    )  # flake-ok: a credential lifetime, not a wait
    await pp.note_pane_request(_request(app, f"localhost:{_PANE}"), plain)
    assert pp.pane_ports(app)._ports == {}


@pytest.mark.asyncio
async def test_a_pane_on_one_of_this_gateways_dashboard_ports_still_records() -> None:
    # The hub picks its forward port on its own machine, so it can equal ours.
    app = _app()
    await pp.note_pane_request(_request(app, f"localhost:{_PORT}"), _claims())
    assert pp.pane_ports(app).parents(int(_PORT), time.time()) == [_HUB]


@pytest.mark.asyncio
async def test_a_host_without_a_port_records_the_http_default() -> None:
    # A browser leaves :80 out of Host, and the router reads that as port 80.
    app = _app()
    await pp.note_pane_request(_request(app, "localhost"), _claims())
    assert pp.pane_ports(app).parents(80, time.time()) == [_HUB]


@pytest.mark.asyncio
async def test_a_forward_claim_records_the_port_it_names() -> None:
    app = _app()
    token = _claims(embed_forward_port=str(_PANE))
    await pp.note_pane_request(_request(app, f"127.0.0.1:{_PANE}"), token)
    assert pp.pane_ports(app).parents(_PANE, time.time()) == [_HUB]


@pytest.mark.asyncio
async def test_each_new_record_is_audited_once(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **fields: Any) -> None:
            rows.append(fields)

    monkeypatch.setattr(pp, "sel", lambda: _Sel())
    monkeypatch.setattr(pp, "sel_is_warm", lambda: True)
    app = _app()
    token = _claims()
    for _ in range(3):
        await pp.note_pane_request(_request(app, f"localhost:{_PANE}"), token)
    assert [(r["operation"], r["resources"]) for r in rows] == [
        ("record pane port", f"pane port {_PANE} for parent port {_HUB} (from Host)")
    ]


# ── the token layer records, the router serves ───────────────────────────────


async def _backend_handler(request: web.Request) -> web.Response:
    return web.Response(text="framed page", content_type="text/html")


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[int]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", _backend_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield runner.addresses[0][1]
    finally:
        await runner.cleanup()


def _gateway_app(
    backend: int, monkeypatch: pytest.MonkeyPatch, ssm_gate: GateRef | None = None
) -> web.Application:
    """The router outermost, then the real token layer, then the dashboard."""

    async def _port(name: str) -> int | None:
        return backend if name == "demo" else None

    monkeypatch.setattr(awo, "_backend_port", _port)

    async def dashboard(_request: web.Request) -> web.Response:
        return web.Response(text="dashboard")

    app = web.Application(
        middlewares=[awo.build_app_web_origin_middleware(ssm_gate), token_auth_middleware()]
    )
    app["allowed_origins"] = set(_ALLOWED)
    app["port"] = int(_PORT)
    app.router.add_route("*", "/{path:.*}", dashboard)
    return app


@pytest_asyncio.fixture
async def gateway(backend: int, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(_gateway_app(backend, monkeypatch), host="127.0.0.1"))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _ancestors(resp: Any) -> str:
    (value,) = [v for v in resp.headers.getall("Content-Security-Policy") if "frame-ancestors" in v]
    return value


@pytest.mark.asyncio
async def test_a_pane_port_is_served_once_its_token_signs_in(gateway: TestClient) -> None:
    app_host = f"demo.apps.localhost:{_PANE}"
    before = await gateway.get("/", headers={"Host": app_host})
    assert before.status == 421

    pane = await gateway.get(f"/?token={_pane_token()}", headers={"Host": f"localhost:{_PANE}"})
    assert await pane.text() == "dashboard"

    after = await gateway.get("/", headers={"Host": app_host})
    assert after.status == 200
    assert await after.text() == "framed page"
    pane_origins = [f"http://{h}:{_PANE}" for h in ("127.0.0.1", "localhost", "kirocrew.localhost")]
    hub_origins = [f"http://{h}:{_HUB}" for h in ("127.0.0.1", "localhost", "kirocrew.localhost")]
    assert _ancestors(after) == "frame-ancestors " + " ".join(
        ["'self'", *pane_origins, *hub_origins]
    )


@pytest.mark.asyncio
async def test_a_pane_on_a_dashboard_port_adds_its_parents_to_the_ancestors(
    gateway: TestClient,
) -> None:
    app_host = f"demo.apps.localhost:{_PORT}"
    hub_origins = [f"http://{h}:{_HUB}" for h in ("127.0.0.1", "localhost", "kirocrew.localhost")]
    before = await gateway.get("/", headers={"Host": app_host})
    assert before.status == 200
    assert not any(o in _ancestors(before) for o in hub_origins)

    pane = await gateway.get(f"/?token={_pane_token()}", headers={"Host": f"localhost:{_PORT}"})
    assert await pane.text() == "dashboard"

    after = await gateway.get("/", headers={"Host": app_host})
    assert after.status == 200
    assert _ancestors(after).startswith(_ancestors(before))
    assert all(o in _ancestors(after).split() for o in hub_origins)


@pytest.mark.asyncio
async def test_a_pane_port_keeps_every_other_refusal(gateway: TestClient) -> None:
    await gateway.get(f"/?token={_pane_token()}", headers={"Host": f"localhost:{_PANE}"})
    app_host = f"demo.apps.localhost:{_PANE}"
    forwarded = await gateway.get("/", headers={"Host": app_host, "X-Forwarded-For": "203.0.113.9"})
    assert forwarded.status == 421
    unknown = await gateway.get("/", headers={"Host": f"other.apps.localhost:{_PANE}"})
    assert unknown.status == 404
    other_port = await gateway.get("/", headers={"Host": f"demo.apps.localhost:{_PANE + 1}"})
    assert other_port.status == 421


@pytest.mark.asyncio
async def test_an_expired_record_is_refused(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await gateway.get(f"/?token={_pane_token()}", headers={"Host": f"localhost:{_PANE}"})
    later = time.time() + 7200
    monkeypatch.setattr(awo, "time", SimpleNamespace(time=lambda: later))
    resp = await gateway.get("/", headers={"Host": f"demo.apps.localhost:{_PANE}"})
    assert resp.status == 421


@pytest.mark.asyncio
async def test_the_session_manager_listener_never_reaches_a_pane_port(
    backend: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = GateRef()
    client = TestClient(TestServer(_gateway_app(backend, monkeypatch, ref), host="127.0.0.1"))
    await client.start_server()
    try:
        runner = client.server.runner
        assert runner is not None
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        ssm_port = next(addr[1] for addr in runner.addresses if addr[1] != client.port)
        ref.gate = SsmGate(port=ssm_port)
        pp.pane_ports(client.app).record(_PANE, _HUB, time.time() + 60, time.time())
        async with client.session.get(
            f"http://127.0.0.1:{ssm_port}/", headers={"Host": f"demo.apps.localhost:{_PANE}"}
        ) as resp:
            assert resp.status == 421
    finally:
        await client.close()


# ── the forward port claim (B+) is minted and carried ────────────────────────


def _payload(token: str) -> dict[str, Any]:
    from kiro_crew.dashboard.token_auth_tokens import _b64url_decode

    return dict(json.loads(_b64url_decode(token.split(".", 1)[0])))


@pytest.mark.asyncio
async def test_the_session_exchange_carries_the_forward_claim() -> None:
    from token_auth_helpers import _make_request, _ok_handler

    from kiro_crew.dashboard.refresh_tokens import (
        refresh_cookie_name,
        refresh_token_embed_forward_port,
    )

    token = _pane_token(embed_forward_port=str(_PANE))
    resp = await token_auth_middleware()(
        _make_request(query={"token": token}, remote="10.0.0.1"), _ok_handler
    )
    assert resp.status == 200
    session = resp.cookies[f"mc_token_{_PORT}"].value
    assert _payload(session)["embed_forward_port"] == str(_PANE)
    refresh = resp.cookies[refresh_cookie_name(_PORT)].value
    assert refresh_token_embed_forward_port(refresh) == str(_PANE)


@pytest.mark.parametrize(
    ("query", "claims"),
    [
        (
            f"embed_parent_port={_HUB}&embed_forward_port={_PANE}",
            {"embed_parent_port": str(_HUB), "embed_forward_port": str(_PANE)},
        ),
        (f"embed_parent_port={_HUB}&embed_forward_port=99999", {"embed_parent_port": str(_HUB)}),
        # A forward port means nothing without the parent it pairs with.
        (f"embed_forward_port={_PANE}", {}),
    ],
)
@pytest.mark.asyncio
async def test_the_local_token_endpoint_mints_the_forward_claim(
    monkeypatch: pytest.MonkeyPatch, query: str, claims: dict[str, str]
) -> None:
    from test_source_providers_endpoints import _pin_local_owner_gate

    from kiro_crew.dashboard.handlers import core

    generate = mock.MagicMock(return_value="owner-token")
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: mock.MagicMock())
    _pin_local_owner_gate(monkeypatch)
    app = web.Application()
    app["local_secret"] = "local-secret"
    app["state"] = SimpleNamespace(owner_id="U_OWNER")
    app.router.add_get("/api/token/local", core.api_token_local)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/api/token/local?ttl=15m&{query}", headers={"X-Local-Secret": "local-secret"}
        )
        assert resp.status == 200
    assert generate.call_args.kwargs["extra"] == {**claims, "owner_mint": "1"}


def test_the_hub_sends_the_forward_port_where_an_older_cli_ignores_it() -> None:
    from kiro_crew.instances.token_mint import build_remote_token_command

    command = build_remote_token_command(
        "",
        ttl=_MINT_TTL,  # flake-ok: a credential lifetime, not a wait
        port=7777,
        embed_parent_port=_HUB,
        embed_forward_port=_PANE,
    )
    assert command.startswith(
        f"KIROCREW_EMBED_FORWARD_PORT={_PANE}; export KIROCREW_EMBED_FORWARD_PORT; "
    )
    assert "--embed-forward-port" not in command
    assert f"--embed-parent-port {_HUB}" in command
    # No parent, no forward claim: the two are one statement.
    assert "KIROCREW_EMBED_FORWARD_PORT" not in build_remote_token_command(
        "",
        ttl=_MINT_TTL,  # flake-ok: a credential lifetime, not a wait
        port=7777,
        embed_forward_port=_PANE,
    )


@pytest.mark.parametrize(
    ("parent", "env", "query"),
    [
        (_HUB, str(_PANE), f"&embed_parent_port={_HUB}&embed_forward_port={_PANE}"),
        (_HUB, "", f"&embed_parent_port={_HUB}"),
        (_HUB, "0", f"&embed_parent_port={_HUB}"),
        (_HUB, "x", f"&embed_parent_port={_HUB}"),
        (_HUB, "70000", f"&embed_parent_port={_HUB}"),
        (None, str(_PANE), ""),
    ],
)
def test_the_cli_asks_for_the_forward_claim_beside_its_parent(
    monkeypatch: pytest.MonkeyPatch, parent: int | None, env: str, query: str
) -> None:
    import argparse

    from kiro_crew import cli_server_requests as csr

    monkeypatch.setenv(csr.EMBED_FORWARD_PORT_ENV, env)
    args = argparse.Namespace(embed_parent_port=parent)
    assert csr._embed_query(args) == query


def test_an_ssh_mint_names_the_open_forward_port() -> None:
    from kiro_crew.instances.ssh_tunnel_manager_transport import _forward_port_kwarg

    tunnels = {"crew": SimpleNamespace(status=SimpleNamespace(local_port=_PANE))}
    assert _forward_port_kwarg(tunnels, "crew") == {"embed_forward_port": _PANE}
    assert _forward_port_kwarg(tunnels, "other") == {}
    closed = {"crew": SimpleNamespace(status=SimpleNamespace(local_port=0))}
    assert _forward_port_kwarg(closed, "crew") == {}


def test_mints_for_our_own_panes_name_the_forward_port_and_a_lent_one_does_not(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connect mint and an own-page mint know where our pane loads the crew;
    a mint lent to another gateway's pane does not, so it carries no claim."""
    import asyncio

    from remote_crew_chaining_helpers import _mgr

    seen: list[int | None] = []

    async def mint(host: str, **kwargs: Any) -> str:
        seen.append(kwargs.get("embed_forward_port"))
        return "TOKEN"

    reg, mgr = _mgr(tmp_path, monkeypatch, mint=mint)
    reg.add(name="C", ssh_host="c-host", instance_id="c")
    asyncio.run(mgr.connect("c"))
    status = mgr.status("c")
    assert status is not None
    hop = status.local_port
    assert hop > 0
    assert asyncio.run(mgr.mint_embed_token("c", 5477, own_page=True))[0] is True
    assert asyncio.run(mgr.mint_embed_token("c", 5477))[0] is True
    assert seen == [hop, hop, None]


def test_an_ssh_mint_passes_the_forward_port_to_the_remote_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from kiro_crew.instances import token_mint

    seen: dict[str, Any] = {}

    def capture(remote_bin: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        raise token_mint.TokenMintError("stop before ssh")

    monkeypatch.setattr(token_mint, "build_remote_token_command", capture)
    with pytest.raises(token_mint.TokenMintError):
        asyncio.run(
            token_mint.mint_remote_token(
                "crew-host", remote_port=7777, embed_parent_port=_HUB, embed_forward_port=_PANE
            )
        )
    assert (seen["embed_parent_port"], seen["embed_forward_port"]) == (_HUB, _PANE)


@pytest.mark.asyncio
async def test_a_grant_on_a_pane_port_says_so_in_its_row(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **fields: Any) -> None:
            rows.append(fields)

    await gateway.get(f"/?token={_pane_token()}", headers={"Host": f"localhost:{_PANE}"})
    monkeypatch.setattr(awo, "sel", lambda: _Sel())
    monkeypatch.setattr(awo, "sel_is_warm", lambda: True)
    resp = await gateway.get("/doc", headers={"Host": f"demo.apps.localhost:{_PANE}"})
    assert resp.status == 200
    assert [(r["outcome"], r["resources"]) for r in rows] == [
        ("granted", f"demo.apps.localhost:{_PANE}/doc (pane port)")
    ]
