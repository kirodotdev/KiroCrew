"""HTTP surface: enablement gate, validation, lazy loading, per-type routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.agentcore_observatory.backend import agentcore, catalog, routes
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig

pytestmark = pytest.mark.asyncio

BASE = "/api/apps/agentcore-observatory"


@pytest.fixture
def app_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's data dir at a tmp dir, so no real config is touched."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.agentcore_observatory.backend.config.app_data_dir",
        lambda _name: tmp_path,
    )
    return tmp_path


def _enable(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: enabled)


async def _client() -> TestClient:
    app = web.Application()
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_disabled_app_denies_every_route(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """Builtin routes exist from gateway startup, so the gate is the only guard."""
    _enable(monkeypatch, False)
    client = await _client()
    try:
        for path in (
            "/config",
            "/profiles",
            "/catalog",
            "/resource/agent-runtimes",
            "/resource/memories/detail",
        ):
            res = await client.get(f"{BASE}{path}")
            assert res.status == 403, path
            assert (await res.json())["code"] == "app_disabled"
        res = await client.put(f"{BASE}/config", json={"region": "us-east-1"})
        assert res.status == 403
    finally:
        await client.close()


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


async def test_get_config_reports_unconfigured(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/config")
        assert await res.json() == {"profile": "", "region": "", "configured": False}
    finally:
        await client.close()


async def test_put_config_roundtrips(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.put(
            f"{BASE}/config", json={"profile": "my-prof", "region": "eu-central-1"}
        )
        assert await res.json() == {
            "profile": "my-prof",
            "region": "eu-central-1",
            "configured": True,
        }
        assert (await (await client.get(f"{BASE}/config")).json())["region"] == "eu-central-1"
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"profile": "has space", "region": "us-east-1"}, "invalid_profile"),
        ({"profile": "ok", "region": "US-EAST-1"}, "invalid_region"),
        ({"profile": "ok"}, "invalid_region"),
        ({"profile": "p" * 300, "region": "us-east-1"}, "field_too_long"),
    ],
)
async def test_put_config_rejects_bad_input(
    monkeypatch: pytest.MonkeyPatch, app_root: Path, body: dict[str, Any], code: str
) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.put(f"{BASE}/config", json=body)
        assert res.status == 400
        assert (await res.json())["code"] == code
    finally:
        await client.close()


async def test_put_config_rejects_non_object_body(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.put(f"{BASE}/config", json=[1, 2])
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"
    finally:
        await client.close()


async def test_put_config_rejects_a_body_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """A malformed body is a 400, never a 500 escaping the handler."""
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.put(
            f"{BASE}/config",
            data="not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"
    finally:
        await client.close()


async def test_detail_unknown_type_is_404(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/resource/not-a-type/detail")
        assert res.status == 404
        assert (await res.json())["code"] == "unknown_type"
    finally:
        await client.close()


async def test_detail_requires_configuration(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/resource/memories/detail?memory-id=m1")
        assert res.status == 409
        assert (await res.json())["code"] == "not_configured"
    finally:
        await client.close()


# --------------------------------------------------------------------------
# catalog — the lazy-loading contract
# --------------------------------------------------------------------------


async def test_catalog_makes_no_aws_call(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    """The whole point of the rail skeleton: instant first paint with 27 types."""
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)

    def boom(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        raise AssertionError("/catalog must not touch AWS")

    monkeypatch.setattr(agentcore, "run_aws", boom)
    client = await _client()
    try:
        body = await (await client.get(f"{BASE}/catalog")).json()
        assert body["config"]["region"] == "us-east-2"
        assert [g["id"] for g in body["groups"]] == list(catalog.GROUPS)
        listed = {t["id"] for g in body["groups"] for t in g["types"]}
        assert listed == {rt.id for rt in catalog.root_types()}
    finally:
        await client.close()


async def test_catalog_exposes_children_with_their_parent_wiring(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """The UI needs the flags to build a child query; it must not guess them."""
    _enable(monkeypatch, True)
    client = await _client()
    try:
        body = await (await client.get(f"{BASE}/catalog")).json()
        types = {t["id"]: t for g in body["groups"] for t in g["types"]}
        kids = {c["id"]: c for c in types["gateways"]["children"]}
        assert kids["gateway-targets"]["parentParams"] == ["--gateway-identifier"]
        assert kids["gateway-targets"]["parentFields"] == ["gatewayId"]
        assert types["memories"]["children"] == []
    finally:
        await client.close()


async def test_catalog_works_before_a_region_is_configured(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """The rail renders while unconfigured, so the shell is never blank."""
    _enable(monkeypatch, True)
    client = await _client()
    try:
        body = await (await client.get(f"{BASE}/catalog")).json()
        assert body["config"]["configured"] is False
        assert body["groups"]
    finally:
        await client.close()


# --------------------------------------------------------------------------
# per-type reads
# --------------------------------------------------------------------------


async def test_resource_requires_configuration(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/resource/agent-runtimes")
        assert res.status == 409
        assert (await res.json())["code"] == "not_configured"
    finally:
        await client.close()


async def test_unknown_type_is_404(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    _enable(monkeypatch, True)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/resource/not-a-type")
        assert res.status == 404
        assert (await res.json())["code"] == "unknown_type"
    finally:
        await client.close()


async def test_resource_returns_one_types_list(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)
    monkeypatch.setattr(
        agentcore,
        "list_resource",
        lambda _cfg, tid, parents: agentcore.ListResult(ok=True, items=[{"t": tid}]),
    )
    client = await _client()
    try:
        body = await (await client.get(f"{BASE}/resource/evaluators")).json()
        assert body["type"] == "evaluators"
        assert body["list"]["items"] == [{"t": "evaluators"}]
    finally:
        await client.close()


async def test_child_parent_id_travels_by_query_string(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """`?gateway-identifier=gw-1` must become `--gateway-identifier gw-1`."""
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)
    seen: dict[str, str] = {}

    def fake_list(_cfg: Any, _tid: str, parents: dict[str, str]) -> agentcore.ListResult:
        seen.update(parents)
        return agentcore.ListResult(ok=True)

    monkeypatch.setattr(agentcore, "list_resource", fake_list)
    client = await _client()
    try:
        await client.get(f"{BASE}/resource/gateway-targets?gateway-identifier=gw-1")
        assert seen == {"--gateway-identifier": "gw-1"}
    finally:
        await client.close()


async def test_singleton_is_served_as_an_object(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """`token-vault` has no list verb, so the rail item must still open."""
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)
    monkeypatch.setattr(
        agentcore,
        "get_resource",
        lambda _cfg, _tid, _ids: agentcore.ObjectResult(ok=True, item={"tokenVaultId": "d"}),
    )
    client = await _client()
    try:
        body = await (await client.get(f"{BASE}/resource/token-vault")).json()
        assert body["singleton"]["item"] == {"tokenVaultId": "d"}
        assert "list" not in body
    finally:
        await client.close()


async def test_failed_read_degrades_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """A 200 carrying ok=false: the section fails, the page does not."""
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)
    monkeypatch.setattr(
        agentcore,
        "list_resource",
        lambda *_a: agentcore.ListResult(ok=False, error="AccessDenied", denied=False),
    )
    client = await _client()
    try:
        res = await client.get(f"{BASE}/resource/policies?policy-engine-id=pe-1")
        assert res.status == 200
        body = await res.json()
        assert body["list"]["ok"] is False
        assert body["list"]["error"] == "AccessDenied"
    finally:
        await client.close()


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------


async def test_detail_passes_identifier_flags(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)
    seen: dict[str, str] = {}

    def fake_get(_cfg: Any, _tid: str, ids: dict[str, str]) -> agentcore.ObjectResult:
        seen.update(ids)
        return agentcore.ObjectResult(ok=True, item={"ok": True})

    monkeypatch.setattr(agentcore, "get_resource", fake_get)
    client = await _client()
    try:
        await client.get(
            f"{BASE}/resource/gateway-targets/detail?gateway-identifier=g1&target-id=t1"
        )
        assert seen == {"--gateway-identifier": "g1", "--target-id": "t1"}
    finally:
        await client.close()


async def test_detail_refused_for_a_type_without_a_get_verb(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)
    ObservatoryConfig(profile="p", region="us-east-2").save(app_root)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/resource/agent-runtime-versions/detail")
        assert res.status == 400
        assert (await res.json())["code"] == "no_detail"
    finally:
        await client.close()


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------


async def test_profiles_returns_names_and_declared_regions(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """Names and regions only — never a credential, never an account id."""
    _enable(monkeypatch, True)
    monkeypatch.setattr(
        "kiro_crew.deploy.profiles.load_registry",
        lambda: {
            "version": 2,
            "default": "good",
            "profiles": [
                {"name": "good", "region": "us-east-2", "account": "123456789012"},
                {"name": "no-region", "region": ""},
                {"name": "bad name", "region": "us-east-1"},
                {"name": "junk-region", "region": "US-EAST-1"},
                {"name": "", "region": "us-east-1"},
            ],
        },
    )
    client = await _client()
    try:
        body = await (await client.get(f"{BASE}/profiles")).json()
        assert body == {
            "profiles": [
                {"name": "good", "region": "us-east-2"},
                {"name": "no-region", "region": ""},
                {"name": "junk-region", "region": ""},
            ]
        }
        assert "123456789012" not in str(body)
    finally:
        await client.close()


async def test_profiles_degrades_to_empty_when_registry_unreadable(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, True)

    def boom() -> dict[str, Any]:
        raise OSError("registry unreadable")

    monkeypatch.setattr("kiro_crew.deploy.profiles.load_registry", boom)
    client = await _client()
    try:
        res = await client.get(f"{BASE}/profiles")
        assert res.status == 200
        assert await res.json() == {"profiles": []}
    finally:
        await client.close()
