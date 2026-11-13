"""PATCH /api/config/kirocrew and ``telemetry.otlp_endpoints``.

The recording switch promises local-only, so the enable gate must count every
OTLP destination, not only ``telemetry.otlp_endpoint`` (that case is pinned in
``test_config_patch.py::TestTelemetryEnabledEgressGate``). The list itself, like
the single endpoint, is config-file-only.
"""

from __future__ import annotations

import dataclasses
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_config_patch import _make_app, _patch, tmp_config  # noqa: F401

from kiro_crew.config import validation
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultTelemetryProvider
from kiro_crew.platform.interfaces import OtlpDestination


class _EditionTelemetry(DefaultTelemetryProvider):
    """A build that ships its own collector, keeping the operator's entries."""

    def otlp_destinations(self, cfg):
        return super().otlp_destinations(cfg) + (
            OtlpDestination(
                "edition-product",
                "https://telemetry.example.net/v1/metrics",
                frozenset({"metrics"}),
            ),
        )


@pytest.mark.asyncio
async def test_a_list_entry_refuses_the_enable(tmp_config) -> None:  # noqa: F811
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    data.setdefault("telemetry", {})["otlp_endpoints"] = [
        {"name": "ops", "endpoint": "https://collector.example.com/v1/metrics"}
    ]
    tmp_config.write_text(json.dumps(data), encoding="utf-8")
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(c, "telemetry.enabled", True)
        assert resp.status == 409
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["telemetry"].get("enabled") is not True


@pytest.mark.asyncio
async def test_a_build_collector_refuses_the_enable(tmp_config) -> None:  # noqa: F811
    """A build-shipped collector is egress on its own, with nothing in config.json."""
    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, telemetry=_EditionTelemetry()))
    try:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "telemetry.enabled", True)
            assert resp.status == 409
            # Disabling always composes, even with a locked entry.
            resp = await _patch(c, "telemetry.enabled", False)
            assert resp.status == 200
    finally:
        reset_context()


@pytest.mark.asyncio
async def test_the_list_is_not_patch_writable(tmp_config) -> None:  # noqa: F811
    """The list names where metrics go, so a dashboard caller cannot set it."""
    async with TestClient(TestServer(_make_app())) as c:
        resp = await _patch(
            c,
            "telemetry.otlp_endpoints",
            [{"endpoint": "https://collector.example.com/v1/metrics"}],
        )
        assert resp.status == 400
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert "otlp_endpoints" not in data.get("telemetry", {})


def test_the_config_view_masks_every_value_in_the_list() -> None:
    """Entries carry endpoint userinfo, and a literal ``headers`` value the entry
    parser refuses can still sit in the file; the list item has no schema of its
    own, so the whole sensitive list is masked."""
    from kiro_crew.config.loader import TelemetryConfig
    from kiro_crew.dashboard.handlers.core_config_view import _SENSITIVE_MASK, _masked_config_dict

    cfg = KiroCrewConfig(
        telemetry=TelemetryConfig(
            otlp_endpoints=[
                {
                    "name": "ops",
                    "endpoint": "https://user:pw@collector.example.com/v1/metrics",
                    "headers": {"Authorization": "Bearer s3cret"},
                    "enabled": True,
                }
            ]
        )
    )
    shown = _masked_config_dict(cfg)["telemetry"]["otlp_endpoints"]
    assert shown == [
        {
            "name": _SENSITIVE_MASK,
            "endpoint": _SENSITIVE_MASK,
            "headers": {"Authorization": _SENSITIVE_MASK},
            "enabled": True,
        }
    ]


@pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
def test_a_wrong_type_entry_is_masked_in_the_validation_warning(caplog) -> None:
    """An entry written as a bare string (the legacy field's shape) fails the
    item schema. Its warning must not echo the URL: the list is sensitive, so
    every value inside it is too."""
    secret = "https://user:TOKEN@c.example.com/v1/metrics"
    data = {"telemetry": {"otlp_endpoints": [secret]}}
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        validation.validate_config_data(data)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "telemetry.otlp_endpoints.0" in text
    assert "TOKEN" not in text
    assert secret not in text
