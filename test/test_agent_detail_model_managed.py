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

    This PATCH handler must lift a legacy value already on disk into the sidecar
    rather than ``data.pop(...)`` the two keys away, the way the other three
    writers (PUT, migrate_agent_specs, _refresh_dynamic_fields) do. It routes
    through ``agent_state.lift_and_strip_bookkeeping`` to guarantee that.
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
    allowedTools/autoApprove grants (a security boundary)."""
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model": "claude-old", "allowedTools": ["@stale"]})
    )
    request = _patch_request("kirocrew", {"model": "claude-new"})

    def fake_sanitize(config):
        config["allowedTools"] = ["governance-filtered"]

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path),
        patch(
            "kiro_crew.dashboard.handlers.agents.sanitize_agent_config_governance",
            fake_sanitize,
        ),
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


@pytest.mark.asyncio
async def test_the_response_reports_the_document_that_landed_not_the_pre_lock_snapshot(tmp_path):
    """A URI this write preserved must appear in the response, or the next edit deletes it.

    The delta merge folds a concurrent writer's URI into the document that is persisted. If the
    response is computed from the pre-lock snapshot instead, that URI is absent from what the
    client now holds, so the client's next submission omits it and the write path -- correctly
    honouring a set that does not name it -- deletes it durably. The loss re-enters through
    the response rather than through the write.
    """
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "resources": ["skill://hand/authored", "file://steering/*.md"],
            }
        )
    )
    request = _patch_request(
        "kirocrew",
        {
            "removed_unmanaged_skills": ["skill://hand/authored"],
            "unmanaged_skills": ["skill://hand/authored"],
        },
    )

    from kiro_crew.dashboard.handlers import agents as agents_mod

    real_read = agents_mod._read_agent_spec
    calls = {"n": 0}
    concurrent_uri = "skill://concurrently-added"

    def racing_read(path, **kwargs):
        calls["n"] += 1
        result = real_read(path, **kwargs)
        # After the pre-lock re-read, a co-owner maps another URI. The locked read that
        # follows sees it; this writer's own snapshot never did.
        if calls["n"] == 2:
            concurrent = dict(result)
            concurrent["resources"] = list(result.get("resources") or []) + [concurrent_uri]
            cfg.write_text(json.dumps(concurrent))
        return result

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path),
        patch("kiro_crew.dashboard.handlers.agents._read_agent_spec", side_effect=racing_read),
    ):
        resp = await api_agent_detail(request)

    assert resp.status == 200, resp.text
    written = json.loads(cfg.read_text(encoding="utf-8"))
    assert concurrent_uri in written["resources"], "the delta merge did not preserve it"
    assert "skill://hand/authored" not in written["resources"], "the named removal did not happen"

    body = json.loads(resp.text)
    reported = body.get("unmanaged_skills") or []
    assert concurrent_uri in reported, (
        f"the response omitted a URI the write preserved, so the next edit would delete "
        f"it: {reported}"
    )


@pytest.mark.asyncio
async def test_a_patch_that_names_no_resource_change_leaves_a_malformed_value_alone(tmp_path):
    """An unrelated PATCH must not rewrite ``resources`` -- valid or not.

    A non-list ``resources`` is a state this codebase guards for in its readers and in the
    skill-mapping writer, so it reaches the delta merge in real operation. Iterating a string
    yields characters, each of them a ``str``, so an unguarded merge persists the value split
    per character -- silent on-disk corruption on the most common PATCH path, driven by a
    request that never mentioned resources.
    """
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(
        json.dumps({"name": "kirocrew", "model": "claude-old", "resources": "skill://not-a-list"})
    )
    request = _patch_request("kirocrew", {"model": "claude-new"})

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
        resp = await api_agent_detail(request)

    assert resp.status == 200, resp.text
    written = json.loads(cfg.read_text(encoding="utf-8"))
    assert written["model"] == "claude-new", "the patch's own field did not apply"
    assert (
        written["resources"] == "skill://not-a-list"
    ), f"an unrelated patch rewrote a malformed resources value: {written.get('resources')!r}"


@pytest.mark.asyncio
async def test_a_resource_change_over_a_malformed_value_does_not_split_it(tmp_path):
    """When a patch DOES name a resource change, the malformed value must not be iterated.

    This is the other half: the early return only covers a patch with no resource delta. Here
    the patch names one, so the merge runs against a ``fresh`` whose ``resources`` is a string
    -- and iterating that yields characters, every one of them a ``str``.
    """
    cfg = tmp_path / "kirocrew.json"
    cfg.write_text(json.dumps({"name": "kirocrew", "resources": ["skill://hand/authored"]}))
    request = _patch_request(
        "kirocrew",
        {
            "removed_unmanaged_skills": ["skill://hand/authored"],
            "unmanaged_skills": ["skill://hand/authored"],
        },
    )

    from kiro_crew.dashboard.handlers import agents as agents_mod

    real_read = agents_mod._read_agent_spec
    calls = {"n": 0}

    def racing_read(path, **kwargs):
        calls["n"] += 1
        result = real_read(path, **kwargs)
        # The locked re-read finds a malformed value this writer's snapshot never saw.
        if calls["n"] == 2:
            cfg.write_text(json.dumps({"name": "kirocrew", "resources": "skill://not-a-list"}))
        return result

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path),
        patch("kiro_crew.dashboard.handlers.agents._read_agent_spec", side_effect=racing_read),
    ):
        resp = await api_agent_detail(request)

    assert resp.status == 200, resp.text
    written = json.loads(cfg.read_text(encoding="utf-8"))
    landed = written.get("resources", [])
    assert not (
        isinstance(landed, list) and any(len(r) == 1 for r in landed)
    ), f"a malformed value was split into characters: {landed!r}"
