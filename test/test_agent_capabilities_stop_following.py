"""Forks, harness fit and Stop following, with isolated config, sidecar and agent homes.

The fork writer's permissions gate, the owned-parent confirmation, the member-backend
warnings for single-tool limits, and Stop following: what the member keeps, what it
refuses, and the fork refresh and spawn gate around it.
Fixtures and helpers come from ``agent_capabilities_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import editor_fixture  # noqa: F401
from agent_capabilities_helpers import _pin_spec_permissions_cli, save, spec_for

from kiro_crew import agent, agent_state
from kiro_crew.agent_capabilities import (
    CapabilityError,
    member_backend_gap,
    prepare_member_capabilities,
    tool_limited_servers,
)
from kiro_crew.config import loader


@pytest.mark.parametrize("release", ["accepts", "refuses", "unknown"])
def test_a_fork_writes_permissions_only_where_the_installed_cli_accepts_them(
    editor, monkeypatch, release
):
    """The fork funnel is a generated-spec writer and shares the one version gate.

    kiro-cli validates agent specs with serde ``deny_unknown_fields``, so a
    release below ``SPEC_PERMISSIONS_MIN_VERSION`` -- or one whose version
    cannot be established -- refuses the WHOLE file this path publishes and
    drops every Crew MCP server with it, falling back to broader default
    grants. The grant list itself is never withheld: it is the ceiling, not
    the optional relay.
    """
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    service, home, specs, parent = editor
    parent["permissions"] = derived_agent_permissions(parent["allowedTools"], "parent")
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    _pin_spec_permissions_cli(monkeypatch, release)

    save(
        service,
        [{"section": "allowedTools", "id": "@search", "action": "set", "value": True}],
        enroll=True,
    )

    spec = spec_for(home, specs)
    assert spec["allowedTools"] == ["@search"], "the grant list is never withheld"
    if release == "accepts":
        assert spec["permissions"] == derived_agent_permissions(["@search"], spec["name"])
    else:
        assert "permissions" not in spec, f"a {release} CLI must get no block"


@pytest.mark.parametrize("release", ["refuses", "unknown"])
def test_a_hand_edited_block_still_blocks_a_fork_on_a_refusing_cli(editor, monkeypatch, release):
    """Two questions, one call site -- and only the second one is the CLI's.

    Whether the source block was hand-edited away from its derivation is a
    governance question about the template, answered the same way whatever
    binary happens to be installed. Withholding the OUTPUT field must not
    quietly withdraw that review.
    """
    service, home, specs, parent = editor
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
        enroll=True,
    )
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    parent["permissions"] = {"rules": [{"capability": "fsWrite", "effect": "allow"}]}
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    _pin_spec_permissions_cli(monkeypatch, release)

    with pytest.raises(CapabilityError, match="alternate_permissions_require_review"):
        service.reset("A", target)
    assert spec_for(home, specs)["prompt"] == "mine"


def test_parent_is_owned_confirmation_gates_the_dashboard_author_stem(tmp_path, monkeypatch):
    """F3 (fail-safe): a global owned-filename parent reads as owned -- except the
    dashboard-author stem, which was a user-creatable template name before it became owned.
    For that stem the owned classification holds ONLY when the on-disk spec POSITIVELY
    confirms as this installer's own -- its bytes reproduce the installer-recorded ownership
    digest. A pre-existing user template at the stem -- no recorded digest, or an
    absent/unreadable file -- is NOT an owned parent, so a capability save does not refresh
    its inherited guards away."""
    from kiro_crew import agent_capabilities as cap
    from kiro_crew import agent_state

    parent = tmp_path / "kirocrew-dashboard-author.json"
    snap = {"parent": {"scope": "global", "path": str(parent)}}

    # Another owned name is owned.
    other = {"parent": {"scope": "global", "path": str(tmp_path / "kirocrew.json")}}
    assert cap._parent_is_owned(other) is True
    # A non-global parent is never owned.
    assert cap._parent_is_owned({"parent": {"scope": "project", "path": str(parent)}}) is False
    # A global parent NOT in the owned set is not owned.
    unowned = {"parent": {"scope": "global", "path": str(tmp_path / "my-template.json")}}
    assert cap._parent_is_owned(unowned) is False

    # Dashboard-author stem, ABSENT file -> not confirmed -> NOT owned (fail-safe).
    assert cap._parent_is_owned(snap) is False
    # A file at the stem with NO recorded ownership digest -> not confirmed -> NOT owned.
    managed = {"name": "kirocrew-dashboard-author", "mcpServers": {"kirocrew-core": {}}}
    parent.write_text(json.dumps(managed, indent=2) + "\n", encoding="utf-8")
    assert cap._parent_is_owned(snap) is False
    # Record the ownership digest of these exact bytes -> reproduces it -> owned.
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(managed))
    assert cap._parent_is_owned(snap) is True


def cap_agent_state_for(cap):
    """`_parent_is_owned` imports agent_state locally; patch the module object itself."""
    from kiro_crew import agent_state

    return agent_state


_LIMITED_DOCS = {
    "section": "mcpServers",
    "id": "docs",
    "action": "set",
    "value": {"command": "example-mcp", "disabledTools": ["wipe"]},
}


def _set_member_backend(home, backend):
    config = json.loads((home / "config.json").read_text())
    config.setdefault("agent", {})["member_acp_backend"] = backend
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()


@pytest.mark.parametrize(
    ("backend", "limited", "expected"),
    [
        ("", (), None),
        ("", ("docs",), None),
        ("kas", (), None),
        ("kas", ("docs",), "tool_limits_unsupported"),
        # Claude, Codex and DeepSeek confirm the consumed projection, so they host a
        # saved spec; none of them carries single-tool limits.
        ("codex", (), None),
        ("codex", ("docs",), "tool_limits_unsupported"),
        ("claude", (), None),
        ("deepseek", ("docs",), "tool_limits_unsupported"),
        ("opencode", (), "harness_unsupported"),
        ("opencode", ("docs",), "harness_unsupported"),
        ("goose", (), "harness_unsupported"),
    ],
)
def test_member_backend_gap_matches_the_start_gate_sets(backend, limited, expected):
    assert member_backend_gap(backend, limited) == expected


def test_tool_limited_servers_skips_muted_and_unlimited_entries():
    spec = {
        "mcpServers": {
            "b": {"command": "x", "disabledTools": ["t"]},
            "a": {"command": "x", "disabledTools": ["t"]},
            "plain": {"command": "x"},
            "empty": {"command": "x", "disabledTools": []},
            "muted": {"command": "x", "disabledTools": ["t"], "disabled": True},
        }
    }
    assert tool_limited_servers(spec) == ("a", "b")


def test_saved_view_has_no_backend_warning_on_the_default_backend(editor):
    service, home, specs, parent = editor
    assert save(service, enroll=True)["backend_warning"] is None


def test_preview_and_save_warn_when_kas_cannot_apply_single_tool_limits(editor):
    service, home, specs, parent = editor
    _set_member_backend(home, "kas")
    body = {
        "revision": service.get("A")["revision"],
        "enroll": True,
        "operations": [_LIMITED_DOCS],
    }
    preview = service.preview("A", body)
    expected = {
        "code": "tool_limits_unsupported",
        "backend": "kas",
        "setting": "agent.member_acp_backend",
        "servers": ["docs"],
    }
    assert preview["backend_warning"] == expected
    # A warning, never a refusal: the save lands and its view repeats it.
    saved = service.put("A", {**body, "preview_token": preview["preview_token"]})
    assert saved["backend_warning"] == expected
    assert spec_for(home, specs)["mcpServers"]["docs"]["disabledTools"] == ["wipe"]
    assert service.get("A")["backend_warning"] == expected


def test_a_kiro_member_backend_is_judged_as_kas_for_single_tool_limits(editor):
    # A member DM set to "kiro" runs on KAS, so the warning names KAS, while the
    # same "kiro" in agent.acp_backend stays kiro-cli and honours the limits.
    service, home, specs, parent = editor
    config = json.loads((home / "config.json").read_text())
    config["agent"] = {"member_acp_backend": "kiro", "acp_backend": "kiro"}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    assert save(service, [_LIMITED_DOCS], enroll=True)["backend_warning"] == {
        "code": "tool_limits_unsupported",
        "backend": "kas",
        "setting": "agent.member_acp_backend",
        "servers": ["docs"],
    }


def test_unsupported_member_backend_warns_on_preview_and_save(editor):
    service, home, specs, parent = editor
    _set_member_backend(home, "opencode")
    body = {"revision": service.get("A")["revision"], "enroll": True}
    preview = service.preview("A", body)
    expected = {
        "code": "harness_unsupported",
        "backend": "opencode",
        "setting": "agent.member_acp_backend",
    }
    assert preview["backend_warning"] == expected
    saved = service.put("A", {**body, "preview_token": preview["preview_token"]})
    assert saved["backend_warning"] == expected


def test_default_backend_refusal_is_named_when_the_member_backend_is_fine(editor):
    # Slack threads, cron jobs and spawned sessions of the member start on
    # agent.acp_backend and reach the same chat-start gate.
    service, home, specs, parent = editor
    config = json.loads((home / "config.json").read_text())
    config["agent"] = {"member_acp_backend": "kas", "acp_backend": "opencode"}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    assert save(service, enroll=True)["backend_warning"] == {
        "code": "harness_unsupported",
        "backend": "opencode",
        "setting": "agent.acp_backend",
    }


def test_member_backend_refusal_is_named_before_the_default_backend(editor):
    service, home, specs, parent = editor
    config = json.loads((home / "config.json").read_text())
    config["agent"] = {"member_acp_backend": "goose", "acp_backend": "opencode"}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    warning = save(service, enroll=True)["backend_warning"]
    assert (warning["setting"], warning["backend"]) == ("agent.member_acp_backend", "goose")


def test_harness_warning_also_names_limited_servers(editor):
    # The switch the page suggests (kas) would refuse these next.
    service, home, specs, parent = editor
    _set_member_backend(home, "opencode")
    warning = save(service, [_LIMITED_DOCS], enroll=True)["backend_warning"]
    assert (warning["code"], warning["servers"]) == ("harness_unsupported", ["docs"])


def test_unenrolled_member_gets_no_backend_warning(editor):
    service, home, specs, parent = editor
    _set_member_backend(home, "opencode")
    assert service.get("A")["backend_warning"] is None


def _detach(service, member="A"):
    return service.detach(member, {"revision": service.get(member)["revision"]})


def test_stop_following_keeps_the_saved_values_as_the_members_own(editor):
    """Following the Parent is reversible. The member keeps exactly the bytes
    it runs today, and a later Parent change does not reach it."""
    service, home, specs, parent = editor
    save(
        service,
        [
            {"section": "prompt", "id": "prompt", "action": "set", "value": "mine"},
            {"section": "tools", "id": "read", "action": "remove"},
        ],
        enroll=True,
    )
    before = spec_for(home, specs)
    assert service.get("A")["detach_blocked"] is None
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    # The agents directory is shared with the Kiro CLI and IDE: stopping
    # adds, renames and removes no agent file there.
    agent_files = sorted(p.name for p in specs.iterdir())

    view = _detach(service)

    assert sorted(p.name for p in specs.iterdir()) == agent_files
    assert view["mode"] == "legacy_snapshot"
    assert agent_state.get_capabilities(target) is None
    assert agent_state.get_fork_info(target) == {"forked_from": "parent", "private_to": "A"}
    assert json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"] == target
    assert spec_for(home, specs) == before
    parent["prompt"] = "upstream"
    (specs / "parent.json").write_text(json.dumps(parent))
    assert service.get("A")["parent_changes"] == []
    assert spec_for(home, specs) == before
    # The way back in stays open and keeps every local choice.
    save(service, enroll=True)
    assert spec_for(home, specs)["prompt"] == "mine"
    assert spec_for(home, specs)["tools"] == []
    assert service.get("A")["mode"] == "inherited"


def test_stop_following_refuses_a_member_that_does_not_follow(editor):
    service, home, specs, parent = editor
    with pytest.raises(CapabilityError, match="not_inherited"):
        _detach(service)
    assert len(list(specs.glob("*.json"))) == 1


@pytest.mark.parametrize("body", [{}, {"revision": 1}, {"revision": "r", "enroll": False}, []])
def test_stop_following_rejects_a_malformed_request(editor, body):
    service, home, specs, parent = editor
    save(service, enroll=True)
    with pytest.raises(CapabilityError) as exc:
        service.detach("A", body)
    assert exc.value.status == 400
    assert service.get("A")["mode"] == "inherited"


def test_stop_following_refuses_a_stale_revision(editor):
    service, home, specs, parent = editor
    save(service, enroll=True)
    revision = service.get("A")["revision"]
    save(service, [{"section": "prompt", "id": "prompt", "action": "set", "value": "x"}])
    with pytest.raises(CapabilityError, match="stale_revision"):
        service.detach("A", {"revision": revision})
    assert service.get("A")["mode"] == "inherited"


def test_stop_following_never_freezes_unreviewed_bytes(editor):
    """A hand-edited private file is not what the owner reviewed, so it is
    not made the member's own. The intent stays until the drift is resolved."""
    service, home, specs, parent = editor
    save(service, enroll=True)
    spec = spec_for(home, specs)
    spec["prompt"] = "untracked"
    (specs / (spec["name"] + ".json")).write_text(json.dumps(spec))
    with pytest.raises(CapabilityError, match="materialization_changed"):
        _detach(service)
    assert service.get("A")["mode"] == "inherited"


