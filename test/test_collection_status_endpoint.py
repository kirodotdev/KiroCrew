"""Tests for GET /api/telemetry/collection — the Privacy panel's recording switch.

The endpoint exists so a Settings panel can read the local metric-collection
posture without paying for ``/api/telemetry/startup``, which parses every metric
shard in the window to aggregate percentiles.

Its contract is that ``enabled`` is the EFFECTIVE state, not the stored flag:
``KIROCREW_TELEMETRY`` overrides ``telemetry.enabled`` inside the collector, so a
switch reading back the config value alone would sit on "off" while metrics were
being written. ``env_pinned`` and ``overlay_override`` name the two cases where
the config file is not what decides, so the panel can disable the control instead
of offering a write that cannot hold.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_collection_status

    app = web.Application()
    app.router.add_get("/api/telemetry/collection", api_collection_status)
    return app


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Isolated data home, and no ambient env pin from the developer's shell."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    return tmp_path


def _write_config(home, telemetry: dict) -> None:
    (home / "config.json").write_text(json.dumps({"telemetry": telemetry}), encoding="utf-8")


def _write_overlay(home, telemetry: dict) -> None:
    (home / "config.local.json").write_text(
        json.dumps({"telemetry": telemetry}), encoding="utf-8"
    )


async def _get(client: TestClient) -> dict:
    resp = await client.get("/api/telemetry/collection")
    assert resp.status == 200
    return await resp.json()


async def client_text(client: TestClient) -> str:
    """The raw response body, for asserting what is NOT in it."""
    resp = await client.get("/api/telemetry/collection")
    assert resp.status == 200
    return await resp.text()


class TestCollectionStatusEndpoint:
    @pytest.mark.asyncio
    async def test_reports_stored_flag_when_nothing_overrides(self, _home) -> None:
        _write_config(_home, {"enabled": True})
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["enabled"] is True
        assert body["env_pinned"] is False
        assert body["overlay_override"] is False

    @pytest.mark.asyncio
    async def test_defaults_off(self, _home) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["enabled"] is False

    @pytest.mark.asyncio
    async def test_env_var_on_beats_a_false_config(self, _home, monkeypatch) -> None:
        # The collector resolves the env var over the config flag, so a switch
        # showing "off" here would deny collection that is actually happening.
        _write_config(_home, {"enabled": False})
        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["enabled"] is True
        assert body["env_pinned"] is True
        assert body["env_var"] == "KIROCREW_TELEMETRY"

    @pytest.mark.asyncio
    async def test_env_var_off_beats_a_true_config(self, _home, monkeypatch) -> None:
        _write_config(_home, {"enabled": True})
        monkeypatch.setenv("KIROCREW_TELEMETRY", "off")
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["enabled"] is False
        assert body["env_pinned"] is True

    @pytest.mark.asyncio
    async def test_overlay_entry_is_reported(self, _home) -> None:
        # config.local.json deep-merges OVER config.json, and the switch writes the
        # base file — so without this the switch would snap back after a successful
        # save with nothing on screen explaining why.
        _write_config(_home, {"enabled": False})
        _write_overlay(_home, {"enabled": True})
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["overlay_override"] is True
        assert body["enabled"] is True

    @pytest.mark.asyncio
    async def test_overlay_touching_only_the_beacon_is_not_reported(self, _home) -> None:
        # The two telemetry switches share one overlay helper; a beacon-only entry
        # must not disable the unrelated recording switch.
        _write_config(_home, {"enabled": True})
        _write_overlay(_home, {"beacon_enabled": False})
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["overlay_override"] is False

    @pytest.mark.asyncio
    async def test_malformed_overlay_does_not_500(self, _home) -> None:
        _write_config(_home, {"enabled": True})
        (_home / "config.local.json").write_text("{not json", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["overlay_override"] is False

    @pytest.mark.asyncio
    async def test_unreadable_config_reports_off_rather_than_raising(self, _home) -> None:
        # A diagnostic must never 500, and it must fail toward "off" so the UI
        # never claims collection is on when that cannot be proven.
        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load", side_effect=OSError("boom")
        ):
            async with TestClient(TestServer(_make_app())) as c:
                body = await _get(c)
        assert body["enabled"] is False

    @pytest.mark.asyncio
    async def test_reports_the_metrics_directory(self, _home) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["metrics_dir"].endswith("metrics")

    @pytest.mark.asyncio
    async def test_reports_that_an_egress_endpoint_is_configured(self, _home) -> None:
        # Collection is not local when an OTLP endpoint is set, so the panel has to
        # know: it disables the switch instead of offering a write the config route
        # refuses.
        _write_config(_home, {"enabled": False, "otlp_endpoint": "http://otel:4318/v1/metrics"})
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["otlp_configured"] is True

    @pytest.mark.asyncio
    async def test_never_returns_the_endpoint_itself(self, _home) -> None:
        # The endpoint can carry credentials in its URL, and the panel only needs
        # to know that one exists.
        _write_config(
            _home, {"enabled": True, "otlp_endpoint": "https://user:secret@otel.example/v1"}
        )
        async with TestClient(TestServer(_make_app())) as c:
            resp = await client_text(c)
        assert "secret" not in resp
        assert "otel.example" not in resp

    @pytest.mark.asyncio
    async def test_no_endpoint_reports_false(self, _home) -> None:
        _write_config(_home, {"enabled": True})
        async with TestClient(TestServer(_make_app())) as c:
            body = await _get(c)
        assert body["otlp_configured"] is False
