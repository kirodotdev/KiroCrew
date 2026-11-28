"""A stored ``mcp_gateway.enabled: false`` is never turned back on by the 0.15 default.

Sharing is on by default since 0.15.0. A file an older build wrote may hold
``false`` as the old default a full rewrite wrote out, or as the owner's own off
from the Settings switch, and the bytes are the same. So the value is only
reported (``superseded_default_drift``), never rewritten: the owner's off holds.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config import superseded_defaults as SD
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Aim config.json, its overlay and the adoption ledger at *tmp_path*."""
    monkeypatch.setattr(L, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return tmp_path


def _write(home, stamp: object, enabled: object = False) -> dict:
    data: dict = {"mcp_gateway": {"enabled": enabled}}
    if stamp is not None:
        data["meta"] = {"lastTouchedVersion": stamp, "lastTouchedAt": "2026-11-01T00:00:00+00:00"}
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    L._invalidate_config_cache()
    return data


def _stored(home) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))["mcp_gateway"]


def test_a_pre_015_false_is_reported_and_kept_as_the_owners_off(home):
    data = _write(home, "0.14.0.3")

    assert KiroCrewConfig.load().mcp_gateway.enabled is False
    assert _stored(home)["enabled"] is False
    assert "mcp_gateway.enabled" in {e.dotted_key for e in SD.superseded_default_drift(data)}


@pytest.mark.parametrize("stamp", ["0.15.0", "0.16.1", None])
def test_a_false_a_newer_build_or_no_stamp_wrote_is_not_even_reported(home, stamp):
    data = _write(home, stamp)

    assert KiroCrewConfig.load().mcp_gateway.enabled is False
    assert "mcp_gateway.enabled" not in {e.dotted_key for e in SD.superseded_default_drift(data)}