def test_stop_following_refuses_a_pending_save(editor):
    service, home, specs, parent = editor
    save(service, enroll=True)
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    state = json.loads((home / "agent_model_state.json").read_text())
    state[target]["capabilities"]["status"] = "pending"
    (home / "agent_model_state.json").write_text(json.dumps(state))
    with pytest.raises(CapabilityError, match="materialization_pending"):
        _detach(service)
    assert agent_state.get_capabilities(target) is not None


def test_stop_following_works_when_the_parent_is_gone(editor):
    """A missing Parent is a reason to leave it, not a reason to be stuck."""
    service, home, specs, parent = editor
    save(service, enroll=True)
    before = spec_for(home, specs)
    (specs / "parent.json").unlink()
    assert service.get("A")["template"]["available"] is False
    view = _detach(service)
    assert view["mode"] == "legacy_snapshot"
    assert spec_for(home, specs) == before


def test_stop_following_leaves_the_spec_alone_and_runs_the_fork_refresh_on_a_newer_answer(
    editor, monkeypatch
):
    """With the Parent gone the member cannot reconcile a newer governance
    answer. Stopping follow writes only the sidecar entry (the spec bytes are
    untouched) and then runs the fork refresh, which governs every private
    copy that has no saved intent."""
    from kiro_crew import agent_capabilities as capabilities

    service, home, specs, parent = editor
    save(service, enroll=True)
    (specs / "parent.json").unlink()
    generation = capabilities.governance_answer_generation()
    monkeypatch.setattr(capabilities, "governance_answer_generation", lambda: generation + 1)
    monkeypatch.setattr(
        capabilities,
        "sanitize_agent_config_governance",
        lambda *a, **k: pytest.fail("detach must not rewrite the spec itself"),
    )
    calls = []
    monkeypatch.setattr(agent, "_refresh_forked_templates", lambda **kw: calls.append(kw))
    before = spec_for(home, specs)
    path = specs / (before["name"] + ".json")
    raw = path.read_bytes()

    assert _detach(service)["mode"] == "legacy_snapshot"

    assert path.read_bytes() == raw
    assert calls == [{}]


