"""Tests that /api/system includes resource posture fields from resource_status.probe()."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.dashboard import handlers_system as hs
from kiro_crew.resource_status import ResourceStatus


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset the metrics cache so each test gets a fresh collection."""
    hs._metrics_cache = {}
    hs._metrics_cache_ts = 0.0
    hs._metrics_lock = None
    yield
    hs._metrics_cache = {}
    hs._metrics_cache_ts = 0.0
    hs._metrics_lock = None


class _Req(dict):
    """Minimal stand-in for web.Request — api_system reads nothing off it."""


_MOCK_STATUS = ResourceStatus(
    available_gb=6.5,
    cpu_count=4,
    load_per_cpu=0.75,
    posture="tight",
    pressure_gb=8.0,
    critical_gb=3.0,
)


@pytest.mark.asyncio
async def test_api_system_includes_resource_posture(monkeypatch):
    """The /api/system response must include resource posture fields."""
    with patch(
        "kiro_crew.dashboard.handlers_system._resource_probe",
        return_value=_MOCK_STATUS,
        create=True,
    ):
        # Patch at the module level where the lazy import will resolve
        monkeypatch.setattr(
            "kiro_crew.resource_status.probe", lambda cfg=None: _MOCK_STATUS
        )
        monkeypatch.setattr(
            "kiro_crew.subagent.compute_max_subagents", lambda cfg: 7
        )
        resp = await hs.api_system(_Req())

    import json

    body = json.loads(resp.body)
    assert body["resource_posture"] == "tight"
    assert body["resource_available_gb"] == 6.5
    assert body["resource_pressure_gb"] == 8.0
    assert body["resource_critical_gb"] == 3.0
    assert body["subagent_cap"] == 7


@pytest.mark.asyncio
async def test_api_system_resource_posture_fallback_on_probe_failure(monkeypatch):
    """When resource_status.probe() raises, fields degrade to safe defaults."""

    def _failing_probe(cfg=None):
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr("kiro_crew.resource_status.probe", _failing_probe)

    resp = await hs.api_system(_Req())

    import json

    body = json.loads(resp.body)
    assert body["resource_posture"] == "unknown"
    assert body["resource_available_gb"] == -1.0
    assert body["resource_pressure_gb"] == 4.0
    assert body["resource_critical_gb"] == 2.0
    assert body["subagent_cap"] == 3


@pytest.mark.asyncio
async def test_api_system_resource_posture_ample(monkeypatch):
    """Verify ample posture with high available memory."""
    ample_status = ResourceStatus(
        available_gb=16.0,
        cpu_count=8,
        load_per_cpu=0.2,
        posture="ample",
        pressure_gb=4.0,
        critical_gb=2.0,
    )
    monkeypatch.setattr(
        "kiro_crew.resource_status.probe", lambda cfg=None: ample_status
    )
    monkeypatch.setattr(
        "kiro_crew.subagent.compute_max_subagents", lambda cfg: 11
    )

    resp = await hs.api_system(_Req())

    import json

    body = json.loads(resp.body)
    assert body["resource_posture"] == "ample"
    assert body["resource_available_gb"] == 16.0
    assert body["subagent_cap"] == 11
