"""The phone widget routes: owner only, behind the ``widgets`` preview, per-reader caching.

Driven over a real loopback listener with the relay ingress, the client-sent header
strip and token auth chained as the gateway orders them (as
``test_members_roster_relay_teammate`` does), so the owner, a relay teammate, another
subject and an unauthenticated caller are told apart by the gateway's own checks and
not by a stub. The package selection and the field read are stubbed so each test can
also say whether a request reached the resolver at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from shared_crew_host_isolation import host_isolation  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import widget_snapshot as ws
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard.handlers import member_widgets as mw
from kiro_crew.dashboard.kiro_relay.caller import CrewKind, HostBinding
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.dashboard_feed import FieldRead
from kiro_crew.shared_crew import owner, people
from kiro_crew.testing.synthetic_credentials import github_classic

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "U0OWNER"
ANA = "kiro-user.teammate-ana-0002"
BOT = "pebble"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "widgets"
NOW = datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc)
SECRET = github_classic()


@pytest.fixture(autouse=True)
def _fresh(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    people.reset_for_tests()
    yield
    people.reset_for_tests()


def _config(*, on: bool = True, names: tuple[str, ...] = (BOT,)) -> Any:
    agents = {
        # A person-created Bot (it has a member id), so a relay teammate's roster
        # reaches it and its Dashboard page admits them.
        name: KiroCrewAgentConfig(
            kiro_agent="kirocrew", source="kirocrew", member_id=f"mem-{i:04d}"
        )
        for i, name in enumerate(names)
    }
    agents[BOT].display_name = "Pebble"
    return SimpleNamespace(
        agents=agents,
        default_agent=BOT,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
        dashboard=SimpleNamespace(feature_previews={"widgets": True} if on else {}),
    )


SLUG = members_mod.member_slug(BOT, _config())


def _package() -> dict[str, Any]:
    return {
        "model": {
            "types": {
                "open_prs": {"type": "number", "source": {"agentic": True}},
                "pipeline": {"type": "text", "source": {"agentic": True}},
            }
        },
        "widgets": [
            {
                "id": "build-health",
                "title": "Build health",
                "sizes": ["small", "medium"],
                "blocks": [
                    {"type": "status", "label": "Pipeline", "field": "pipeline"},
                    {
                        "type": "metric",
                        "label": "Open PRs",
                        "field": "open_prs",
                        "warn_at": 10,
                    },
                ],
                "tap": {"open": "bot"},
            },
            {
                "id": "prs",
                "title": "PRs",
                "sizes": ["small"],
                "blocks": [{"type": "metric", "label": "Open PRs", "field": "open_prs"}],
                "tap": {"open": "bot"},
            },
        ],
    }


def _read() -> FieldRead:
    return FieldRead(
        fields={
            "open_prs": 7,
            "pipeline": "amber",
            "waiting_count": 1,
            "waiting_items": [{"text": "Approve the deploy"}],
            "waiting_omitted": 0,
            "workers_total": 2,
            "_at_waiting": "2026-11-18T00:25:00Z",
        },
        written_at={"open_prs": "2026-11-18T00:29:00Z", "pipeline": "2026-11-18T00:21:00Z"},
    )


class _Resolver:
    """Stubs the package selection and the read, and counts every call to either."""

    def __init__(self, package: dict[str, Any] | None = None, read: FieldRead | None = None):
        self.package = _package() if package is None else package
        self.read = read or _read()
        self.calls = 0

    async def package_in_force(self, slug: str, member: str):
        self.calls += 1
        return self.package, 12

    def read_fields(self, slug: str, member: str, manifest: Any) -> FieldRead:
        self.calls += 1
        return self.read


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> _Resolver:
    stub = _Resolver()
    monkeypatch.setattr(ws, "package_in_force", stub.package_in_force)
    monkeypatch.setattr(md, "read_fields", stub.read_fields)
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "")
    return stub


def _app(tmp_path: Path, register: Any = mw.register_member_widgets_routes) -> web.Application:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM, owner_user_id=""))
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            relay_header_strip_middleware,
            token_auth_middleware(port=5476),
        ]
    )
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    owner.set_owner_subject(OWNER)
    app["state"] = state
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    register(app)

    async def _dashboard_admits(request: web.Request) -> web.Response:
        # The Dashboard page's own teammate check, asked on the same request.
        admitted = await md._teammate_reader(request, request.match_info["slug"], "probe")
        return web.json_response({"admitted": admitted})

    app.router.add_get("/probe/{slug}", _dashboard_admits)
    return app


SNAPSHOT = f"/api/members/{SLUG}/widgets/build-health?member={BOT}"
CATALOG = "/api/widgets"


def _owner() -> dict[str, str]:
    return {"token": generate_token(OWNER)}


def _teammate(client: TestClient) -> Any:
    return client.server.app[RELAY_INGRESS_APP_KEY].stamp_headers([], relay_user=ANA)


async def _get(client: TestClient, path: str, cfg: Any = None, **kwargs: Any):
    with patch.object(KiroCrewConfig, "load", return_value=cfg or _config()):
        resp = await client.get(path, **kwargs)
        return resp, await resp.read()


def _frozen(monkeypatch: pytest.MonkeyPatch, moment: datetime = NOW) -> None:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return moment

    monkeypatch.setattr(mw, "datetime", _Clock)


def _assert_cache_headers(resp: Any) -> None:
    assert resp.headers["Cache-Control"] == "private, no-cache"
    assert resp.headers["ETag"].startswith('"')
    checked = resp.headers["Kiro-Widget-Checked-At"]
    assert datetime.fromisoformat(checked.replace("Z", "+00:00")).tzinfo is not None


# --------------------------------------------------------------------------
# the owner
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_reads_a_snapshot(tmp_path, resolver) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(client, SNAPSHOT, params=_owner())
    assert resp.status == 200, raw
    _assert_cache_headers(resp)
    body = json.loads(raw)
    assert body["bot"] == {"slug": SLUG, "member": BOT, "name": "Pebble"}
    assert body["widget"]["id"] == "build-health"
    assert body["layout_version"] == 12
    assert [b["type"] for b in body["blocks"]] == ["status", "metric"]
    assert body["blocks"][1]["value"] == 7
    assert resp.headers["Kiro-Widget-Checked-At"] == body["generated_at"]


@pytest.mark.asyncio
async def test_the_owner_reads_the_catalog_of_every_bot(tmp_path, resolver) -> None:
    cfg = _config(names=(BOT, "atlas"))
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(client, CATALOG, cfg, params=_owner())
    assert resp.status == 200, raw
    _assert_cache_headers(resp)
    body = json.loads(raw)
    assert body["v"] == 1
    assert [b["member"] for b in body["bots"]] == [BOT, "atlas"]
    pebble = body["bots"][0]
    assert (pebble["slug"], pebble["name"]) == (SLUG, "Pebble")
    assert pebble["widgets"] == [
        {
            "id": "build-health",
            "title": "Build health",
            "sizes": ["small", "medium"],
            "default": False,
            "layout_version": 12,
        },
        {"id": "prs", "title": "PRs", "sizes": ["small"], "default": False, "layout_version": 12},
        {
            "id": "status",
            "title": "Status",
            "sizes": ["small", "medium"],
            "default": True,
            "layout_version": 0,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_a_matching_if_none_match_is_a_304_with_the_same_headers(
    tmp_path, resolver, monkeypatch, path
) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        _frozen(monkeypatch, NOW)
        first, _ = await _get(client, path, params=_owner())
        etag = first.headers["ETag"]
        # Later, with nothing changed: the tag holds although generated_at moved.
        _frozen(monkeypatch, datetime(2026, 11, 18, 0, 45, tzinfo=timezone.utc))
        again, raw = await _get(client, path, params=_owner(), headers={"If-None-Match": etag})
    assert first.status == 200
    assert again.status == 304
    assert raw == b""
    _assert_cache_headers(again)
    assert again.headers["ETag"] == etag
    assert again.headers["Kiro-Widget-Checked-At"] == "2026-11-18T00:45:00Z"


@pytest.mark.asyncio
async def test_a_changed_value_misses_the_old_tag(tmp_path, resolver) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        first, _ = await _get(client, SNAPSHOT, params=_owner())
        resolver.read.fields["open_prs"] = 8
        again, raw = await _get(
            client, SNAPSHOT, params=_owner(), headers={"If-None-Match": first.headers["ETag"]}
        )
    assert again.status == 200
    assert json.loads(raw)["blocks"][1]["value"] == 8
    assert again.headers["ETag"] != first.headers["ETag"]


@pytest.mark.asyncio
async def test_an_unknown_bot_and_an_unknown_widget_are_404s(tmp_path, resolver) -> None:
    other = members_mod.member_slug("nobody", _config())
    async with TestClient(TestServer(_app(tmp_path))) as client:
        bot, bot_raw = await _get(
            client, f"/api/members/{other}/widgets/status?member=nobody", params=_owner()
        )
        widget, widget_raw = await _get(
            client, f"/api/members/{SLUG}/widgets/nope?member={BOT}", params=_owner()
        )
    assert (bot.status, json.loads(bot_raw)["code"]) == (404, "bot_not_found")
    assert (widget.status, json.loads(widget_raw)["code"]) == (404, "widget_not_found")


# --------------------------------------------------------------------------
# everyone else
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_a_relay_teammate_the_dashboard_admits_is_refused_owner_only(
    tmp_path, resolver, path
) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        probe, probe_raw = await _get(client, f"/probe/{SLUG}", headers=_teammate(client))
        resp, raw = await _get(client, path, headers=_teammate(client))
    # The same caller reads this Bot's Dashboard page...
    assert (probe.status, json.loads(probe_raw)) == (200, {"admitted": True})
    # ...and gets no widget, and no Bot list, in v1.
    assert resp.status == 403, raw
    assert json.loads(raw) == {"error": "owner authorization required", "code": "owner_only"}
    assert resolver.calls == 0, "a refused request reached the resolver"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_a_teammate_admitted_by_the_route_check_is_still_refused(
    tmp_path, resolver, monkeypatch, path
) -> None:
    # Even if the Dashboard's per-Bot admission said yes for every request, the widget
    # routes do not ask it.
    monkeypatch.setattr(md, "_teammate_reader", AsyncMock(return_value=True))
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(client, path, headers=_teammate(client))
    assert resp.status == 403
    assert json.loads(raw)["code"] == "owner_only"
    assert resolver.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_another_subject_is_refused_owner_only(tmp_path, resolver, path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(client, path, params={"token": generate_token("U0SOMEONE")})
    assert resp.status == 403, raw
    assert json.loads(raw)["code"] == "owner_only"
    assert resolver.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_an_internal_secret_caller_is_refused(tmp_path, resolver, path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(
            client, path, params=_owner(), headers={"X-Internal-Secret": "anything"}
        )
    assert resp.status in (401, 403), raw
    assert resolver.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_no_widget_data_reaches_a_teammate_or_an_unauthenticated_caller(
    tmp_path, resolver, path
) -> None:
    # A secret and a marker in every value the resolver could read.
    resolver.read.fields.update({"open_prs": 4242, "pipeline": f"marker {SECRET}"})
    resolver.package["widgets"][0]["title"] = "Marker title"
    async with TestClient(TestServer(_app(tmp_path))) as client:
        teammate, teammate_raw = await _get(client, path, headers=_teammate(client))
        anonymous, anonymous_raw = await _get(client, path)
    assert teammate.status == 403
    # Refused by token auth before any handler runs.
    assert anonymous.status in (401, 403), anonymous_raw
    assert json.loads(anonymous_raw).get("code") != "owner_only"
    for raw in (teammate_raw, anonymous_raw):
        text = raw.decode("utf-8")
        for leak in ("4242", "marker", "Marker", SECRET, "build-health", BOT, SLUG, '"bots"'):
            assert leak not in text, (leak, text)
    for resp in (teammate, anonymous):
        assert "ETag" not in resp.headers
        assert "Kiro-Widget-Checked-At" not in resp.headers
    assert resolver.calls == 0


@pytest.mark.asyncio
async def test_a_refusal_is_audited(tmp_path, resolver, monkeypatch) -> None:
    records: list[dict] = []
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(
        sel_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )
    async with TestClient(TestServer(_app(tmp_path))) as client:
        await _get(client, SNAPSHOT, headers=_teammate(client))
    denied = [r for r in records if r.get("operation") == "widgets.snapshot"]
    assert denied == [
        {
            "caller": f"relay:{ANA}",
            "operation": "widgets.snapshot",
            "outcome": "denied",
            "source": "dashboard",
            "resources": "widgets_owner_only",
        }
    ]


# --------------------------------------------------------------------------
# the preview switch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [SNAPSHOT, CATALOG], ids=["snapshot", "catalog"])
async def test_with_the_preview_off_the_routes_answer_widgets_off_and_read_nothing(
    tmp_path, resolver, path
) -> None:
    off = _config(on=False)
    async with TestClient(TestServer(_app(tmp_path))) as client:
        owner_resp, owner_raw = await _get(client, path, off, params=_owner())
        teammate_resp, teammate_raw = await _get(client, path, off, headers=_teammate(client))
    for resp, raw in ((owner_resp, owner_raw), (teammate_resp, teammate_raw)):
        assert resp.status == 404, raw
        assert json.loads(raw)["code"] == "widgets_off"
    assert resolver.calls == 0


def test_the_preview_reads_only_an_exact_true(monkeypatch) -> None:
    for value, on in ((True, True), (1, False), ("true", False), (None, False)):
        cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={"widgets": value}))
        assert mw.widgets_on(cfg) is on, value
    assert mw.widgets_on(SimpleNamespace()) is False
    monkeypatch.setattr("kiro_crew.edition_locks.local_enable_locked", lambda _name: True)
    assert mw.widgets_on(_config()) is False, "an LTS build keeps every preview off"


# --------------------------------------------------------------------------
# the validator
# --------------------------------------------------------------------------


def test_the_etag_is_scoped_to_the_reader() -> None:
    body = {"v": 1, "blocks": [{"type": "metric", "value": 7}], "generated_at": "x"}
    scope = (SLUG, BOT, "build-health", "")
    assert mw.widget_etag(OWNER, scope, body) != mw.widget_etag("relay:" + ANA, scope, body)
    assert mw.widget_etag(OWNER, scope, body) == mw.widget_etag(OWNER, scope, dict(body))


def test_two_widgets_with_equal_bodies_get_different_tags() -> None:
    body = {"v": 1, "blocks": []}
    one = mw.widget_etag(OWNER, (SLUG, BOT, "build-health", ""), body)
    two = mw.widget_etag(OWNER, (SLUG, BOT, "prs", ""), body)
    assert one != two
    # Every scope part counts, the locale included.
    assert one != mw.widget_etag(OWNER, (SLUG, BOT, "build-health", "fr"), body)


def test_the_etag_ignores_only_generated_at() -> None:
    scope = (SLUG, BOT, "status", "")
    base = {"v": 1, "generated_at": "2026-11-18T00:30:00Z", "blocks": [{"value": 1}]}
    later = {**base, "generated_at": "2026-11-18T09:00:00Z"}
    changed = {**base, "blocks": [{"value": 2}]}
    assert mw.widget_etag(OWNER, scope, base) == mw.widget_etag(OWNER, scope, later)
    assert mw.widget_etag(OWNER, scope, base) != mw.widget_etag(OWNER, scope, changed)


# --------------------------------------------------------------------------
# size caps
# --------------------------------------------------------------------------


def _widest_package() -> dict[str, Any]:
    """Four widgets, six blocks each, every string and list at its 3.2.1 limit."""
    types = {f"f{i}": {"type": "array", "source": {"agentic": True}} for i in range(6)}
    widgets = []
    for w in range(4):
        widgets.append(
            {
                "id": f"w{w}" + "x" * 30,
                "title": "T" * 40,
                "sizes": ["small", "medium", "large"],
                "blocks": [
                    {
                        "type": "list",
                        "label": "L" * 40,
                        "field": f"f{i}",
                        "item_text": "text",
                        "item_detail": "detail",
                        "max_items": 5,
                    }
                    for i in range(6)
                ],
                "tap": {"open": "bot"},
            }
        )
    return {"model": {"types": types}, "widgets": widgets}


def _widest_read() -> FieldRead:
    rows = [{"text": "x" * 200, "detail": "y" * 200} for _ in range(9)]
    return FieldRead(
        fields={f"f{i}": rows for i in range(6)},
        written_at={f"f{i}": "2026-11-18T00:29:00Z" for i in range(6)},
    )


@pytest.mark.asyncio
async def test_a_snapshot_at_every_limit_fits_16_kib(tmp_path, monkeypatch) -> None:
    stub = _Resolver(_widest_package(), _widest_read())
    monkeypatch.setattr(ws, "package_in_force", stub.package_in_force)
    monkeypatch.setattr(md, "read_fields", stub.read_fields)
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "")
    widget = _widest_package()["widgets"][0]["id"]
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(
            client, f"/api/members/{SLUG}/widgets/{widget}?member={BOT}", params=_owner()
        )
    assert resp.status == 200, raw
    assert len(json.loads(raw)["blocks"]) == 6
    assert len(raw) <= mw.MAX_SNAPSHOT_BYTES, len(raw)


@pytest.mark.asyncio
async def test_a_catalog_at_every_limit_fits_64_kib(tmp_path, monkeypatch) -> None:
    stub = _Resolver(_widest_package())
    monkeypatch.setattr(ws, "package_in_force", stub.package_in_force)
    # Dozens of Bots (design 3.2.2), each with four widgets plus the default one.
    names = (BOT, *(f"bot-{i:02d}-" + "n" * 30 for i in range(47)))
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(client, CATALOG, _config(names=names), params=_owner())
    assert resp.status == 200, raw
    bots = json.loads(raw)["bots"]
    assert len(bots) == 48 and all(len(b["widgets"]) == 5 for b in bots)
    assert len(raw) <= mw.MAX_CATALOG_BYTES, len(raw)


@pytest.mark.asyncio
async def test_a_body_over_its_cap_is_refused_not_sent(tmp_path, resolver, monkeypatch) -> None:
    monkeypatch.setattr(mw, "MAX_SNAPSHOT_BYTES", 64)
    async with TestClient(TestServer(_app(tmp_path))) as client:
        resp, raw = await _get(client, SNAPSHOT, params=_owner())
    assert resp.status == 500
    assert json.loads(raw)["code"] == "widget_too_large"


# --------------------------------------------------------------------------
# the production binder and the fixtures for the phone
# --------------------------------------------------------------------------


def test_the_gateway_binds_exactly_these_paths_deferred() -> None:
    """``server.py`` restates each path against its deferred binder; the two agree."""
    import inspect

    from kiro_crew.dashboard import server

    app = web.Application()
    mw.register_member_widgets_routes(app)
    ours = {str(r.resource.canonical) for r in app.router.routes()}
    flat = " ".join(inspect.getsource(server._register_mcp_routes).split())
    assert ours == {"/api/widgets", "/api/members/{slug}/widgets/{widget_id}"}
    missing = {path for path in ours if f'"{path}", _deferred("member_widgets",' not in flat}
    assert not missing, missing


@pytest.mark.asyncio
async def test_both_widget_routes_are_reached_through_the_servers_binder(
    tmp_path, resolver
) -> None:
    from kiro_crew.dashboard import server

    async with TestClient(TestServer(_app(tmp_path, server._register_mcp_routes))) as client:
        catalog, catalog_raw = await _get(client, CATALOG, params=_owner())
        snapshot, snapshot_raw = await _get(client, SNAPSHOT, params=_owner())
        refused, _ = await _get(client, SNAPSHOT, headers=_teammate(client))
    assert catalog.status == 200, catalog_raw
    assert json.loads(catalog_raw)["bots"][0]["slug"] == SLUG
    assert snapshot.status == 200, snapshot_raw
    assert json.loads(snapshot_raw)["widget"]["id"] == "build-health"
    assert refused.status == 403


@pytest.mark.asyncio
async def test_the_committed_fixtures_are_what_the_routes_serve(
    tmp_path, resolver, monkeypatch
) -> None:
    """``fixtures/widgets/`` holds one real catalog and one real snapshot for the phone's
    parser vectors (KiroMobileCore C1). On a change, the failure prints the served body to
    paste into the fixture; the test itself writes nothing."""
    _frozen(monkeypatch, NOW)
    async with TestClient(TestServer(_app(tmp_path))) as client:
        _, catalog = await _get(client, CATALOG, params=_owner())
        _, snapshot = await _get(client, SNAPSHOT, params=_owner())
    served = {"catalog.json": json.loads(catalog), "snapshot.json": json.loads(snapshot)}
    for name, body in served.items():
        recorded = json.loads((FIXTURES / name).read_text("utf-8"))
        assert recorded == body, f"{name} drifted; served:\n" + json.dumps(
            body, indent=2, ensure_ascii=False
        )