def test_stop_following_reruns_the_fork_refresh_when_it_failed_for_the_copy(editor, monkeypatch):
    """A missing Parent makes the refresh fail for a member that still has an
    intent. The current governance answer would skip the refresh, leaving chat
    start refusing, so a recorded failure for the copy triggers it too."""
    service, home, specs, parent = editor
    save(service, enroll=True)
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset({target}))
    calls = []
    monkeypatch.setattr(agent, "_refresh_forked_templates", lambda **kw: calls.append(kw))
    assert _detach(service)["mode"] == "legacy_snapshot"
    assert calls == [{}]


def test_stop_following_holds_the_spawn_gate_from_before_the_write_to_after_the_refresh(
    editor, monkeypatch
):
    """Between dropping the intent and the refresh a fork-backed session start
    must keep waiting, or it could run grants the refresh has not re-filtered."""
    from kiro_crew import agent_capabilities as capabilities

    service, home, specs, parent = editor
    save(service, enroll=True)
    generation = capabilities.governance_answer_generation()
    monkeypatch.setattr(capabilities, "governance_answer_generation", lambda: generation + 1)
    seen = {}
    real_write = agent_state._write

    def watching_write(state, *args, **kwargs):
        seen["at_write"] = agent._fork_refresh_settled.is_set()
        return real_write(state, *args, **kwargs)

    def watching_refresh(**kwargs):
        seen["at_refresh"] = agent._fork_refresh_settled.is_set()

    monkeypatch.setattr(agent_state, "_write", watching_write)
    monkeypatch.setattr(agent, "_refresh_forked_templates", watching_refresh)
    _detach(service)
    assert seen == {"at_write": False, "at_refresh": False}
    assert agent._fork_refresh_settled.is_set()


