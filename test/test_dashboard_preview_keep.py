"""The person's Keep and Discard on a staged dashboard page, from the Dashboard tab.

``POST /api/members/{slug}/dashboard/preview/apply`` installs exactly the staged page the
preview read handed out (named by its ``staged_ms``), through the same store writer as
the agent's ``dashboard_apply``. ``.../preview/discard`` drops the staged page. Both are
person-only: an internal-secret caller is refused, then the owner gate, as for the lock.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "fleet-conductor"
MEMBER = "Fleet Conductor"
PAGE = '<div><b data-dashboard-field="credits"></b></div>'
OTHER_PAGE = '<section><b data-dashboard-field="credits"></b></section>'
Q = f"?member={MEMBER.replace(' ', '+')}"
KEEP_URL = f"/api/members/{SLUG}/dashboard/preview/apply{Q}"
DISCARD_URL = f"/api/members/{SLUG}/dashboard/preview/discard{Q}"
READ_URL = f"/api/members/{SLUG}/dashboard{Q}"


def _manifest(template_id: str) -> dict[str, Any]:
    return {
        "id": template_id,
        "version": 1,
        "title": template_id,
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
        },
    }


async def _none(*_a: Any, **_k: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """An isolated data home, two fixture built-ins, the member checks and owner gate open."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = tmp_path / "builtin"
    for template_id, page in (("fixture-board", PAGE), ("fixture-other", OTHER_PAGE)):
        directory = root / template_id
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(
            json.dumps(_manifest(template_id)), encoding="utf-8"
        )
        (directory / "template.html").write_text(page, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: root)
    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(
        routes.members_mod, "canonical_member_key", lambda name, cfg=None: name
    )
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    _floor_monkeypatch.setattr(routes, "_render", lambda *a, **k: None)
    yield


@asynccontextmanager
async def _client():
    app = web.Application()
    frames: list[tuple[str, dict[str, Any]]] = []
    app["state"] = SimpleNamespace(broadcast_ws=lambda kind, data: frames.append((kind, data)))
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c, frames
    finally:
        await c.close()


def _stage(template_id: str = "fixture-other") -> int:
    return instance.stage_preview(SLUG, template_id=template_id).staged_ms


@pytest.mark.asyncio
async def test_the_preview_read_names_the_staged_page() -> None:
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    async with _client() as (client, _):
        body = await (await client.get(READ_URL + "&preview=1")).json()
    assert body["preview"] is True
    assert body["staged_ms"] == staged_ms


@pytest.mark.asyncio
async def test_keep_installs_the_staged_page_and_says_the_page_changed() -> None:
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    async with _client() as (client, frames):
        resp = await client.post(KEEP_URL, json={"staged_ms": staged_ms})
        assert resp.status == 200
        body = await resp.json()
    assert (body["ok"], body["template_id"], body["instance_version"]) == (True, "fixture-other", 2)
    current = instance.read(SLUG)
    assert (current.template_id, current.html) == ("fixture-other", OTHER_PAGE)
    assert instance.staged_preview(SLUG) is None, "Keep cannot be answered twice"
    assert frames == [("dashboard_instance_changed", {"slug": SLUG})]


@pytest.mark.asyncio
async def test_keep_refuses_a_page_staged_after_the_one_shown(_floor_monkeypatch) -> None:
    instance.adopt(SLUG, "fixture-board")
    shown = _stage("fixture-other")
    # The agent stages again before the click, at a later stamp.
    _floor_monkeypatch.setattr(instance, "_now_ms", lambda: shown + 1)
    newer = _stage("fixture-board")
    assert newer != shown
    async with _client() as (client, _):
        resp = await client.post(KEEP_URL, json={"staged_ms": shown})
        assert resp.status == 409
        assert (await resp.json())["code"] == "preview_changed"
    assert instance.read(SLUG).instance_version == 1
    assert instance.staged_preview(SLUG) is not None, "the newer page stays staged"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [KEEP_URL, DISCARD_URL])
