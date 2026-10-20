"""``stt.submit_on_release`` survives the Settings round trip.

The flag is read only by the browser, so the whole server side is three places
that must agree: the loader keeps it, the PUT stores it, and the GET serves it
to the composer. A setting the GET never returns looks saved in Settings and
does nothing in the composer, so this asserts the value on disk and in the
response.
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


def test_default_is_off() -> None:
    assert KiroCrewConfig().stt.submit_on_release is False


@pytest.mark.asyncio
async def test_put_and_get_round_trip(seeded_config, quiet_probes) -> None:
    async with TestClient(TestServer(_stt_app())) as client:
        assert (await (await client.get("/api/config/stt")).json())["submit_on_release"] is False
        on = await client.put("/api/config/stt", json={"submit_on_release": True})
        assert on.status == 200
        assert (await on.json())["submit_on_release"] is True
        assert (
            json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]["submit_on_release"]
            is True
        )
        # The loader reads the stored value back, which is what the next GET serves.
        assert (await (await client.get("/api/config/stt")).json())["submit_on_release"] is True
        off = await client.put("/api/config/stt", json={"submit_on_release": False})
        assert (await off.json())["submit_on_release"] is False


@pytest.mark.asyncio
async def test_non_bool_is_ignored(seeded_config, quiet_probes) -> None:
    async with TestClient(TestServer(_stt_app())) as client:
        await client.put("/api/config/stt", json={"submit_on_release": "yes"})
    stt = json.loads(seeded_config.read_text(encoding="utf-8")).get("stt", {})
    assert "submit_on_release" not in stt or stt["submit_on_release"] is False