def test_stop_following_releases_the_spawn_gate_when_it_is_refused(editor):
    service, home, specs, parent = editor
    with pytest.raises(CapabilityError, match="not_inherited"):
        _detach(service)
    assert agent._fork_refresh_settled.is_set()


def test_stop_following_survives_a_failing_fork_refresh(editor, monkeypatch):
    """The refresh fails closed on its own (the copy's sessions stay blocked), so
    its failure never undoes or fails the stop."""
    from kiro_crew import agent_capabilities as capabilities

    service, home, specs, parent = editor
    save(service, enroll=True)
    generation = capabilities.governance_answer_generation()
    monkeypatch.setattr(capabilities, "governance_answer_generation", lambda: generation + 1)

    def boom(**kwargs):
        raise RuntimeError("refresh down")

    monkeypatch.setattr(agent, "_refresh_forked_templates", boom)
    assert _detach(service)["mode"] == "legacy_snapshot"


def test_stop_following_leaves_the_file_untouched_on_a_current_governance_answer(
    editor, monkeypatch
):
    service, home, specs, parent = editor
    save(service, enroll=True)
    before = spec_for(home, specs)
    path = specs / (before["name"] + ".json")
    raw = path.read_bytes()
    monkeypatch.setattr(
        agent,
        "_refresh_forked_templates",
        lambda **kw: pytest.fail("current governance needs no refresh"),
    )
    _detach(service)
    assert path.read_bytes() == raw


