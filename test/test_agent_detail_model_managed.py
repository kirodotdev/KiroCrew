"""Tests for api_agent_detail PATCH model_managed marker behavior.

An explicit model pick must freeze the choice (model_managed=False); clearing
the model (auto) must resume tracking the shipped default (model_managed=True).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.dashboard.handlers.agents import api_agent_detail


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: these tests exercise handler behavior PAST
    the owner boundary on the agents module's mutating endpoints, which has
    its own enumerate-the-invariant coverage in
    test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _patch_request(name: str, body: dict):
    request = MagicMock(spec=web.Request)
    request.method = "PATCH"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        return body

    request.json = _json
    return request


@pytest.mark.asyncio
async def test_patch_explicit_model_freezes(tmp_path):
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(json.dumps({"name": "kirocrew", "model": "claude-old", "model_managed": True}))
    request = _patch_request("kirocrew", {"model": "claude-new"})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["model"] == "claude-new"
    # Spec stays schema-clean; managed-state goes to the sidecar.
    assert "model_managed" not in data
    assert agent_state.get_model_managed("kirocrew") is False


@pytest.mark.asyncio
async def test_patch_clear_model_resumes_tracking(tmp_path):
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model": "claude-pinned", "model_managed": False})
    )
    request = _patch_request("kirocrew", {"model": ""})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "model" not in data
    assert "model_managed" not in data
    assert agent_state.get_model_managed("kirocrew") is True


@pytest.mark.asyncio
async def test_patch_without_model_lifts_stale_bookkeeping_keys(tmp_path):
    """A PATCH that never touches ``model`` still runs the shared strip/lift rule.

    Regression for a design-review follow-up on #2570: this PATCH handler used
    to unconditionally ``data.pop(...)`` these two keys, discarding a legacy
    value already on disk instead of lifting it into the sidecar the way the
    other three writers (PUT, migrate_agent_specs, _refresh_dynamic_fields) do.
    Routing through ``agent_state.lift_and_strip_bookkeeping`` closes that gap.
    """
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model_managed": False, "cc_model": "claude-sonnet-4.6"})
    )
    request = _patch_request("kirocrew", {})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "model_managed" not in data
    assert "cc_model" not in data
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_patch_write_is_governance_sanitized(tmp_path):
    """The PATCH overwrite must run the whole-config governance funnel inside
    the spec lock: a stale snapshot must not restore ceiling-rejected
    allowedTools/autoApprove grants (GPT round-9 security finding)."""
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model": "claude-old", "allowedTools": ["@stale"]})
    )
    request = _patch_request("kirocrew", {"model": "claude-new"})

    def fake_sanitize(config):
        config["allowedTools"] = ["governance-filtered"]

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path),
        patch("kiro_crew.platform.governance.sanitize_agent_config_governance", fake_sanitize),
    ):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    written = json.loads(cfg.read_text(encoding="utf-8"))
    assert written["allowedTools"] == ["governance-filtered"]
    assert written["model"] == "claude-new"


@pytest.mark.asyncio
async def test_patch_merge_preserves_concurrent_writer_changes(tmp_path):
    """The locked overwrite re-reads INSIDE the lock and merges only this
    patch's delta: a concurrent refresh's change to an untouched key must
    survive, and a key the concurrent writer removed must stay removed."""
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "model": "claude-old",
                "hooks": {"old": True},
                "staleGrant": "x",
            }
        )
    )
    request = _patch_request("kirocrew", {"model": "claude-new"})

    from kiro_crew.dashboard.handlers import agents as agents_mod

    real_read = agents_mod._read_agent_spec
    calls = {"n": 0}

    def racing_read(path, **kwargs):
        calls["n"] += 1
        result = real_read(path, **kwargs)
        # After the handler's pre-lock re-read (2nd read: detail read + reread),
        # simulate a concurrent refresh: change hooks, drop staleGrant.
        if calls["n"] == 2:
            concurrent = dict(result)
            concurrent["hooks"] = {"refreshed": True}
            concurrent.pop("staleGrant", None)
            cfg.write_text(json.dumps(concurrent))
        return result

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path),
        patch("kiro_crew.dashboard.handlers.agents._read_agent_spec", side_effect=racing_read),
    ):
        resp = await api_agent_detail(request)

    assert resp.status == 200
    written = json.loads(cfg.read_text(encoding="utf-8"))
    # This patch's own delta applied...
    assert written["model"] == "claude-new"
    # ...while the concurrent writer's changes to untouched keys survive.
    assert written["hooks"] == {"refreshed": True}
    assert "staleGrant" not in written