async def test_a_page_staged_in_the_same_millisecond_is_not_the_one_shown(
    url: str, _floor_monkeypatch
) -> None:
    """Two stagings that read one clock tick still name two pages, so a click on the
    first is refused rather than acting on the replacement nobody saw."""
    instance.adopt(SLUG, "fixture-board")
    frozen = instance._now_ms()
    _floor_monkeypatch.setattr(instance, "_now_ms", lambda: frozen)
    shown = _stage("fixture-other")
    # Restaging after a discard must not hand the old stamp out again either.
    instance.discard_preview(SLUG)
    newer = _stage("fixture-board")
    assert newer > shown
    async with _client() as (client, _):
        resp = await client.post(url, json={"staged_ms": shown})
        assert resp.status == 409
        assert (await resp.json())["code"] == "preview_changed"
    assert instance.read(SLUG).instance_version == 1
    assert instance.staged_preview(SLUG).staged_ms == newer, "the newer page stays staged"


@pytest.mark.asyncio
async def test_keep_refuses_when_the_layout_is_locked() -> None:
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    instance.set_structure_lock(SLUG, True)
    async with _client() as (client, _):
        resp = await client.post(KEEP_URL, json={"staged_ms": staged_ms})
        assert resp.status == 423
        assert (await resp.json())["code"] == "dashboard_locked"
    assert instance.read(SLUG).html == PAGE
    assert instance.staged_preview(SLUG) is not None


@pytest.mark.asyncio
async def test_keep_refuses_when_nothing_is_staged() -> None:
    instance.adopt(SLUG, "fixture-board")
    async with _client() as (client, _):
        resp = await client.post(KEEP_URL, json={"staged_ms": 123})
        assert resp.status == 404
        assert (await resp.json())["code"] == "no_preview"
    assert instance.read(SLUG).instance_version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"staged_ms": "1"}, {"staged_ms": True}, {"staged_ms": 0}])
async def test_keep_needs_the_staged_page_it_was_drawn_beside(body: dict[str, Any]) -> None:
    instance.adopt(SLUG, "fixture-board")
    _stage()
    async with _client() as (client, _):
        resp = await client.post(KEEP_URL, json=body)
        assert resp.status == 400
    assert instance.read(SLUG).instance_version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [KEEP_URL, DISCARD_URL])
async def test_an_internal_secret_caller_is_refused(url: str) -> None:
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    async with _client() as (client, _):
        resp = await client.post(
            url, json={"staged_ms": staged_ms}, headers={"X-Internal-Secret": "s"}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "human_only"
    assert instance.read(SLUG).instance_version == 1
    assert instance.staged_preview(SLUG) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [KEEP_URL, DISCARD_URL])
async def test_a_non_owner_is_refused(url: str, _floor_monkeypatch) -> None:
    async def deny(_request: Any, _operation: str) -> web.Response:
        return web.json_response({"error": "no", "code": "owner_only"}, status=403)

    _floor_monkeypatch.setattr(routes, "_owner_only", deny)
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    async with _client() as (client, _):
        resp = await client.post(url, json={"staged_ms": staged_ms})
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
    assert instance.read(SLUG).instance_version == 1
    assert instance.staged_preview(SLUG) is not None


@pytest.mark.asyncio
async def test_the_real_owner_gate_guards_keep(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    _floor_monkeypatch.setattr(routes, "_owner_only", require_owner_dashboard_request)

    @asynccontextmanager
    async def as_user(user: str):
        @web.middleware
        async def stamp(request: web.Request, handler: Any) -> web.StreamResponse:
            request["user"] = user
            request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[stamp])
        app["state"] = SimpleNamespace(owner_id="owner-subject", broadcast_ws=lambda *_a: None)
        routes.register_member_dashboard_routes(app)
        c = TestClient(TestServer(app))
        await c.start_server()
        try:
            yield c
        finally:
            await c.close()

    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    async with as_user("someone-else") as client:
        assert (await client.post(KEEP_URL, json={"staged_ms": staged_ms})).status == 403
    assert instance.read(SLUG).instance_version == 1
    async with as_user("owner-subject") as client:
        assert (await client.post(KEEP_URL, json={"staged_ms": staged_ms})).status == 200
    assert instance.read(SLUG).template_id == "fixture-other"


