"""``stt.submit_on_release`` and ``stt.slide_to_send`` survive the Settings round trip.

Both flags are read only by the browser, so the whole server side is three
places that must agree: the loader keeps it, the PUT stores it, and the GET
serves it to the composer. A setting the GET never returns looks saved in
Settings and does nothing in the composer, so this asserts the value on disk
and in the response.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_handlers_core_coverage_helpers import seeded_config_fixture  # noqa: F401

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers import core as core_mod

# Browser-only voice toggles, each off by default.
KEYS = ["submit_on_release", "slide_to_send"]


def _stt_app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner-1")
    app.router.add_route("*", "/api/config/stt", core_mod.api_stt_config)
    return app


@pytest.fixture
def quiet_probes(monkeypatch):
    monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
    monkeypatch.setattr(
        core_mod, "availability_detail", lambda _cfg: core_mod.stt.Availability(False)
    )


@pytest.mark.parametrize("key", KEYS)
def test_default_is_off(key: str) -> None:
    assert getattr(KiroCrewConfig().stt, key) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_put_and_get_round_trip(seeded_config, quiet_probes, key: str) -> None:
    async with TestClient(TestServer(_stt_app())) as client:
        assert (await (await client.get("/api/config/stt")).json())[key] is False
        on = await client.put("/api/config/stt", json={key: True})
        assert on.status == 200
        assert (await on.json())[key] is True
        assert json.loads(seeded_config.read_text(encoding="utf-8"))["stt"][key] is True
        # The loader reads the stored value back, which is what the next GET serves.
        assert (await (await client.get("/api/config/stt")).json())[key] is True
        off = await client.put("/api/config/stt", json={key: False})
        assert (await off.json())[key] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_non_bool_is_ignored(seeded_config, quiet_probes, key: str) -> None:
    async with TestClient(TestServer(_stt_app())) as client:
        await client.put("/api/config/stt", json={key: "yes"})
    stt = json.loads(seeded_config.read_text(encoding="utf-8")).get("stt", {})
    assert key not in stt or stt[key] is False
