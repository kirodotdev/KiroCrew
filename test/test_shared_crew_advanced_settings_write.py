"""Shared Crew's advanced settings are writable through ``PATCH /api/config/kirocrew``.

The Settings page writes one path per call: ``{"path": "dashboard.shared_crew.<key>",
"value": ...}``. The integer domains are the loader's own bounds.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import loader as L
from kiro_crew.config.sections_shared_crew import (
    SHARED_CREW_BOUNDS,
    SharedCrewConfig,
    shared_crew_config_from,
)
from kiro_crew.dashboard.handlers import api_kirocrew_config_patch
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

PREFIX = "dashboard.shared_crew."
KNOBS = {
    "admission_enabled": "bool",
    "tailnet_direct": "bool",
    "principal_concurrency": "int",
    "crew_concurrency": "int",
    "typing_idle_seconds": "int",
}


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    cfgp.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    return cfgp


async def _patch(path: str, value):
    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.patch("/api/config/kirocrew", json={"path": path, "value": value})
        return resp.status, await resp.json()


def test_the_five_knobs_are_editable_with_the_loaders_bounds():
    for name, kind in KNOBS.items():
        assert _EDITABLE_CONFIG[PREFIX + name]["type"] == kind, name
    for name, (lo, hi) in SHARED_CREW_BOUNDS.items():
        spec = _EDITABLE_CONFIG[PREFIX + name]
        assert (spec["min"], spec["max"]) == (lo, hi), name


def test_the_enforcement_switches_stay_config_file_only():
    assert PREFIX + "hard_limits" not in _EDITABLE_CONFIG
    assert PREFIX + "containment_enabled" not in _EDITABLE_CONFIG
    assert "dashboard.shared_crew" not in _EDITABLE_CONFIG


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("admission_enabled", True),
        ("tailnet_direct", True),
        ("principal_concurrency", 6),
        ("crew_concurrency", 12),
        ("typing_idle_seconds", 20),
    ],
)
async def test_each_knob_writes_and_loads_back(cfg_home, name, value):
    status, body = await _patch(PREFIX + name, value)
    assert status == 200, body
    written = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert written["dashboard"]["shared_crew"][name] == value
    assert getattr(shared_crew_config_from(written["dashboard"]["shared_crew"]), name) == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("admission_enabled", "yes"),
        ("tailnet_direct", 1),
        ("principal_concurrency", 0),
        ("principal_concurrency", 257),
        ("crew_concurrency", -1),
        ("typing_idle_seconds", 2),
        ("typing_idle_seconds", 61),
    ],
)
async def test_a_value_outside_the_domain_is_refused_and_writes_nothing(cfg_home, name, value):
    status, _ = await _patch(PREFIX + name, value)
    assert status == 400
    assert "shared_crew" not in json.loads(cfg_home.read_text(encoding="utf-8"))["dashboard"]


@pytest.mark.asyncio
async def test_a_switch_that_is_not_listed_is_not_editable(cfg_home):
    status, body = await _patch(PREFIX + "hard_limits", True)
    assert status == 400 and "not editable" in body["error"]


def test_the_section_and_field_help_name_the_feature_preview_not_the_environment():
    from dataclasses import fields

    from kiro_crew.config.sections_dashboard import DashboardConfig

    section = next(f for f in fields(DashboardConfig) if f.name == "shared_crew")
    assert "Not active in this build" not in section.metadata["help"]
    assert "Feature Previews" in section.metadata["help"]
    for f in fields(SharedCrewConfig):
        assert "Feature Previews" in f.metadata["help"], f.name
        assert "KIROCREW_SHARED_CREW_PREVIEW" not in f.metadata["help"], f.name


# -- tailnet_direct opens the tailnet, so a ceiling pinning it off refuses it -- #


@pytest.fixture
def governance_records(monkeypatch) -> list[dict]:
    """Every ``governance_decision`` SEL row the request writes."""
    import kiro_crew.sel as sel_mod

    got: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            got.append(kw)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    return got


def _pin_tailnet_off(monkeypatch) -> None:
    """An enterprise build whose ceiling is silent on ``capabilities.tailnet_origin``.

    The enterprise baseline turns that capability off, so the host is pinned.
    """
    from types import SimpleNamespace

    from kiro_crew.platform import context as pc
    from kiro_crew.platform import governance_profiles
    from kiro_crew.platform.governance import parse_policy

    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
    ctx = SimpleNamespace(governance=parse_policy({"version": 1, "boot": {}}))
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    monkeypatch.setattr(pc, "installed_context", lambda: ctx)
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)


def _tailnet_rows(records: list[dict]) -> list[tuple[str, str]]:
    return [
        (r["tool_name"], r["outcome"])
        for r in records
        if r.get("scope") == "capabilities.tailnet_origin"
    ]


@pytest.mark.asyncio
async def test_tailnet_direct_is_refused_and_audited_under_a_pin(
    cfg_home, monkeypatch, governance_records
):
    _pin_tailnet_off(monkeypatch)
    status, body = await _patch(PREFIX + "tailnet_direct", True)
    assert status == 403, body
    assert "administrator" in body["error"]
    assert "shared_crew" not in json.loads(cfg_home.read_text(encoding="utf-8"))["dashboard"]
    assert _tailnet_rows(governance_records) == [("config_patch_dashboard_tailnet", "denied")]


@pytest.mark.asyncio
async def test_tailnet_direct_off_is_still_writable_under_a_pin(cfg_home, monkeypatch):
    _pin_tailnet_off(monkeypatch)
    status, body = await _patch(PREFIX + "tailnet_direct", False)
    assert status == 200, body
    written = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert written["dashboard"]["shared_crew"]["tailnet_direct"] is False


@pytest.mark.asyncio
async def test_tailnet_direct_writes_on_a_host_that_is_not_pinned(cfg_home, monkeypatch):
    from kiro_crew.dashboard import tailnet

    monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda **_k: False)
    status, body = await _patch(PREFIX + "tailnet_direct", True)
    assert status == 200, body
    written = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert written["dashboard"]["shared_crew"]["tailnet_direct"] is True


@pytest.mark.parametrize("local", [False, True])
def test_config_set_refuses_tailnet_direct_under_a_pin(tmp_path, monkeypatch, local):
    import argparse

    from kiro_crew.cli_config import _config_cmd
    from kiro_crew.dashboard import tailnet

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seen: list[dict] = []
    monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda **kw: seen.append(kw) or True)
    args = argparse.Namespace(
        config_action="set", key=PREFIX + "tailnet_direct", value="true", local=local, file=None
    )
    with pytest.raises(SystemExit) as exc:
        _config_cmd(args)
    assert exc.value.code == 1
    assert seen == [{"audit_tool": "config_set_cli_tailnet"}]
    for name in ("config.json", "config.local.json"):
        path = tmp_path / name
        if path.exists():
            assert "tailnet_direct" not in path.read_text(encoding="utf-8")
