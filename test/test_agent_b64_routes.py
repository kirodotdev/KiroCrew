"""The base64url crew routes: a name a proxy would mangle never rides the path.

``PUT /api/agents`` takes the name in the body. Every other
crew route that carries the name in its path also answers under
``/api/agents/b64/{name_b64}...``, which spells the name in ``[A-Za-z0-9_-]``
only, so a path-normalising proxy has no ``%20`` to decode into a bare space.
These tests pin the encoding, that each twin reaches the same handler behind
the same owner gate, and that change cards build and recognise the new paths.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew import change_card_catalog as catalog
from kiro_crew.agent_url_path import (
    B64_ROUTE_ALIASES,
    agent_path,
    agent_record_write,
    decode_agent_name,
    encode_agent_name,
    needs_proxy_safe_path,
)
from kiro_crew.dashboard.agent_name_segment import agent_route_name
from kiro_crew.dashboard.handlers import change_cards
from kiro_crew.dashboard.routes import agents as agents_routes

# ── the encoding ──


@pytest.mark.parametrize("name", ["Nova Sky", "a/b", "café", "名前 テスト", "b64", "x"])
def test_encode_decode_round_trip_and_alphabet(name):
    seg = encode_agent_name(name)
    assert seg and set(seg) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert decode_agent_name(seg) == name


@pytest.mark.parametrize(
    "segment",
    [
        "",  # empty
        "Tm92YSBTa3k=",  # padded: not the canonical spelling
        "Tm92YSBTa3l",  # last character carries non-zero spare bits
        "A",  # a length no base64 text has
        "Tm92 YSBTa3k",  # outside the alphabet
        "Tm92YSBTa3k/",  # standard base64, not base64url
        "_w",  # bytes that are not UTF-8
    ],
)
def test_decode_refuses_every_non_canonical_segment(segment):
    assert decode_agent_name(segment) is None


def test_agent_path_keeps_plain_names_in_path_form():
    assert agent_path("scout") == "/api/agents/scout"
    assert agent_path("scout", "/avatar") == "/api/agents/scout/avatar"
    assert not needs_proxy_safe_path("scout")
    # A crew literally named ``b64`` still gets its path-form routes.
    assert agent_path("b64", "/capabilities") == "/api/agents/b64/capabilities"


def test_agent_path_moves_an_encoded_name_into_a_b64_segment():
    path = agent_path("Nova Sky", "/worker-kit")
    assert path == f"/api/agents/b64/{encode_agent_name('Nova Sky')}/worker-kit"
    assert "%" not in path and " " not in path


def test_record_write_puts_an_encoded_name_in_the_body():
    assert agent_record_write("scout", {"x": 1}) == ("/api/agents/scout", {"x": 1})
    assert agent_record_write("Nova Sky", {"x": 1}) == (
        "/api/agents",
        {"x": 1, "name": "Nova Sky"},
    )


def test_route_name_reads_either_spelling():
    plain = SimpleNamespace(match_info={"name": "scout"})
    b64 = SimpleNamespace(match_info={"name_b64": encode_agent_name("Nova Sky")})
    bad = SimpleNamespace(match_info={"name_b64": "Tm92YSBTa3k="})
    assert agent_route_name(plain) == "scout"  # type: ignore[arg-type]
    assert agent_route_name(b64) == "Nova Sky"  # type: ignore[arg-type]
    assert agent_route_name(bad) is None  # type: ignore[arg-type]


# ── the route table ──


class _FakeState:
    owner_id = ""


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agents_routes.register(app)
    return app


def _routes(app: web.Application) -> dict[tuple[str, str], object]:
    return {
        (r.method, r.resource.canonical): r.handler
        for r in app.router.routes()
        if r.resource is not None and r.method != "HEAD"
    }


def test_every_b64_route_reaches_its_path_form_twins_handler():
    table = _routes(_build_app())
    b64 = {k: h for k, h in table.items() if k[1].startswith("/api/agents/b64/")}
    # Every alias is registered, and every registered b64 route has an alias.
    assert {t for _, t in b64} == set(B64_ROUTE_ALIASES)
    assert len(b64) == 10
    for (method, template), handler in b64.items():
        twin = (method, B64_ROUTE_ALIASES[template])
        assert twin in table, f"{method} {template} has no path-form twin"
        assert table[twin] is handler, f"{method} {template} reaches a different handler"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/agents/b64/avatar", "/api/agents/{name}/avatar"),
        ("PUT", "/api/agents/b64/capabilities", "/api/agents/{name}/capabilities"),
        ("DELETE", "/api/agents/b64", "/api/agents/{name}"),
        ("GET", "/api/agents/b64/Tm92YSBTa3k/avatar", "/api/agents/b64/{name_b64}/avatar"),
    ],
)
async def test_a_crew_named_b64_keeps_its_path_form_routes(method, path, expected):
    app = _build_app()
    match = await app.router.resolve(make_mocked_request(method, path, app=app))
    assert match.route.resource is not None
    assert match.route.resource.canonical == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,suffix",
    [("DELETE", ""), ("GET", "/avatar"), ("GET", "/worker-kit"), ("POST", "/capabilities/detach")],
)
async def test_an_owner_with_a_malformed_segment_gets_400(method, suffix):
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, f"/api/agents/b64/Tm92YSBTa3k={suffix}")
        assert resp.status == 400
        assert (await resp.json())["code"] == "name_segment_invalid"


def _overlap(a: list[str], b: list[str]) -> bool:
    """Whether two segment patterns can match one path (``{...}`` matches any segment)."""
    if len(a) != len(b):
        return False
    return all(x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b))


def test_no_b64_twin_shadows_a_path_form_route():
    # aiohttp tries the longer static prefix `/api/agents/b64` first, so the
    # only thing keeping a crew named `b64` on its own routes is that no twin
    # can match a path a path-form route of the same method matches.
    table = _routes(_build_app())
    twins = [k for k in table if k[1].startswith("/api/agents/b64/")]
    others = [k for k in table if k[1].startswith("/api/agents/") and k not in twins]
    for method, twin in twins:
        for other_method, other in others:
            if other_method != method:
                continue
            assert not _overlap(twin.split("/"), other.split("/")), (method, twin, other)


@pytest.mark.asyncio
async def test_every_b64_route_refuses_a_non_owner():
    app = _build_app()
    seg = encode_agent_name("Nova Sky")
    b64 = [k for k in _routes(app) if k[1].startswith("/api/agents/b64/")]
    async with TestClient(TestServer(app)) as client:
        for method, template in sorted(b64):
            path = template.replace("{name_b64}", seg)
            resp = await client.request(method, path, headers={"X-Test-User": "someone-else"})
            assert resp.status == 403, f"{method} {template} answered {resp.status}"


# ── change cards ──


def test_hook_reads_a_b64_route_as_its_path_form_template():
    hooked_b64 = {k for k in catalog.HOOKED_ROUTES if k[1].startswith("/api/agents/b64/")}
    assert hooked_b64 == {
        ("DELETE", "/api/agents/b64/{name_b64}"),
        ("POST", "/api/agents/b64/{name_b64}/capabilities/preview"),
        ("PUT", "/api/agents/b64/{name_b64}/capabilities"),
    }
    for method, template in hooked_b64:
        twin = (method, B64_ROUTE_ALIASES[template])
        assert twin in catalog.HOOKED_ROUTES
        request = SimpleNamespace(
            method=method,
            match_info=SimpleNamespace(
                route=SimpleNamespace(resource=SimpleNamespace(canonical=template))
            ),
        )
        assert change_cards.route_key(request) == twin  # type: ignore[arg-type]
    # The preview is a read on either spelling, so neither writes a memory line.
    assert ("POST", "/api/agents/b64/{name_b64}/capabilities/preview") not in catalog.MEMORY_ROUTES


def test_hook_decodes_the_b64_name_for_the_memory_line():
    class _Info(dict):
        route = None

    request = SimpleNamespace(match_info=_Info(name_b64=encode_agent_name("Nova Sky")))
    assert change_cards.route_match_info(request) == {"name": "Nova Sky"}  # type: ignore[arg-type]
    bad = SimpleNamespace(match_info=_Info(name_b64="Tm92YSBTa3k="))
    assert change_cards.route_match_info(bad) == {"name": ""}  # type: ignore[arg-type]


def test_card_plans_never_put_an_encoded_name_in_the_path():
    seg = encode_agent_name("Nova Sky")
    p = catalog.validate_params(
        "crewmate.update", {"name": "Nova Sky", "fields": {"description": "new"}}
    )
    snapshot = {"exists": True, "fields": {"description": "old"}}
    apply = catalog.build_preview("crewmate.update", p, snapshot, {})["apply"]
    assert [(s["method"], s["path"]) for s in apply] == [("PUT", "/api/agents")]
    assert apply[0]["body"]["name"] == "Nova Sky"
    undo, reason = catalog.build_undo("crewmate.update", p, snapshot, [{}], applied_steps=1)
    assert reason is None
    assert [(s["method"], s["path"]) for s in undo] == [("PUT", "/api/agents")]
    assert undo[0]["body"]["name"] == "Nova Sky"

    created = catalog.validate_params("crewmate.create", {"name": "Nova Sky", "goal": "watch"})
    undo, _ = catalog.build_undo(
        "crewmate.create", created, {}, [{"name": "Nova Sky"}], applied_steps=1
    )
    assert undo == [{"method": "DELETE", "path": f"/api/agents/b64/{seg}", "body": None}]

    plan = catalog._capability_plan("Nova Sky", "r1", {})
    assert [s["path"] for s in plan] == [
        f"/api/agents/b64/{seg}/capabilities/preview",
        f"/api/agents/b64/{seg}/capabilities",
    ]


def test_card_plans_keep_plain_names_in_path_form():
    p = catalog.validate_params(
        "crewmate.update", {"name": "scout", "fields": {"description": "n"}}
    )
    snapshot = {"exists": True, "fields": {"description": "o"}}
    apply = catalog.build_preview("crewmate.update", p, snapshot, {})["apply"]
    assert apply[0]["path"] == "/api/agents/scout" and "name" not in apply[0]["body"]