@pytest.mark.asyncio
async def test_http_stop_following_maps_each_refusal(editor, monkeypatch):
    """The detach route answers a bad body 400, a refusal by its code and an
    unreadable source 503, never a raw 500."""
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, _, _, _ = editor

    @web.middleware
    async def identity(request, handler):
        request["user"] = "owner"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    path = "/api/agents/A/capabilities/detach"
    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, data="{", headers={"content-type": "application/json"})
        assert (response.status, (await response.json())["code"]) == (400, "invalid_json")
        revision = (await (await client.get("/api/agents/A/capabilities")).json())["revision"]
        response = await client.post(path, json={"revision": revision})
        assert (response.status, (await response.json())["code"]) == (409, "not_inherited")

        def unreadable(*args, **kwargs):
            raise OSError("disk")

        monkeypatch.setattr(service, "detach", unreadable)
        response = await client.post(path, json={"revision": revision})
        assert (response.status, (await response.json())["code"]) == (
            503,
            "capabilities_unavailable",
        )


def test_a_detached_member_passes_the_chat_start_seam(editor):
    """After stopping follow, chat start takes the unenrolled path: the start
    seam names the member's own private copy and checks no saved intent."""
    service, home, specs, parent = editor
    save(service, enroll=True)
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    _detach(service)
    assert prepare_member_capabilities("A") == {
        "template": target,
        "revision": "",
        "status": "unverified",
    }
    # A hand edit of the independent copy does not block a start.
    spec = spec_for(home, specs)
    spec["prompt"] = "edited by hand"
    (specs / (target + ".json")).write_text(json.dumps(spec))
    assert prepare_member_capabilities("A")["status"] == "unverified"


