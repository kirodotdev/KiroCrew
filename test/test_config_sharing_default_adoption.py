"""The one-shot adoption of ``mcp_gateway.enabled: false`` that pre-0.15 builds wrote.

Sharing is on by default since 0.15.0, and a full rewrite wrote every default
into ``config.json``, so an older file holds ``false`` nobody chose. Such a file,
by its ``meta`` stamp, has the value adopted once on load; a file a 0.15+ build
wrote, or one with no stamp, keeps its ``false`` as the owner's off. The gateway's
meta refresh does not re-stamp a file whose adoption is still due, so a load that
could not land it can still do so later.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Aim config.json, its overlay and the adoption ledger at *tmp_path*."""
    monkeypatch.setattr(L, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return tmp_path


def _write(home, stamp: object, enabled: object = False) -> None:
    data: dict = {"mcp_gateway": {"enabled": enabled}}
    if stamp is not None:
        data["meta"] = {"lastTouchedVersion": stamp, "lastTouchedAt": "2026-11-01T00:00:00+00:00"}
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    L._invalidate_config_cache()


def _stored(home) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))["mcp_gateway"]


def test_a_pre_015_false_is_adopted_once_and_a_later_off_is_kept(home):
    _write(home, "0.14.0.3")

    assert KiroCrewConfig.load().mcp_gateway.enabled is True
    assert "enabled" not in _stored(home)

    _write(home, "0.14.0.3")
    assert KiroCrewConfig.load().mcp_gateway.enabled is False


@pytest.mark.parametrize("stamp", ["0.15.0", "0.16.1", None])
def test_a_false_a_newer_build_or_no_stamp_wrote_is_the_owners_off(home, stamp):
    _write(home, stamp)

    assert KiroCrewConfig.load().mcp_gateway.enabled is False
    assert _stored(home)["enabled"] is False


def test_the_meta_refresh_keeps_the_stamp_while_the_adoption_is_due(home):
    _write(home, "0.14.0.3")

    assert L.refresh_config_meta_stamp() is False
    assert json.loads((home / "config.json").read_text())["meta"]["lastTouchedVersion"] == (
        "0.14.0.3"
    )


def test_the_meta_refresh_runs_once_nothing_is_due(home):
    _write(home, "0.14.0.3", enabled=True)

    assert L.refresh_config_meta_stamp() is True
