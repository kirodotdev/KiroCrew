"""The memory knobs are editable from Settings.

``agent.spawn_min_memory_gb``, ``agent.resource_pressure_gb`` and
``agent.resource_critical_gb`` are performance trade-offs the user owns, not
authority grants, so they go through the generic PATCH allowlist with a
0..1024 GB range. These tests pin that a decimal value is
written, that the range holds on the write path and on load, and that none of the
three asks for a restart.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

KEYS = ("spawn_min_memory_gb", "resource_pressure_gb", "resource_critical_gb")


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    from kiro_crew.config import loader

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agent": {"approval_mode": "auto"}}), encoding="utf-8")
    # config_dir too: the superseded-defaults ack and adoption sidecars live there.
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_a_decimal_value_is_written_to_config(tmp_config, key: str) -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, f"agent.{key}", 1.5)
        assert resp.status == 200, await resp.text()
    assert json.loads(tmp_config.read_text())["agent"][key] == 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_zero_is_accepted_because_it_is_the_off_value(tmp_config, key: str) -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, f"agent.{key}", 0)
        assert resp.status == 200, await resp.text()
    assert json.loads(tmp_config.read_text())["agent"][key] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("bad", [-1, 1024.5, "abc", float("inf")])
async def test_out_of_range_or_non_numeric_is_refused(tmp_config, key: str, bad) -> None:
    before = tmp_config.read_text()
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, f"agent.{key}", bad)
        assert resp.status == 400
    assert tmp_config.read_text() == before


@pytest.mark.asyncio
async def test_residual_a_json_true_is_still_read_as_one(tmp_config) -> None:
    """RESIDUAL, pinned on purpose: the shared ``float`` branch of the PATCH
    validator does ``float(value)``, so ``true`` is stored as ``1.0`` for every
    float key in ``_EDITABLE_CONFIG`` (``session.autocompact_pct`` included), not
    only these three. Out of scope here; whoever adds a bool guard to that branch
    should flip this to expect 400.
    """
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, "agent.spawn_min_memory_gb", True)
        assert resp.status == 200
    assert json.loads(tmp_config.read_text())["agent"]["spawn_min_memory_gb"] == 1.0


@pytest.mark.parametrize("key", KEYS)
def test_none_of_them_needs_a_restart(key: str) -> None:
    """The Settings rows promise "applies without a restart"; the schema must agree."""
    from kiro_crew.config.schema import requires_restart

    assert requires_restart(f"agent.{key}") is False


@pytest.mark.parametrize("key", KEYS)
def test_a_hand_edited_negative_loads_as_zero(tmp_path, key: str) -> None:
    """Every consumer already reads a value <= 0 as off, so the floor changes nothing."""
    from kiro_crew.config.loader import KiroCrewConfig

    (tmp_path / "config.json").write_text(json.dumps({"agent": {key: -3.0}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        cfg = KiroCrewConfig.load()
    assert getattr(cfg.agent, key) == 0.0


def test_write_bounds_come_from_the_shared_constants() -> None:
    from kiro_crew.config import sections
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    for key in KEYS:
        spec = _EDITABLE_CONFIG[f"agent.{key}"]
        assert spec["type"] == "float"
        assert (spec["min"], spec["max"]) == (
            sections.RESOURCE_MEMORY_GB_MIN,
            sections.RESOURCE_MEMORY_GB_MAX,
        )


@pytest.mark.asyncio
async def test_saving_the_superseded_floor_survives_the_next_load(tmp_config) -> None:
    """4.0 is the floor's superseded default, adopted away on load unless acknowledged.

    On an install whose adoption ledger has no row for the key (a fresh one never
    stored 4.0), a Settings save of 4.0 must not quietly revert to 2.0.
    """
    from kiro_crew.config import loader
    from kiro_crew.config import superseded_defaults as sd

    assert sd.adopted_superseded() == {}
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, "agent.spawn_min_memory_gb", 4)
        assert resp.status == 200, await resp.text()

    loader._invalidate_config_cache()
    assert loader.KiroCrewConfig.load().agent.spawn_min_memory_gb == 4.0
    assert json.loads(tmp_config.read_text())["agent"]["spawn_min_memory_gb"] == 4.0
    assert sd.acked_superseded() == {"agent.spawn_min_memory_gb": 4.0}


@pytest.mark.asyncio
async def test_a_value_that_is_not_a_superseded_default_is_not_acknowledged(tmp_config) -> None:
    from kiro_crew.config import superseded_defaults as sd

    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, "agent.spawn_min_memory_gb", 3.0)
        assert resp.status == 200
    assert sd.acked_superseded() == {}