def test_stop_following_refuses_single_tool_limits_an_independent_copy_cannot_keep(editor):
    """Chat start enforces single-tool limits only for a saved setup, so a
    detached copy would keep the limit in its file but lose the refusal."""
    service, home, specs, parent = editor
    save(
        service,
        [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": {"command": "search", "disabledTools": ["wipe"]},
            }
        ],
        enroll=True,
    )
    with pytest.raises(CapabilityError, match="detach_tool_limits_unkept"):
        _detach(service)
    assert service.get("A")["mode"] == "inherited"


def test_stop_following_refuses_a_removal_an_owned_template_refresh_would_restore(
    editor, monkeypatch
):
    from kiro_crew import agent_capabilities as capabilities

    service, home, specs, parent = editor
    save(service, [{"section": "tools", "id": "read", "action": "remove"}], enroll=True)
    monkeypatch.setattr(capabilities, "_parent_is_owned", lambda snap: True)
    with pytest.raises(CapabilityError, match="detach_removal_unkept"):
        _detach(service)
    assert service.get("A")["mode"] == "inherited"
    # The same removal under a template Crew does not own is kept as is.
    monkeypatch.setattr(capabilities, "_parent_is_owned", lambda snap: False)
    assert _detach(service)["mode"] == "legacy_snapshot"
    assert spec_for(home, specs)["tools"] == []


def test_stop_following_needs_confirmation_for_a_copy_that_also_reads_the_global_mcp_json(
    editor, monkeypatch
):
    """The fork refresh pins the global mcp.json merge off for a private copy
    without a saved intent. That changes which servers the member loads, so it
    needs the owner's confirmation; the spec itself is never touched."""
    service, home, specs, parent = editor
    del parent["includeMcpJson"]
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    save(service, enroll=True)
    assert "includeMcpJson" not in spec_for(home, specs)
    view = service.get("A")
    assert view["detach_mcp_json_loss"] is True
    assert view["detach_blocked"] is None
    revision = view["revision"]
    with pytest.raises(CapabilityError, match="detach_mcp_json_unkept"):
        service.detach("A", {"revision": revision})
    assert service.get("A")["mode"] == "inherited"
    with pytest.raises(CapabilityError, match="invalid_request"):
        service.detach("A", {"revision": revision, "accept_mcp_json": "yes"})
    path = specs / (spec_for(home, specs)["name"] + ".json")
    raw = path.read_bytes()
    calls = []
    monkeypatch.setattr(agent, "_refresh_forked_templates", lambda **kw: calls.append(kw))
    assert service.detach("A", {"revision": revision, "accept_mcp_json": True})["mode"] == (
        "legacy_snapshot"
    )
    # The refresh, not the stop, pins the merge off.
    assert path.read_bytes() == raw
    assert calls == [{}]