@pytest.mark.asyncio
async def test_discard_drops_the_staged_page_and_leaves_the_live_one() -> None:
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    async with _client() as (client, frames):
        resp = await client.post(DISCARD_URL, json={"staged_ms": staged_ms})
        assert resp.status == 200
        # Idempotent: a page that already went is still "not that one".
        assert (await client.post(DISCARD_URL, json={"staged_ms": staged_ms})).status == 200
    assert instance.staged_preview(SLUG) is None
    assert instance.read(SLUG).html == PAGE
    assert frames[0] == ("dashboard_instance_changed", {"slug": SLUG})


@pytest.mark.asyncio
async def test_discard_keeps_a_page_staged_after_the_one_shown(_floor_monkeypatch) -> None:
    instance.adopt(SLUG, "fixture-board")
    shown = _stage("fixture-other")
    _floor_monkeypatch.setattr(instance, "_now_ms", lambda: shown + 1)
    _stage("fixture-board")
    async with _client() as (client, frames):
        resp = await client.post(DISCARD_URL, json={"staged_ms": shown})
        assert resp.status == 409
        assert (await resp.json())["code"] == "preview_changed"
    assert instance.staged_preview(SLUG) is not None, "the unseen page stays staged"
    assert frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"staged_ms": "1"}, {"staged_ms": 0}])
async def test_discard_needs_the_staged_page_it_was_drawn_beside(body: dict[str, Any]) -> None:
    instance.adopt(SLUG, "fixture-board")
    _stage()
    async with _client() as (client, _):
        assert (await client.post(DISCARD_URL, json=body)).status == 400
    assert instance.staged_preview(SLUG) is not None


@pytest.mark.asyncio
async def test_discard_does_not_claim_success_when_the_page_stays_staged(
    _floor_monkeypatch,
) -> None:
    """``discard_preview`` swallows a failed delete; the route must not say "Discarded"."""
    instance.adopt(SLUG, "fixture-board")
    staged_ms = _stage()
    _floor_monkeypatch.setattr(instance, "discard_preview", lambda slug: None)
    async with _client() as (client, frames):
        resp = await client.post(DISCARD_URL, json={"staged_ms": staged_ms})
        assert resp.status == 500
        assert (await resp.json())["code"] == "dashboard_failed"
    assert instance.staged_preview(SLUG) is not None
    assert frames == []


@pytest.mark.asyncio
async def test_a_bound_composed_package_never_reaches_keep(_floor_monkeypatch) -> None:
    """Template previews only. A crewmate bound to a composed package has no staged
    package to keep on this build: the preview read is the template one or nothing,
    and Keep installs only a staged TEMPLATE page."""
    served: list[str] = []

    async def no_package(*_a: Any, **_k: Any) -> web.Response:
        served.append("package")
        return web.json_response({"error": "package served", "code": "package"}, status=500)

    from kiro_crew.artifact_store import dashboard_package

    # The crewmate IS bound to a package: the resolver the read route asks says so.
    _floor_monkeypatch.setattr(dashboard_package, "resolve_bound_slug", lambda owner: "crew-board")
    _floor_monkeypatch.setattr(routes, "_serve_package", no_package)
    instance.adopt(SLUG, "fixture-board")
    async with _client() as (client, _):
        # Nothing staged: the preview read says so instead of serving the package.
        read = await client.get(READ_URL + "&preview=1")
        assert read.status == 404
        assert (await read.json())["code"] == "no_preview"
        keep = await client.post(KEEP_URL, json={"staged_ms": 123})
        assert keep.status == 404
        assert (await keep.json())["code"] == "no_preview"
        # A staged template is what the preview shows, and what Keep installs.
        staged_ms = _stage()
        body = await (await client.get(READ_URL + "&preview=1")).json()
        assert (body["staged_ms"], body["template"]["id"]) == (staged_ms, "fixture-other")
        assert (await client.post(KEEP_URL, json={"staged_ms": staged_ms})).status == 200
    assert served == [], "the package page was never what Keep was drawn beside"
    assert instance.read(SLUG).template_id == "fixture-other"


def test_the_deferred_binder_registers_both_routes() -> None:
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/server_runtime/mcp_routes.py"
    ).read_text(encoding="utf-8")
    for path, name in (
        ("preview/apply", "api_member_dashboard_preview_apply"),
        ("preview/discard", "api_member_dashboard_preview_discard"),
    ):
        assert f'"/api/members/{{slug}}/dashboard/{path}"' in text
        assert f'_deferred("member_dashboard", "{name}")' in text
