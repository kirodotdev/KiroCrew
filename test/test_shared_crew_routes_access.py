"""The access card routes refuse before they act, and map each failure to one status.

The owner gate and the access set are stubbed; what is real is each handler's
order of checks and the status it answers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import routes_access as ra


@pytest.fixture(autouse=True)
def _shared_crew_preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover the preview's behaviour; the gate itself has its own tests."""
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


def _req(method: str, state: Any = None) -> web.Request:
    app = web.Application()
    if state is not None:
        app["state"] = state
    req = make_mocked_request(method, "/x", app=app)
    return req


def _body(resp: web.Response) -> dict[str, Any]:
    assert resp.text is not None
    return json.loads(resp.text)


@pytest.fixture()
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(_r: web.Request) -> None:
        return None

    monkeypatch.setattr(ra, "_owner_refusal", allowed)
    monkeypatch.setattr(ra, "_actor", lambda _r: "local:owner")


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [ra.api_access, ra.api_ack])
async def test_a_non_owner_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    async def refuse(_r: web.Request) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(ra, "_owner_refusal", refuse)
    monkeypatch.setattr(access_set, "acknowledge_first_share", pytest.fail)
    resp = await handler(_req("POST"))
    assert resp.status == 403


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (access_set.CODE_ACK_REQUIRED, 409),
        (access_set.CODE_UNREADABLE, 503),
        ("something_else", 400),
    ],
)
def test_access_failures_map_to_one_status(code: str, status: int) -> None:
    resp = ra._access_failure(access_set.AccessError(code))
    assert resp.status == status
    assert _body(resp)["code"] == code


def _raise(code: str) -> Any:
    def fail(*_a: Any) -> Any:
        raise access_set.AccessError(code)

    return fail


@pytest.mark.asyncio
async def test_ack_answers_and_maps_an_unreadable_set(
    owner: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    acked: list[str] = []
    monkeypatch.setattr(access_set, "acknowledge_first_share", acked.append)
    assert _body(await ra.api_ack(_req("POST"))) == {"acknowledged": True}
    assert acked == ["local:owner"]
    monkeypatch.setattr(access_set, "acknowledge_first_share", _raise(access_set.CODE_UNREADABLE))
    assert (await ra.api_ack(_req("POST"))).status == 503


@pytest.mark.asyncio
async def test_counts_read_unknown_when_their_sources_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import memory
    from kiro_crew.shared_crew import routes_identity

    async def broken_rows(_r: web.Request) -> list[dict[str, Any]]:
        raise RuntimeError("catalog unreadable")

    async def broken_store(_state: Any) -> Any:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(routes_identity, "_catalog_rows", broken_rows)
    monkeypatch.setattr(memory, "_get_vector_store_async", broken_store)
    counts = await ra._counts(_req("GET", state=SimpleNamespace()))
    assert counts == {"sessions": None, "memory_items": None}


@pytest.mark.asyncio
async def test_counts_add_semantic_and_episodic_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import memory
    from kiro_crew.shared_crew import routes_identity

    async def rows(_r: web.Request) -> list[dict[str, Any]]:
        return [{"key": "chat-1"}, {"key": "chat-2"}]

    store = SimpleNamespace(memory_stats=lambda: {"semantic_active": 3, "episodic_active": 4})

    async def vector(_state: Any) -> Any:
        return store

    monkeypatch.setattr(routes_identity, "_catalog_rows", rows)
    monkeypatch.setattr(routes_identity, "_machine_only", lambda _k: False)
    monkeypatch.setattr(memory, "_get_vector_store_async", vector)
    counts = await ra._counts(_req("GET", state=SimpleNamespace()))
    assert counts == {"sessions": 2, "memory_items": 7}


@pytest.mark.asyncio
async def test_a_refused_read_is_audited_and_a_refused_write_is_left_to_the_request_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The dashboard's SEL request audit skips GET, so the owner gate records a
    # refused read itself; a refused write is already one record there.
    import kiro_crew.sel as sel_mod
    from kiro_crew.shared_crew import routes_invites as ri

    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sel_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )

    async def refuse(_r: web.Request) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(ri, "_owner_refusal_decision", refuse)
    get = make_mocked_request("GET", "/api/shared-crew/tailscale/invites", app=web.Application())
    assert (await ri._owner_refusal(get)).status == 403
    post = make_mocked_request("POST", "/api/shared-crew/tailscale/invites", app=web.Application())
    assert (await ri._owner_refusal(post)).status == 403
    assert [(r["operation"], r["outcome"]) for r in records] == [
        ("GET /api/shared-crew/tailscale/invites", "denied")
    ]


@pytest.mark.asyncio
async def test_the_identity_and_avatar_reads_audit_a_non_dashboard_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An app token with a /api/shared-crew/* grant reaches these GETs; the 403 it
    # gets is written to SEL, since the request audit does not record a GET.
    import kiro_crew.sel as sel_mod
    from kiro_crew.shared_crew import routes_identity
    from kiro_crew.shared_crew import routes_invites as ri

    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sel_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )
    sessions = make_mocked_request("GET", "/api/shared-crew/sessions", app=web.Application())
    sessions["is_dashboard_user"] = True
    sessions["app"] = "some-app"
    assert (await routes_identity._refusal(sessions)).status == 403
    avatar = make_mocked_request(
        "GET",
        "/api/shared-crew/tailscale/avatars/a1",
        app=web.Application(),
        match_info={"avatar_id": "a1"},
    )
    assert (await ri.api_avatar(avatar)).status == 403
    assert [(r["operation"], r["outcome"], r["source"]) for r in records] == [
        ("GET /api/shared-crew/sessions", "denied", "shared_crew_identity"),
        ("GET /api/shared-crew/tailscale/avatars/a1", "denied", "shared_crew_avatar"),
    ]