def test_a_member_that_does_not_read_the_global_mcp_json_needs_no_confirmation(editor):
    service, home, specs, parent = editor
    save(service, enroll=True)
    assert service.get("A")["detach_mcp_json_loss"] is False
    assert _detach(service)["mode"] == "legacy_snapshot"


def test_stop_following_stops_tracking_the_shipped_model_for_a_chosen_model(editor, monkeypatch):
    """A copy of an owned template can still track the shipped model. The fork
    refresh would replace a model the member chose once the intent is gone, so
    stopping follow turns the tracking off in the same write."""
    from kiro_crew import agent_capabilities as capabilities
    from kiro_crew.agent_materialization import fork_refresh

    service, home, specs, parent = editor
    save(
        service,
        [{"section": "model", "id": "model", "action": "set", "value": "mine-model"}],
        enroll=True,
    )
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    agent_state.set_model_managed(target, True)
    monkeypatch.setattr(capabilities, "_parent_is_owned", lambda snap: False)
    monkeypatch.setattr(fork_refresh, "OWNED_KIRO_AGENT_FILES", frozenset({"parent.json"}))
    before = spec_for(home, specs)

    _detach(service)

    assert agent_state.get_model_managed(target) is False
    assert spec_for(home, specs) == before
    assert spec_for(home, specs)["model"] == "mine-model"


def test_stop_following_leaves_model_tracking_alone_without_a_chosen_model(editor, monkeypatch):
    from kiro_crew import agent_capabilities as capabilities
    from kiro_crew.agent_materialization import fork_refresh

    service, home, specs, parent = editor
    save(service, enroll=True)
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    agent_state.set_model_managed(target, True)
    monkeypatch.setattr(capabilities, "_parent_is_owned", lambda snap: False)
    monkeypatch.setattr(fork_refresh, "OWNED_KIRO_AGENT_FILES", frozenset({"parent.json"}))
    _detach(service)
    assert agent_state.get_model_managed(target) is True


def test_stop_following_refuses_a_removal_under_an_owned_name_the_parent_check_misses(
    editor, monkeypatch
):
    """The fork refresh re-applies owned plumbing by origin NAME, whatever scope the
    parent came from. ``_parent_is_owned`` counts only a global-scope parent, so the
    refusal asks the refresh's own rule too, or a project-scoped parent at an owned
    name slips through and the refresh restores what the member removed."""
    from kiro_crew import agent_capabilities as capabilities
    from kiro_crew.agent_materialization import fork_refresh

    service, home, specs, parent = editor
    save(service, [{"section": "tools", "id": "read", "action": "remove"}], enroll=True)
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    monkeypatch.setattr(capabilities, "_parent_is_owned", lambda snap: False)
    monkeypatch.setattr(fork_refresh, "OWNED_KIRO_AGENT_FILES", frozenset({"parent.json"}))
    assert fork_refresh.fork_origin_is_owned(target) is True
    with pytest.raises(CapabilityError, match="detach_removal_unkept"):
        _detach(service)
    assert service.get("A")["mode"] == "inherited"
    # A set value is not a removal, so the same lineage detaches.
    save(service, [{"section": "tools", "id": "read", "action": "set", "value": True}])
    assert _detach(service)["mode"] == "legacy_snapshot"


def test_stop_following_keeps_the_intent_and_the_bytes_when_the_sidecar_write_fails(
    editor, monkeypatch
):
    """The sidecar entry is the only write, so a failed write leaves the saved
    intent and the reviewed spec bytes exactly as they were and a retry works."""
    service, home, specs, parent = editor
    save(service, enroll=True)
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    path = specs / (target + ".json")
    raw = path.read_bytes()
    real_write = agent_state._write

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(agent_state, "_write", failing_write)
    with pytest.raises(OSError):
        _detach(service)
    assert agent_state.get_capabilities(target) is not None
    assert path.read_bytes() == raw
    monkeypatch.setattr(agent_state, "_write", real_write)
    assert _detach(service)["mode"] == "legacy_snapshot"
    assert path.read_bytes() == raw
