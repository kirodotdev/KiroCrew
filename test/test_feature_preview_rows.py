"""The config keys behind edition Feature Preview rows: the contributor seam and the owner-only routes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import feature_preview_rows as fpr
from kiro_crew.platform.defaults import DefaultDashboardContributor

KEY = "sample_edition.enabled"
PATH = "/api/feature-previews/edition"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    yield tmp_path


def _contributor(monkeypatch, keys: Any) -> None:
    dashboard = SimpleNamespace(feature_preview_config_keys=lambda: keys)
    monkeypatch.setattr(
        "kiro_crew.platform.current_context", lambda: SimpleNamespace(dashboard=dashboard)
    )


def test_edition_keys_are_kept_and_core_or_malformed_keys_dropped():
    raw = [
        KEY,
        "agent.acp_backend",  # a core schema section
        "dashboard.feature_previews.webhooks",
        "toplevel",  # needs a section
        "Bad.Key",
        "a.b.c.d.e",  # too deep
        7,
        KEY,  # a duplicate
        "other_edition.flags.beta",
    ]
    assert fpr.accepted_keys(raw, fpr._core_top_keys()) == [KEY, "other_edition.flags.beta"]


def test_the_public_edition_names_no_keys():
    assert list(DefaultDashboardContributor().feature_preview_config_keys()) == []


def test_an_edition_without_the_method_contributes_nothing(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.platform.current_context", lambda: SimpleNamespace(dashboard=SimpleNamespace())
    )
    assert fpr.contributed_keys() == []


def test_keys_come_from_the_contributor(monkeypatch):
    _contributor(monkeypatch, (KEY, "agent.acp_backend"))
    assert fpr.contributed_keys() == [KEY]


async def _client(monkeypatch, *, owner: bool = True) -> TestClient:
    async def gate(request, operation):
        return None if owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request", gate
    )
    monkeypatch.setattr("kiro_crew.dashboard.origin.check_origin", lambda request: True)
    monkeypatch.setattr(fpr, "_audit", lambda *a: None)
    _contributor(monkeypatch, [KEY])
    app = web.Application()
    app.router.add_get(fpr.EDITION_PATH, fpr.api_get_edition)
    app.router.add_put(fpr.EDITION_PATH, fpr.api_put_edition)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


H = {"Origin": "http://127.0.0.1"}


def _cfg(home) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_registered_key_reads_unset_and_its_toggle_writes_it(monkeypatch, _home):
    (_home / "config.json").write_text(json.dumps({"agent": {"x": 1}}), encoding="utf-8")
    client = await _client(monkeypatch)
    try:
        assert await (await client.get(PATH)).json() == {"values": {KEY: None}}

        r = await client.put(PATH, json={"config_key": KEY, "enabled": True}, headers=H)
        assert r.status == 200
        assert await r.json() == {"config_key": KEY, "enabled": True}
        cfg = _cfg(_home)
        assert cfg["sample_edition"] == {"enabled": True}
        assert cfg["agent"] == {"x": 1}
        assert await (await client.get(PATH)).json() == {"values": {KEY: True}}

        r = await client.put(PATH, json={"config_key": KEY, "enabled": False}, headers=H)
        assert await r.json() == {"config_key": KEY, "enabled": False}
        assert _cfg(_home)["sample_edition"] == {"enabled": False}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_anyone_but_the_owner_is_refused(monkeypatch, _home):
    client = await _client(monkeypatch, owner=False)
    try:
        assert (await client.get(PATH)).status == 403
        r = await client.put(PATH, json={"config_key": KEY, "enabled": True}, headers=H)
        assert r.status == 403
        assert not (_home / "config.json").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        ({"config_key": "agent.acp_backend", "enabled": True}, H, 404),
        ({"config_key": "other_edition.enabled", "enabled": True}, H, 404),
        ({"config_key": KEY, "enabled": "yes"}, H, 400),
        ({"enabled": True}, H, 400),
        ({"config_key": KEY, "enabled": True}, {}, 403),
    ],
)
async def test_a_bad_write_is_refused_and_writes_nothing(monkeypatch, _home, body, headers, status):
    client = await _client(monkeypatch)
    try:
        r = await client.put(PATH, json=body, headers=headers)
        assert r.status == status
        assert not (_home / "config.json").exists()
    finally:
        await client.close()
