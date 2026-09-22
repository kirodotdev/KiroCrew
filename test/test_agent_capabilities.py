"""Real-file capability editing with isolated config, sidecar and agent homes."""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_capabilities import (
    CapabilityError,
    CapabilityService,
    prepare_member_capabilities,
    validate_request,
)
from kiro_crew.config import loader


@pytest.fixture
def editor(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    specs = home / "agents"
    specs.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / "kiro"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr(agent_state, "_state_path", lambda: home / "agent_model_state.json")
    monkeypatch.setattr("kiro_crew.agent_capabilities.default_project_dir", lambda _: "")
    loader._invalidate_config_cache()
    config = {
        "agents": {
            "A": {"kiro_agent": "parent", "workspace": "default", "memory_store": "default"},
            "B": {"kiro_agent": "parent", "workspace": "default", "memory_store": "default"},
        },
        "dashboard": {"bot_name": "unchanged"},
    }
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    parent = {
        "includeMcpJson": False,
        "name": "parent",
        "prompt": "original",
        "tools": ["read"],
        "allowedTools": [],
        "mcpServers": {
            "search": {"command": "search", "args": ["old"], "env": {"API_KEY": "opaque-secret"}}
        },
        "resources": ["file://guide.md", "skill://manual/*"],
    }
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    return CapabilityService(), home, specs, parent


def save(service, operations=None, *, enroll=False, accept_parent=None, accept_members=None):
    body = {
        "revision": service.get("A")["revision"],
        "enroll": enroll,
        "operations": operations or [],
        "accept_parent": accept_parent or [],
        "accept_members": accept_members or [],
    }
    preview = service.preview("A", body)
    return service.put("A", {**body, "preview_token": preview["preview_token"]})


def spec_for(home, specs, member="A"):
    config = json.loads((home / "config.json").read_text())
    return json.loads((specs / (config["agents"][member]["kiro_agent"] + ".json")).read_text())


def test_preview_is_pure_and_redacts_transports(editor):
    service, home, specs, parent = editor
    current = service.get("A")
    # Normal gateway startup has already loaded/migrated the installation config.
    # Preview starts from the revision obtained by GET and must write nothing.
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    preview = service.preview("A", {"revision": current["revision"], "enroll": True})
    assert preview["mode"] == "inherited"
    assert "opaque-secret" not in json.dumps(preview)
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_enrollment_is_explicit_and_keeps_other_bindings(editor):
    service, home, specs, parent = editor
    with pytest.raises(CapabilityError, match="enrollment_required"):
        save(service)
    result = save(service, enroll=True)
    assert result["runtime"]["status"] == "pending"
    assert spec_for(home, specs)["prompt"] == "original"
    assert spec_for(home, specs, "B") == parent
    assert json.loads((home / "config.json").read_text())["dashboard"]["bot_name"] == "unchanged"
    prepared = prepare_member_capabilities("A")
    assert prepared["status"] == "unverified"
    assert prepared["revision"]


def test_whole_transport_replacement_and_custom_resources_survive(editor):
    service, home, specs, _ = editor
    save(
        service,
        [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": {"url": "https://example.test/mcp"},
            }
        ],
        enroll=True,
    )
    spec = spec_for(home, specs)
    assert spec["mcpServers"]["search"] == {"url": "https://example.test/mcp"}
    assert spec["resources"] == ["file://guide.md", "skill://manual/*"]


def test_tombstone_survives_parent_update_and_restore_is_per_item(editor):
    service, home, specs, parent = editor
    save(
        service,
        [
            {"section": "tools", "id": "read", "action": "remove"},
            {"section": "prompt", "id": "prompt", "action": "set", "value": "mine"},
        ],
        enroll=True,
    )
    parent["prompt"] = "upstream"
    parent["tools"].append("write")
    (specs / "parent.json").write_text(json.dumps(parent))
    view = service.get("A")
    assert any(c["id"] == "prompt" and c["conflict"] for c in view["parent_changes"])
    save(service, [{"section": "tools", "id": "read", "action": "inherit"}])
    spec = spec_for(home, specs)
    assert spec["tools"] == ["read"]
    assert spec["prompt"] == "mine"
    save(service, accept_parent=[{"section": "tools", "id": "write"}])
    assert spec_for(home, specs)["tools"] == ["read", "write"]


def test_accepting_a_conflicting_parent_change_keeps_the_local_value_until_inherit(editor):
    """Accepting only moves the reviewed Parent baseline. An explicit local
    value stays until the owner sets that row to Inherited, which then adopts
    the accepted Parent value without another review."""
    service, home, specs, parent = editor
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
        enroll=True,
    )
    parent["prompt"] = "upstream"
    (specs / "parent.json").write_text(json.dumps(parent))
    change = next(c for c in service.get("A")["parent_changes"] if c["id"] == "prompt")
    assert change["conflict"] is True
    view = save(service, accept_parent=[{"section": "prompt", "id": "prompt"}])
    assert spec_for(home, specs)["prompt"] == "mine"
    assert view["parent_changes"] == []
    assert next(r for r in view["rows"] if r["section"] == "prompt")["state"] == "local"
    save(service, [{"section": "prompt", "id": "prompt", "action": "inherit"}])
    assert spec_for(home, specs)["prompt"] == "upstream"


def test_stale_preview_parent_and_request_rejected(editor):
    service, home, specs, parent = editor
    body = {"revision": service.get("A")["revision"], "enroll": True}
    preview = service.preview("A", body)
    parent["prompt"] = "new"
    (specs / "parent.json").write_text(json.dumps(parent))
    with pytest.raises(CapabilityError, match="stale_revision"):
        service.put("A", {**body, "preview_token": preview["preview_token"]})
    body["revision"] = service.get("A")["revision"]
    with pytest.raises(CapabilityError, match="stale_preview"):
        service.put("A", {**body, "preview_token": preview["preview_token"]})
    assert len(list(specs.glob("*.json"))) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown": True},
        {"enroll": "true"},
        {"operations": [{"section": "prompt", "id": "prompt", "action": "set", "value": None}]},
        {"operations": [{"section": "prompt", "id": "prompt", "action": "inherit", "value": "x"}]},
    ],
)
def test_request_rejects_ambiguous_or_unknown_fields(extra):
    with pytest.raises(CapabilityError):
        validate_request({"revision": "r", **extra})


def test_legacy_enrollment_preserves_all_local_choices(editor):
    service, home, specs, parent = editor
    private = {**parent, "name": "private", "tools": [], "prompt": "legacy"}
    (specs / "private.json").write_text(json.dumps(private))
    agent_state.set_fork_info("private", "parent", "A")
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "private"
    (home / "config.json").write_text(json.dumps(config))
    loader._invalidate_config_cache()
    assert service.get("A")["mode"] == "legacy_snapshot"
    save(service, enroll=True)
    assert spec_for(home, specs)["tools"] == []
    assert spec_for(home, specs)["prompt"] == "legacy"
    save(service, [{"section": "tools", "id": "read", "action": "inherit"}])
    assert spec_for(home, specs)["tools"] == ["read"]


def test_wildcard_exclusion_is_refused(editor):
    service, _, specs, parent = editor
    parent["tools"] = ["*"]
    (specs / "parent.json").write_text(json.dumps(parent))
    with pytest.raises(CapabilityError, match="wildcard_exclusion_unrepresentable"):
        save(service, [{"section": "tools", "id": "read", "action": "remove"}], enroll=True)


def test_missing_parent_preserves_saved_private_spec(editor):
    service, home, specs, _ = editor
    save(service, enroll=True)
    old = spec_for(home, specs)
    (specs / "parent.json").unlink()
    view = service.get("A")
    assert not view["template"]["available"]
    assert view["template"]["error_code"] == "parent_missing"
    assert "warnings" not in view
    assert view["runtime"]["error_code"] == "parent_missing"
    assert "apply_mode" not in view["runtime"]
    with pytest.raises(CapabilityError, match="parent_missing"):
        save(service)
    assert spec_for(home, specs) == old


def test_corrupt_sidecar_is_not_legacy_mode(editor):
    service, home, _, _ = editor
    (home / "agent_model_state.json").write_text("{")
    with pytest.raises(ValueError):
        service.get("A")


def test_runtime_seam_refuses_unverified_bytes(editor):
    service, home, specs, _ = editor
    save(service, enroll=True)
    spec = spec_for(home, specs)
    path = specs / (spec["name"] + ".json")
    spec["prompt"] = "untracked"
    path.write_text(json.dumps(spec))
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")


def test_selected_parent_rows_can_be_accepted_for_multiple_members(editor):
    service, home, specs, parent = editor
    save(service, enroll=True)
    body = {"revision": service.get("B")["revision"], "enroll": True}
    preview = service.preview("B", body)
    service.put("B", {**body, "preview_token": preview["preview_token"]})
    parent["tools"].append("write")
    (specs / "parent.json").write_text(json.dumps(parent))
    save(service, accept_parent=[{"section": "tools", "id": "write"}], accept_members=["B"])
    assert spec_for(home, specs)["tools"] == ["read", "write"]
    assert spec_for(home, specs, "B")["tools"] == ["read", "write"]


@pytest.mark.parametrize("kept_members", [("A",), ("B",), ("A", "B")])
@pytest.mark.parametrize("failed_step", ["first_spec", "second_spec", "binding", "receipt"])
def test_batch_failure_preserves_reviewed_intent_until_binding_commit(
    editor, monkeypatch, kept_members, failed_step
):
    import kiro_crew.agent_capabilities as capabilities

    service, home, specs, parent = editor
    for member in ("A", "B"):
        body = {
            "revision": service.get(member)["revision"],
            "enroll": True,
            "operations": (
                [{"section": "tools", "id": "write", "action": "remove"}]
                if member in kept_members
                else []
            ),
        }
        preview = service.preview(member, body)
        service.put(member, {**body, "preview_token": preview["preview_token"]})
    old_specs = {name: spec_for(home, specs, name) for name in ("A", "B")}
    old_intents = {
        name: agent_state.get_capabilities(spec["name"]) for name, spec in old_specs.items()
    }
    parent["tools"].append("write")
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    body = {
        "revision": service.get("A")["revision"],
        "accept_parent": [{"section": "tools", "id": "write"}],
        "accept_members": ["B"],
        "operations": [{"section": "prompt", "id": "prompt", "action": "set", "value": "original"}],
    }
    expected_overrides = {name: dict(intent["overrides"]) for name, intent in old_intents.items()}
    expected_overrides["A"]["prompt"] = {"prompt": {"action": "set", "value": "original"}}
    preview = service.preview("A", body)
    assert {row["member"] for row in preview["impact"]} == {"A", "B"} - set(kept_members)
    before_config = (home / "config.json").read_bytes()
    with monkeypatch.context() as patch:
        calls = 0
        if failed_step in ("first_spec", "second_spec"):
            real_write = capabilities.atomic_write

            def fail_spec(path, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == (1 if failed_step == "first_spec" else 2):
                    raise OSError("batch storage unavailable")
                return real_write(path, *args, **kwargs)

            patch.setattr(capabilities, "atomic_write", fail_spec)
        elif failed_step == "binding":

            def fail_binding(*args, **kwargs):
                raise OSError("batch storage unavailable")

            patch.setattr(loader, "write_config_atomically", fail_binding)
        else:
            real_state_write = agent_state._write

            def fail_receipt(state):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("batch storage unavailable")
                real_state_write(state)

            patch.setattr(agent_state, "_write", fail_receipt)
        with pytest.raises(OSError, match="batch storage unavailable"):
            service.put("A", {**body, "preview_token": preview["preview_token"]})

    published = failed_step == "receipt"
    if not published:
        # A fresh review of the same selection must remain valid after failure.
        service.preview("A", {**body, "revision": service.get("A")["revision"]})
    assert ((home / "config.json").read_bytes() != before_config) is published
    for member, old_spec in old_specs.items():
        assert json.loads((specs / (old_spec["name"] + ".json")).read_text()) == old_spec
        current = spec_for(home, specs, member)
        intent = agent_state.get_capabilities(current["name"])
        assert (current["name"] != old_spec["name"]) is published
        assert ("write" in intent["accepted"]["tools"]) is published
        assert intent["overrides"] == (
            expected_overrides[member] if published else old_intents[member]["overrides"]
        )
        with pytest.raises(CapabilityError, match="materialization_pending"):
            prepare_member_capabilities(member)
        if not published:
            assert intent == {**old_intents[member], "status": "pending"}
        assert bool(service.get(member)["parent_changes"]) is not published

    # A restart/reconciliation must not silently accept a failed owner's batch.
    service = CapabilityService()
    before_retry = set(specs.glob("*.json"))
    for member in ("A", "B"):
        capabilities.reconcile_member_capabilities(member)
        assert prepare_member_capabilities(member)["status"] == "unverified"
        assert bool(service.get(member)["parent_changes"]) is not published
    assert set(specs.glob("*.json")) == before_retry
    if not published:
        save(
            service,
            body["operations"],
            accept_parent=body["accept_parent"],
            accept_members=["B"],
        )
    for member in ("A", "B"):
        current = spec_for(home, specs, member)
        intent = agent_state.get_capabilities(current["name"])
        assert intent["accepted"]["tools"]["write"] is True
        assert intent["overrides"] == expected_overrides[member]
        assert ("write" in current["tools"]) is (member not in kept_members)
        assert service.get(member)["parent_changes"] == []
        assert prepare_member_capabilities(member)["status"] == "unverified"
    # Genuine no-ops still reuse their generation, without dropping acceptance.
    final_specs = {name: spec_for(home, specs, name) for name in ("A", "B")}
    for member in ("A", "B"):
        capabilities.reconcile_member_capabilities(member)
        assert spec_for(home, specs, member) == final_specs[member]
    assert all(agent_state.get_fork_info(path.stem) for path in specs.glob("crew-*.json"))


@pytest.mark.asyncio
async def test_http_failed_batch_can_review_and_retry_the_same_acceptance(
    editor, capability_app, monkeypatch
):
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.agent_capabilities as capabilities

    service, home, specs, parent = editor
    app, refreshes = capability_app

    def setup():
        save(
            service,
            [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
            enroll=True,
        )
        body = {"revision": service.get("B")["revision"], "enroll": True}
        preview = service.preview("B", body)
        service.put("B", {**body, "preview_token": preview["preview_token"]})
        parent["prompt"] = "upstream"
        (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")

    await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:
        body = {
            "accept_parent": [{"section": "prompt", "id": "prompt"}],
            "accept_members": ["B"],
        }
        for fail in (True, False):
            response = await client.get("/api/agents/A/capabilities")
            current = await response.json()
            assert current["parent_changes"]
            body["revision"] = current["revision"]
            response = await client.post("/api/agents/A/capabilities/preview", json=body)
            assert response.status == 200
            preview = await response.json()
            with monkeypatch.context() as patch:
                if fail:

                    def fail_spec(*args, **kwargs):
                        raise OSError("batch storage unavailable")

                    patch.setattr(capabilities, "atomic_write", fail_spec)
                response = await client.put(
                    "/api/agents/A/capabilities",
                    json={**body, "preview_token": preview["preview_token"]},
                )
                assert response.status == (503 if fail else 200)
                result = await response.json()
            if fail:
                assert result == {
                    "error": "capabilities_unavailable",
                    "code": "capabilities_unavailable",
                }
                assert refreshes == []
        assert result["parent_changes"] == []
    assert (await asyncio.to_thread(spec_for, home, specs, "A"))["prompt"] == "mine"
    assert (await asyncio.to_thread(spec_for, home, specs, "B"))["prompt"] == "upstream"
    assert refreshes == ["agents"]


def test_failed_config_publication_preserves_all_valid_bindings(editor, monkeypatch):
    service, home, specs, parent = editor
    save(service, enroll=True)
    before = json.loads((home / "config.json").read_text())
    old_spec = spec_for(home, specs)
    body = {
        "revision": service.get("A")["revision"],
        "operations": [{"section": "prompt", "id": "prompt", "action": "set", "value": "new"}],
    }
    preview = service.preview("A", body)

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(loader, "write_config_atomically", fail)
    with pytest.raises(OSError):
        service.put("A", {**body, "preview_token": preview["preview_token"]})
    assert json.loads((home / "config.json").read_text()) == before
    assert spec_for(home, specs) == old_spec
    assert spec_for(home, specs, "B") == parent
    assert all(agent_state.get_fork_info(p.stem) for p in specs.glob("crew-*.json"))


def test_skills_preserve_manual_resources_and_do_not_change_permissions(editor):
    _, home, specs, _ = editor
    service = CapabilityService(catalog=lambda project: {"guide": "skill://guide/SKILL.md"})
    save(
        service, [{"section": "skills", "id": "guide", "action": "set", "value": True}], enroll=True
    )
    first = spec_for(home, specs)
    assert first["resources"] == ["file://guide.md", "skill://manual/*", "skill://guide/SKILL.md"]
    save(service, [{"section": "skills", "id": "guide", "action": "remove"}])
    second = spec_for(home, specs)
    assert second["resources"] == ["file://guide.md", "skill://manual/*"]
    for section in ("tools", "allowedTools", "mcpServers"):
        assert first[section] == second[section]


def test_background_refresh_preserves_tombstones_and_holds_expansions(editor):
    service, home, specs, parent = editor
    save(service, [{"section": "tools", "id": "read", "action": "remove"}], enroll=True)
    parent["prompt"] = "updated"
    parent["tools"].append("write")
    (specs / "parent.json").write_text(json.dumps(parent))
    agent._refresh_forked_templates()
    spec = spec_for(home, specs)
    assert spec["prompt"] == "updated"
    assert spec["tools"] == []
    assert prepare_member_capabilities("A")["status"] == "unverified"


def test_old_model_reset_cannot_override_inheritance(editor):
    service, home, specs, _ = editor
    save(service, enroll=True)
    old = spec_for(home, specs)
    with pytest.raises(CapabilityError, match="capabilities_editor_required"):
        agent.reset_agent_model(old["name"])
    assert spec_for(home, specs) == old


def test_transport_types_are_validated_before_any_write(editor):
    service, home, specs, _ = editor
    body = {
        "revision": service.get("A")["revision"],
        "enroll": True,
        "operations": [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": {"command": "search", "args": "not-an-array"},
            }
        ],
    }
    with pytest.raises(CapabilityError, match="invalid_transport"):
        service.preview("A", body)
    assert not (home / "agent_model_state.json").exists()
    assert len(list(specs.glob("*.json"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller,app_id,expected",
    [("owner", "", 200), ("viewer", "", 403), ("owner", "app", 403), ("", "", 403)],
)
async def test_http_owner_boundary(editor, caller, app_id, expected):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, _, _, _ = editor

    @web.middleware
    async def identity(request, handler):
        request["user"] = caller
        request["app"] = app_id
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/agents/A/capabilities")
        assert response.status == expected
        if expected != 200:
            for method, path in (
                ("post", "/api/agents/A/capabilities/preview"),
                ("put", "/api/agents/A/capabilities"),
            ):
                response = await getattr(client, method)(path, json={})
                assert response.status == 403
            return
        current = await response.json()
        body = {"revision": current["revision"], "enroll": True}
        response = await client.post("/api/agents/A/capabilities/preview", json=body)
        assert response.status == 200
        preview = await response.json()
        response = await client.put(
            "/api/agents/A/capabilities", json={**body, "preview_token": preview["preview_token"]}
        )
        assert response.status == 200
        saved = await response.json()
        assert saved["mode"] == "inherited"
        assert saved["runtime"]["status"] == "pending"
        prepared = await asyncio.to_thread(prepare_member_capabilities, "A")
        response = await client.patch(
            "/api/agents/detail/" + prepared["template"], json={"model": "auto"}
        )
        assert response.status == 409


def test_whole_reset_restores_parent_and_keeps_member_metadata(editor):
    service, home, specs, parent = editor
    save(
        service,
        [
            {"section": "prompt", "id": "prompt", "action": "set", "value": "mine"},
            {"section": "tools", "id": "read", "action": "remove"},
        ],
        enroll=True,
    )
    before = json.loads((home / "config.json").read_text())["agents"]["A"]
    target = before["kiro_agent"]
    service.reset("A", target)
    after = json.loads((home / "config.json").read_text())["agents"]["A"]
    assert {k: v for k, v in before.items() if k != "kiro_agent"} == {
        k: v for k, v in after.items() if k != "kiro_agent"
    }
    assert spec_for(home, specs)["prompt"] == parent["prompt"]
    assert spec_for(home, specs)["tools"] == parent["tools"]
    assert service.get("A")["mode"] == "inherited"


def test_publish_flattens_saved_snapshot_without_accepting_pending_parent(editor):
    service, home, specs, parent = editor
    save(service, enroll=True)
    parent["tools"].append("write")
    (specs / "parent.json").write_text(json.dumps(parent))
    target = spec_for(home, specs)["name"]
    result = service.publish("A", target, "published")
    assert result["template"] == "published"
    spec = spec_for(home, specs)
    assert spec["tools"] == ["read"]
    assert "capabilities" not in spec
    assert "private_to" not in spec
    assert agent_state.get_fork_info("published") is None
    assert service.get("A")["mode"] == "shared"
    assert spec_for(home, specs, "B") == parent


def test_derived_permissions_follow_owner_approval_edits(editor):
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    service, home, specs, parent = editor
    parent["permissions"] = {"rules": []}
    (specs / "parent.json").write_text(json.dumps(parent))
    save(
        service,
        [{"section": "allowedTools", "id": "@search", "action": "set", "value": True}],
        enroll=True,
    )
    spec = spec_for(home, specs)
    assert spec["permissions"] == derived_agent_permissions(["@search"], spec["name"])
    save(service, [{"section": "allowedTools", "id": "@search", "action": "remove"}])
    assert spec_for(home, specs)["permissions"] == {"rules": []}


def test_reset_reviews_the_parent_tools_settings_it_adopts(editor):
    service, home, specs, parent = editor
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
        enroll=True,
    )
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    parent["toolsSettings"] = {"execute_bash": {"allowedCommands": ["git status"]}}
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    with pytest.raises(CapabilityError, match="alternate_permissions_require_review"):
        service.reset("A", target)
    assert spec_for(home, specs)["prompt"] == "mine"


def test_reset_reviews_parent_permissions_the_derive_does_not_produce(editor):
    service, home, specs, parent = editor
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
        enroll=True,
    )
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    parent["permissions"] = {"rules": [{"capability": "fsWrite", "effect": "allow"}]}
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    with pytest.raises(CapabilityError, match="alternate_permissions_require_review"):
        service.reset("A", target)
    assert spec_for(home, specs)["prompt"] == "mine"


def test_reset_onto_reviewed_parent_keeps_derived_permissions(editor):
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    service, home, specs, parent = editor
    parent["allowedTools"] = ["@search"]
    parent["permissions"] = derived_agent_permissions(["@search"], "parent")
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
        enroll=True,
    )
    target = json.loads((home / "config.json").read_text())["agents"]["A"]["kiro_agent"]
    service.reset("A", target)
    spec = spec_for(home, specs)
    assert spec["prompt"] == parent["prompt"]
    assert spec["permissions"] == derived_agent_permissions(spec.get("allowedTools"), spec["name"])
    assert service.get("A")["mode"] == "inherited"


def test_ordinary_apply_leaves_untouched_approval_sections_alone(editor):
    service, home, specs, parent = editor
    parent["permissions"] = {"rules": [{"capability": "fsWrite", "effect": "allow"}]}
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "mine"}],
        enroll=True,
    )
    assert spec_for(home, specs)["prompt"] == "mine"


def test_policy_withheld_approval_never_resurrects_on_relaxation(editor):
    from dataclasses import replace

    from kiro_crew.platform.context import current_context, set_context
    from kiro_crew.platform.governance import parse_policy

    service, home, specs, parent = editor
    parent["allowedTools"] = ["@search"]
    (specs / "parent.json").write_text(json.dumps(parent))
    original = current_context()
    policy = parse_policy(
        {"version": 1, "boot": {"fail_closed": True}, "mcp": {"mode": "deny", "deny": ["@search"]}}
    )
    try:
        set_context(replace(original, governance=policy))
        save(service, enroll=True)
        assert spec_for(home, specs)["allowedTools"] == []
        set_context(original)
        agent._refresh_forked_templates()
        assert spec_for(home, specs)["allowedTools"] == []
    finally:
        set_context(original)


def test_project_parent_is_pinned_and_never_falls_back(editor, monkeypatch):
    service, home, specs, parent = editor
    project = home / "project"
    project_specs = project / ".kiro" / "agents"
    project_specs.mkdir(parents=True)
    project_parent = {**parent, "prompt": "project parent"}
    (project_specs / "parent.json").write_text(json.dumps(project_parent))
    config = json.loads((home / "config.json").read_text())
    config["workspaces"] = {"default": {"dir": str(project)}}
    (home / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities.default_project_dir", loader.default_project_dir
    )
    loader._invalidate_config_cache()
    assert service.get("A")["template"]["scope"] == "project"
    save(service, enroll=True)
    assert spec_for(home, specs)["prompt"] == "project parent"
    (project_specs / "parent.json").unlink()
    assert service.get("A")["template"]["error_code"] == "parent_identity_changed"
    with pytest.raises(CapabilityError, match="parent_identity_changed"):
        save(service)
    assert spec_for(home, specs)["prompt"] == "project parent"


@pytest.mark.parametrize("junk", ["{", "[]", "null"])
def test_unrelated_invalid_spec_does_not_block_resolution(editor, junk):
    service, _, specs, _ = editor
    (specs / "unrelated.json").write_text(junk)
    assert service.get("A")["template"]["name"] == "parent"
    save(service, enroll=True)
    assert service.get("A")["mode"] == "inherited"


def test_duplicate_declared_parent_still_refuses(editor):
    service, _, specs, parent = editor
    (specs / "duplicate.json").write_text(json.dumps(parent))
    with pytest.raises(CapabilityError, match="ambiguous_template_name"):
        service.get("A")


def test_unmasked_transport_roundtrip_keeps_arguments(editor):
    service, home, specs, parent = editor
    parent["mcpServers"]["search"].pop("env")
    (specs / "parent.json").write_text(json.dumps(parent))
    row = next(r for r in service.get("A")["rows"] if r["section"] == "mcpServers")
    assert row["value"]["args"] == ["old"]
    value = {**row["value"], "command": "new-command"}
    save(
        service,
        [{"section": "mcpServers", "id": "search", "action": "set", "value": value}],
        enroll=True,
    )
    assert spec_for(home, specs)["mcpServers"]["search"] == {
        "command": "new-command",
        "args": ["old"],
    }


def test_redacted_transport_is_never_written_as_placeholders(editor):
    service, _, specs, parent = editor
    parent["mcpServers"]["search"]["args"] = ["--key", "opaque-secret"]
    (specs / "parent.json").write_text(json.dumps(parent))
    row = next(r for r in service.get("A")["rows"] if r["section"] == "mcpServers")
    assert row["value"]["env"] == {"API_KEY": "[REDACTED]"}
    assert row["value"]["args"] == ["--key", "[REDACTED]"]
    with pytest.raises(CapabilityError, match="redacted_value_not_writable"):
        save(
            service,
            [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": {**row["value"], "command": "changed"},
                }
            ],
            enroll=True,
        )
    assert json.loads((specs / "parent.json").read_text()) == parent


def test_existing_connection_preserves_secret_transport_fields(editor):
    _, home, specs, parent = editor
    service = CapabilityService(connections=lambda: {"existing": parent["mcpServers"]["search"]})
    save(
        service,
        [{"section": "mcpServers", "id": "search", "action": "set", "connection_id": "existing"}],
        enroll=True,
    )
    assert spec_for(home, specs)["mcpServers"]["search"] == parent["mcpServers"]["search"]


@pytest.mark.parametrize("failed_step", ["spec", "receipt"])
def test_failed_reconciliation_recovers_on_retry(editor, monkeypatch, failed_step):
    import kiro_crew.agent_capabilities as capabilities

    service, home, specs, parent = editor
    save(service, enroll=True)
    old = spec_for(home, specs)
    parent["prompt"] = "new parent prompt"
    (specs / "parent.json").write_text(json.dumps(parent))
    with monkeypatch.context() as patch:
        if failed_step == "spec":

            def fail(*args, **kwargs):
                raise OSError("test storage failure")

            patch.setattr(capabilities, "atomic_write", fail)
        else:
            real_write = agent_state._write
            count = 0

            def fail_receipt(data):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("test receipt failure")
                real_write(data)

            patch.setattr(agent_state, "_write", fail_receipt)
        with pytest.raises(OSError):
            capabilities.reconcile_member_capabilities("A")
    with pytest.raises(CapabilityError, match="materialization_pending"):
        prepare_member_capabilities("A")
    assert json.loads((specs / (old["name"] + ".json")).read_text()) == old
    if failed_step == "spec":
        assert spec_for(home, specs)["name"] == old["name"]
    else:
        assert spec_for(home, specs)["name"] != old["name"]
    before_retry = set(specs.glob("*.json"))
    capabilities.reconcile_member_capabilities("A")
    if failed_step == "receipt":
        assert set(specs.glob("*.json")) == before_retry
    assert spec_for(home, specs)["prompt"] == "new parent prompt"
    assert prepare_member_capabilities("A")["status"] == "unverified"


def test_legacy_enrollment_preserves_all_noncapability_and_absent_fields(editor):
    service, home, specs, parent = editor
    private = {
        "name": "private",
        "prompt": None,
        "model": None,
        "includeMcpJson": True,
        "hooks": {"agentSpawn": [{"command": "custom-hook"}]},
        "description": "private description",
        "toolsSettings": {"read": {"setting": "custom"}},
    }
    (specs / "private.json").write_text(json.dumps(private))
    agent_state.set_fork_info("private", "parent", "A")
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "private"
    (home / "config.json").write_text(json.dumps(config))
    loader._invalidate_config_cache()
    save(service, enroll=True)
    effective = spec_for(home, specs)
    assert {k: v for k, v in effective.items() if k != "name"} == {
        k: v for k, v in private.items() if k != "name"
    }
    assert json.loads((specs / "private.json").read_text()) == private


def test_shared_enrollment_keeps_implicit_global_scope_unchanged(editor):
    service, home, specs, parent = editor
    parent.pop("includeMcpJson")
    (specs / "parent.json").write_text(json.dumps(parent))
    save(service, enroll=True)
    effective = spec_for(home, specs)
    assert {k: v for k, v in effective.items() if k != "name"} == {
        k: v for k, v in parent.items() if k != "name"
    }
    with pytest.raises(CapabilityError, match="global_mcp_exclusion_unrepresentable"):
        save(service, [{"section": "tools", "id": "read", "action": "remove"}])


def test_preview_and_publication_use_identical_governance_without_preview_audit(
    editor, monkeypatch
):
    from copy import deepcopy
    from dataclasses import replace

    from kiro_crew.platform import governance
    from kiro_crew.platform.context import current_context, set_context

    service, _, specs, parent = editor
    parent["allowedTools"] = ["@search"]
    parent["mcpServers"]["search"]["autoApprove"] = ["find"]
    (specs / "parent.json").write_text(json.dumps(parent))
    original = current_context()
    policy = governance.parse_policy(
        {"version": 1, "boot": {"fail_closed": True}, "mcp": {"mode": "deny", "deny": ["@search"]}}
    )
    try:
        set_context(replace(original, governance=policy))
        actual = deepcopy(parent)
        governance.sanitize_agent_config_governance(actual)
        audit_calls = []

        def audit_forbidden():
            audit_calls.append(True)
            raise AssertionError("preview must not emit an audit")

        with monkeypatch.context() as patch:
            patch.setattr(governance, "sel", audit_forbidden)
            projected = deepcopy(parent)
            governance.sanitize_agent_config_governance(projected, audit=False)
            assert projected == actual
            view = service.get("A")
            preview = service.preview("A", {"revision": view["revision"], "enroll": True})
            assert not any(
                r["section"] in ("allowedTools", "autoApprove") and r["present"]
                for r in preview["rows"]
            )
            assert audit_calls == []
    finally:
        set_context(original)


@pytest.mark.parametrize("connection_id", [[], {}, True, None, ""])
def test_connection_identity_requires_a_nonempty_string(connection_id):
    with pytest.raises(CapabilityError, match="invalid_connection_id"):
        validate_request(
            {
                "revision": "r",
                "operations": [
                    {
                        "section": "mcpServers",
                        "id": "search",
                        "action": "set",
                        "connection_id": connection_id,
                    }
                ],
            }
        )


def test_retain_paths_preserve_secrets_while_replacing_command(editor):
    service, home, specs, parent = editor
    parent["mcpServers"]["search"]["env"] = {"key/a~b": "opaque-secret"}
    (specs / "parent.json").write_text(json.dumps(parent))
    row = next(r for r in service.get("A")["rows"] if r["section"] == "mcpServers")
    value = {**row["value"], "command": "replacement"}
    save(
        service,
        [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": value,
                "retain_paths": ["/env/key~1a~0b"],
            }
        ],
        enroll=True,
    )
    assert spec_for(home, specs)["mcpServers"]["search"] == {
        "command": "replacement",
        "args": ["old"],
        "env": {"key/a~b": "opaque-secret"},
    }
    assert "opaque-secret" not in json.dumps(service.get("A"))


@pytest.mark.parametrize(
    "paths",
    [
        [""],
        ["/env/API_KEY", "/env/API_KEY"],
        ["/env", "/env/API_KEY"],
        ["/env/~2"],
        ["/args/99"],
        ["/command"],
        [True],
        "not-list",
        [],
    ],
)
def test_invalid_retain_paths_write_nothing(editor, paths):
    service, home, specs, parent = editor
    row = next(r for r in service.get("A")["rows"] if r["section"] == "mcpServers")
    with pytest.raises(CapabilityError):
        save(
            service,
            [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": row["value"],
                    "retain_paths": paths,
                }
            ],
            enroll=True,
        )
    assert not (home / "agent_model_state.json").exists()
    assert json.loads((specs / "parent.json").read_text()) == parent


def test_absent_scalars_are_editable_and_managed_rows_identify_ownership(editor):
    service, _, specs, parent = editor
    parent.pop("prompt")
    parent["mcpServers"]["kirocrew-core"] = {"command": "system", "args": []}
    (specs / "parent.json").write_text(json.dumps(parent))
    rows = service.get("A")["rows"]
    for section in ("prompt", "model"):
        row = next(r for r in rows if r["section"] == section)
        assert not row["present"] and row["value"] == ""
    row = next(r for r in rows if r["id"] == "kirocrew-core")
    assert row["managed"]
    assert all("editable" not in row and "locked_reason" not in row for row in rows)


def test_local_overlay_member_saves_in_overlay_and_preserves_base(editor):
    service, home, specs, _ = editor
    service.get("A")
    base = (home / "config.json").read_bytes()
    local = home / "config.local.json"
    local.write_text(
        json.dumps(
            {
                "agents": {"A": {"description": "local member"}},
                "dashboard": {"bot_name": "local name"},
            }
        )
    )
    loader._invalidate_config_cache()
    save(service, enroll=True)
    assert (home / "config.json").read_bytes() == base
    overlay = json.loads(local.read_text())
    assert overlay["dashboard"]["bot_name"] == "local name"
    assert overlay["agents"]["A"]["description"] == "local member"
    target = overlay["agents"]["A"]["kiro_agent"]
    assert target.startswith("crew-") and (specs / (target + ".json")).is_file()
    assert service.get("A")["mode"] == "inherited"
    service.reset("A", target)
    current = loader.KiroCrewConfig.load().agents["A"].kiro_agent
    service.publish("A", current, "from-local")
    assert loader.KiroCrewConfig.load().agents["A"].kiro_agent == "from-local"
    assert (home / "config.json").read_bytes() == base


def test_reconcile_changes_ordinary_parent_fields_in_new_generation_only(editor):
    from kiro_crew.agent_capabilities import reconcile_member_capabilities

    service, home, specs, parent = editor
    parent["description"] = "before"
    (specs / "parent.json").write_text(json.dumps(parent))
    save(service, enroll=True)
    old = spec_for(home, specs)
    old_path = specs / (old["name"] + ".json")
    old_bytes = old_path.read_bytes()
    files = set(specs.glob("*.json"))
    reconcile_member_capabilities("A")
    assert set(specs.glob("*.json")) == files
    assert spec_for(home, specs)["name"] == old["name"]
    parent["description"] = "after"
    (specs / "parent.json").write_text(json.dumps(parent))
    reconcile_member_capabilities("A")
    assert old_path.read_bytes() == old_bytes
    assert spec_for(home, specs)["name"] != old["name"]
    assert spec_for(home, specs)["description"] == "after"
    assert agent_state.get_fork_info(old["name"])["private_to"] == "A"


def test_worker_refresh_and_capability_publication_preserve_each_others_state(editor):
    service, home, specs, parent = editor
    parent["name"] = "kirocrew"
    default_path = specs / "kirocrew.json"
    default_path.write_text(json.dumps(parent), encoding="utf-8")
    agent._install_worker_agent()
    worker_path = specs / "kirocrew-worker.json"
    worker_before = json.loads(worker_path.read_text())
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "kirocrew-worker"
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "private intent"}],
        enroll=True,
    )
    private = spec_for(home, specs)
    private_path = specs / (private["name"] + ".json")
    private_bytes = private_path.read_bytes()
    state_before = json.loads(agent_state._state_path().read_text())
    assert state_before["kirocrew-worker"]["mirrored_from"] == agent.default_spec_fingerprint()
    assert state_before["kirocrew-worker"]["mirrored_stat"] == agent.default_spec_identity()

    parent["tools"].append("write")
    parent["allowedTools"].append("write")
    default_path.write_text(json.dumps(parent), encoding="utf-8")
    agent._install_worker_agent()
    state_after = json.loads(agent_state._state_path().read_text())
    mirrored = state_after["kirocrew-worker"]
    assert mirrored["mirrored_from"] == agent.default_spec_fingerprint()
    assert mirrored["mirrored_from"] != state_before["kirocrew-worker"]["mirrored_from"]
    assert mirrored["mirrored_stat"] == agent.default_spec_identity()
    assert state_after[private["name"]] == state_before[private["name"]]
    assert private_path.read_bytes() == private_bytes
    worker_after = json.loads(worker_path.read_text())
    assert "write" in worker_after["tools"]
    assert "write" in worker_after["allowedTools"]
    assert worker_after["mcpServers"] == worker_before["mcpServers"]
    assert {("tools", "write"), ("allowedTools", "write")} <= {
        (row["section"], row["id"]) for row in service.get("A")["parent_changes"]
    }

    # Cold-start reconciliation and another private save must not accept the grant.
    prepare_member_capabilities("A")
    save(
        service,
        [{"section": "prompt", "id": "prompt", "action": "set", "value": "private revision"}],
    )
    current = spec_for(home, specs)
    assert current["name"] != private["name"]
    assert current["prompt"] == "private revision"
    assert "write" not in current["tools"]
    assert "write" not in current["allowedTools"]
    assert private_path.read_bytes() == private_bytes
    assert json.loads(agent_state._state_path().read_text())["kirocrew-worker"] == mirrored
    assert json.loads(worker_path.read_text()) == worker_after


def test_uninstalled_app_namespace_cannot_be_forged(editor):
    service, _, _, _ = editor
    with pytest.raises(CapabilityError, match="app_transport_unavailable"):
        save(
            service,
            [
                {
                    "section": "mcpServers",
                    "id": "missing-app:bridge",
                    "action": "set",
                    "value": {"command": "fake"},
                }
            ],
            enroll=True,
        )


def test_publish_respects_reserved_legacy_default_binding(editor):
    service, home, specs, _ = editor
    save(service, enroll=True)
    config = json.loads((home / "config.json").read_text())
    config.setdefault("agent", {})["default_agent"] = "reserved-template"
    (home / "config.json").write_text(json.dumps(config))
    loader._invalidate_config_cache()
    with pytest.raises(CapabilityError, match="name_taken"):
        service.publish("A", spec_for(home, specs)["name"], "reserved-template")


@pytest.mark.parametrize(
    "name",
    ["CON", "con", "Con.json", "PRN", "aux.txt", "NUL", "COM1", "com1.json", "lpt9", "LPT1.md"],
)
def test_publish_refuses_windows_device_names_in_any_case_or_extension(editor, name):
    from unittest.mock import patch

    from kiro_crew import agent_capabilities

    # One shared definition: constants.WINDOWS_DEVICE_STEMS, not an inline copy.
    service, home, specs, _ = editor
    save(service, enroll=True)
    before = {p.name: p.read_bytes() for p in specs.iterdir()}
    with patch.object(
        agent_capabilities, "atomic_write", wraps=agent_capabilities.atomic_write
    ) as writer:
        with pytest.raises(CapabilityError, match="invalid_template_name"):
            service.publish("A", spec_for(home, specs)["name"], name)
    writer.assert_not_called()
    # Device-name exists() is not evidence of a directory entry on Windows.
    assert {p.name: p.read_bytes() for p in specs.iterdir()} == before


@pytest.mark.parametrize("name", ["COM10", "lpt10", "console", "auxiliary"])
def test_publish_allows_names_that_only_resemble_device_names(editor, name):
    service, home, specs, _ = editor
    save(service, enroll=True)
    result = service.publish("A", spec_for(home, specs)["name"], name)
    assert result["template"] == name
    assert (specs / f"{name}.json").exists()


def test_owned_parent_refreshes_real_plumbing_without_regranting_tools(editor, monkeypatch):
    import sys

    from kiro_crew.agent_capabilities import reconcile_member_capabilities

    service, home, specs, parent = editor
    parent.update(name="kirocrew", prompt="owner prompt", model="owner-selected-model")
    parent["mcpServers"] = {
        "kirocrew-core": {
            "command": "outdated",
            "args": ["old"],
            "env": {"KIROCREW_HOME": "outdated", "KEEP": "custom"},
        }
    }
    parent["hooks"] = {"agentSpawn": [{"command": "outdated"}]}
    (specs / "kirocrew.json").write_text(json.dumps(parent))
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "kirocrew"
    (home / "config.json").write_text(json.dumps(config))
    loader._invalidate_config_cache()
    monkeypatch.setattr(
        agent,
        "_MANAGED_MCP_SERVERS",
        {
            "kirocrew-core": {"command": sys.executable, "args": ["mcp-core"]},
            "kirocrew-cron": {"command": sys.executable, "args": ["mcp-cron"]},
        },
    )
    save(service, enroll=True)
    first = spec_for(home, specs)
    assert first["prompt"] == "owner prompt"
    assert first["model"] == "owner-selected-model"
    assert first["tools"] == ["read"]
    assert set(first["mcpServers"]) == {"kirocrew-core"}
    assert first["mcpServers"]["kirocrew-core"]["command"] == sys.executable
    assert first["mcpServers"]["kirocrew-core"]["env"]["KIROCREW_HOME"] == str(home)
    assert first["mcpServers"]["kirocrew-core"]["env"]["KEEP"] == "custom"
    assert first["hooks"] != parent["hooks"]
    path = specs / (first["name"] + ".json")
    old_bytes = path.read_bytes()
    monkeypatch.setattr(
        agent,
        "_MANAGED_MCP_SERVERS",
        {"kirocrew-core": {"command": sys.executable, "args": ["mcp-core", "--new"]}},
    )
    reconcile_member_capabilities("A")
    assert path.read_bytes() == old_bytes
    assert spec_for(home, specs)["mcpServers"]["kirocrew-core"]["args"] == ["mcp-core", "--new"]


def test_retained_secret_is_bound_to_member_revision(editor):
    service, _, specs, parent = editor
    current = service.get("A")
    row = next(r for r in current["rows"] if r["section"] == "mcpServers")
    body = {
        "revision": current["revision"],
        "enroll": True,
        "operations": [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": {**row["value"], "command": "new"},
                "retain_paths": ["/env/API_KEY"],
            }
        ],
    }
    preview = service.preview("A", body)
    parent["mcpServers"]["search"]["env"]["API_KEY"] = "rotated-secret"
    (specs / "parent.json").write_text(json.dumps(parent))
    with pytest.raises(CapabilityError, match="stale_revision"):
        service.put("A", {**body, "preview_token": preview["preview_token"]})


@pytest.mark.parametrize(
    "section,action", [("prompt", "set"), ("mcpServers", "inherit"), ("tools", "remove")]
)
def test_retain_paths_only_belong_to_transport_set(section, action):
    with pytest.raises(CapabilityError, match="invalid_retain_paths"):
        validate_request(
            {
                "revision": "r",
                "operations": [
                    {
                        "section": section,
                        "id": "x",
                        "action": action,
                        "value": {},
                        "retain_paths": [],
                    }
                ],
            }
        )


def test_mixed_layer_parent_acceptance_is_one_overlay_delta(editor):
    service, home, specs, parent = editor
    save(service, enroll=True)
    body = {"revision": service.get("B")["revision"], "enroll": True}
    preview = service.preview("B", body)
    service.put("B", {**body, "preview_token": preview["preview_token"]})
    local = home / "config.local.json"
    local.write_text(json.dumps({"agents": {"B": {"description": "local"}}}))
    loader._invalidate_config_cache()
    parent["tools"].append("write")
    (specs / "parent.json").write_text(json.dumps(parent))
    save(service, accept_parent=[{"section": "tools", "id": "write"}], accept_members=["B"])
    cfg = loader.KiroCrewConfig.load()
    for member in ("A", "B"):
        effective = json.loads((specs / (cfg.agents[member].kiro_agent + ".json")).read_text())
        assert effective["tools"] == ["read", "write"]
    assert set(json.loads(local.read_text())["agents"]) == {"A", "B"}


def test_prepare_detects_governance_change_during_spec_read(editor, monkeypatch):
    import kiro_crew.agent_capabilities as capabilities
    from kiro_crew.platform.context import current_context, set_context

    service, home, specs, _ = editor
    save(service, enroll=True)
    name = spec_for(home, specs)["name"]
    real_read = capabilities._read_spec
    original = current_context()

    def read_then_change(path):
        result = real_read(path)
        if path.name == name + ".json":
            set_context(original)
        return result

    monkeypatch.setattr(capabilities, "_read_spec", read_then_change)
    with pytest.raises(CapabilityError, match="governance_reconciliation_pending"):
        prepare_member_capabilities("A")


@pytest.mark.parametrize("stage", ["receipt", "spec", "binding", "completion", "crash"])
def test_publish_interruption_is_recoverable_with_same_name(editor, monkeypatch, stage):
    import kiro_crew.agent_capabilities as capabilities

    class Interrupted(BaseException):
        pass

    service, home, specs, _ = editor
    save(service, enroll=True)
    source = spec_for(home, specs)
    original_config = json.loads((home / "config.json").read_text())
    source_bytes = (specs / (source["name"] + ".json")).read_bytes()
    with monkeypatch.context() as patch:
        if stage in ("receipt", "completion", "crash"):
            write = agent_state._write
            calls = 0

            def fail_write(data):
                nonlocal calls
                calls += 1
                if calls == (1 if stage == "receipt" else 2):
                    if stage == "crash":
                        raise Interrupted()
                    raise OSError("test publication storage failure")
                write(data)

            patch.setattr(agent_state, "_write", fail_write)
        elif stage == "spec":

            def fail_spec(*args, **kwargs):
                raise OSError("test spec storage failure")

            patch.setattr(capabilities, "atomic_write", fail_spec)
        else:

            def fail_binding(*args, **kwargs):
                raise OSError("test config storage failure")

            patch.setattr(loader, "write_config_atomically", fail_binding)
        if stage == "completion":
            result = service.publish("A", source["name"], "published")
            assert result["warning"] == "publish_incomplete"
        else:
            with pytest.raises(Interrupted if stage == "crash" else OSError):
                service.publish("A", source["name"], "published")
    current_config = json.loads((home / "config.json").read_text())
    if stage in ("completion", "crash"):
        assert current_config["agents"]["A"]["kiro_agent"] == "published"
    else:
        assert current_config == original_config
    if stage != "receipt":
        assert agent_state.get_fork_info("published")["private_to"] == "A"
        assert agent_state.get_publish_info("published")["source"] == source["name"]
    assert (specs / (source["name"] + ".json")).read_bytes() == source_bytes
    # A new service simulates a restarted gateway with no in-memory receipt/key.
    retry = CapabilityService()
    result = retry.publish("A", source["name"], "published")
    assert result == {"ok": True, "template": "published", "filename": "published.json"}
    assert agent_state.get_fork_info("published") is None
    published = spec_for(home, specs)
    assert {k: v for k, v in published.items() if k != "name"} == {
        k: v for k, v in source.items() if k != "name"
    }
    config = json.loads((home / "config.json").read_text())
    assert config["agents"]["B"] == original_config["agents"]["B"]
    assert {k: v for k, v in config["agents"]["A"].items() if k != "kiro_agent"} == {
        k: v for k, v in original_config["agents"]["A"].items() if k != "kiro_agent"
    }
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert retry.publish("A", "published", "published")["ok"]
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("change", ["source", "destination", "binding", "owner"])
def test_publish_retry_does_not_override_a_concurrent_change(editor, monkeypatch, change):
    service, home, specs, _ = editor
    save(service, enroll=True)
    source = spec_for(home, specs)
    with monkeypatch.context() as patch:

        def fail_binding(*args, **kwargs):
            raise OSError("test config failure")

        patch.setattr(loader, "write_config_atomically", fail_binding)
        with pytest.raises(OSError):
            service.publish("A", source["name"], "published")
    if change in ("source", "destination"):
        path = specs / ((source["name"] if change == "source" else "published") + ".json")
        edited = json.loads(path.read_text())
        edited["prompt"] = "a concurrent owner edit"
        path.write_text(json.dumps(edited))
    elif change == "owner":
        agent_state.set_fork_info("published", "parent", "B")
    else:

        def rebind(document):
            document["agents"]["A"]["kiro_agent"] = "parent"
            return document

        loader.update_config_locked(mutate=rebind)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    with pytest.raises(CapabilityError):
        service.publish("A", source["name"], "published")
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    assert agent_state.get_fork_info("published") is not None


def test_publish_rechecks_binding_at_finalization(editor, monkeypatch):
    service, home, specs, _ = editor
    save(service, enroll=True)
    source = spec_for(home, specs)
    finish = service._finish_publish

    def rebind_then_finish(member, expected, name):
        def rebind(document):
            document["agents"][member]["kiro_agent"] = "parent"
            return document

        loader.update_config_locked(mutate=rebind)
        return finish(member, expected, name)

    monkeypatch.setattr(service, "_finish_publish", rebind_then_finish)
    with pytest.raises(CapabilityError, match="stale_binding"):
        service.publish("A", source["name"], "published")
    assert spec_for(home, specs)["name"] == "parent"
    assert agent_state.get_fork_info("published")["private_to"] == "A"


def test_publish_completion_preserves_other_sidecar_metadata(editor, monkeypatch):
    service, _, specs, _ = editor
    save(service, enroll=True)
    source = prepare_member_capabilities("A")["template"]
    finish = service._finish_publish

    def add_metadata_then_finish(member, expected, name):
        agent_state.set_model_managed(name, False)
        agent_state.set_cc_model(name, "owner-selection")
        return finish(member, expected, name)

    monkeypatch.setattr(service, "_finish_publish", add_metadata_then_finish)
    service.publish("A", source, "published")
    assert agent_state.get_model_managed("published") is False
    assert agent_state.get_cc_model("published") == "owner-selection"
    assert agent_state.get_fork_info("published") is None


@pytest.mark.asyncio
async def test_publish_http_retries_pending_and_completed_receipts(editor, monkeypatch):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, home, specs, _ = editor
    await asyncio.to_thread(save, service, enroll=True)
    source = (await asyncio.to_thread(spec_for, home, specs))["name"]

    @web.middleware
    async def owner(request, handler):
        request["user"], request["app"] = "owner", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    write = agent_state._write
    calls = 0

    def fail_completion_once(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("test completion failure")
        write(data)

    async with TestClient(TestServer(app)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(agent_state, "_write", fail_completion_once)
            response = await client.post(
                f"/api/agents/detail/{source}/publish", json={"crew": "A", "name": "published"}
            )
            assert response.status == 200
            assert (await response.json())["warning"] == "publish_incomplete"
        for target in ("published", source, "published"):
            response = await client.post(
                f"/api/agents/detail/{target}/publish", json={"crew": "A", "name": "published"}
            )
            assert response.status == 200
            assert await response.json() == {
                "ok": True,
                "template": "published",
                "filename": "published.json",
            }
        response = await client.get("/api/agents/A/capabilities")
        assert (await response.json())["mode"] == "shared"


def test_publish_rechecks_binding_before_staging(editor, monkeypatch):
    service, home, specs, _ = editor
    save(service, enroll=True)
    source = spec_for(home, specs)["name"]
    publish_bindings = service._write_bindings

    def race_rebind(prepared, mutate):
        def rebind(document):
            document["agents"]["A"]["kiro_agent"] = "parent"
            return document

        loader.update_config_locked(mutate=rebind)
        return publish_bindings(prepared, mutate)

    monkeypatch.setattr(service, "_write_bindings", race_rebind)
    with pytest.raises(CapabilityError, match="stale_binding"):
        service.publish("A", source, "published")
    assert not (specs / "published.json").exists()
    assert agent_state.get_publish_info("published") is None
    assert spec_for(home, specs)["name"] == "parent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,replacement,delete",
    [
        *(
            (("capabilities", field, section), None, True)
            for field in ("accepted", "overrides")
            for section in agent_state.CAPABILITY_SECTIONS
        ),
        *(
            (("capabilities", field, section), [], False)
            for field in ("accepted", "overrides")
            for section in agent_state.CAPABILITY_SECTIONS
        ),
        *((("capabilities", field), {}, False) for field in ("accepted", "overrides")),
        *(
            (
                ("capabilities", field, "mcpServers"),
                {
                    "search": (
                        transport if field == "accepted" else {"action": "set", "value": transport}
                    )
                },
                False,
            )
            for field in ("accepted", "overrides")
            for transport in (
                {"command": []},
                {"command": ""},
                {"url": []},
                {"url": ""},
                {"args": "--read"},
                {"args": [1]},
                {"env": []},
                {"env": {"TOKEN": []}},
                {"headers": []},
                {"headers": {"Authorization": 1}},
                {"type": False},
                {"type": ""},
                {"timeout": "30"},
                {"timeout": True},
                {"timeout": 0},
                {"timeout": -1},
                {"timeout": float("nan")},
                {"timeout": float("inf")},
                {"timeout": float("-inf")},
                {"disabled": "false"},
                {"disabledTools": "read"},
                {"disabledTools": [1]},
                {"oauthScopes": "read"},
                {"oauthScopes": [1]},
                {"oauth": []},
                {"oauth": {"clientId": []}},
                {"oauth": {"clientSecret": False}},
                {"oauth": {"redirectUri": []}},
                {"oauth": {"clientMetadataUrl": 1}},
                {"oauth": {"oauthScopes": "read"}},
                {"oauth": {"oauthScopes": [1]}},
            )
        ),
        *(
            (
                ("capabilities", field, section),
                {key: value if field == "accepted" else {"action": "set", "value": value}},
                False,
            )
            for field in ("accepted", "overrides")
            for section, key, value in (
                ("mcpServers", "search", []),
                ("mcpServers", "search", {"command": "search", "autoApprove": ["read"]}),
                ("resources", "file://guide.md", "file://different.md"),
                ("tools", "read", False),
                ("allowedTools", "read", 1),
                ("autoApprove", "@search/read", "true"),
                ("skills", "catalog/skill", True),
                ("prompt", "prompt", None),
                ("model", "model", {}),
                ("resources", "file://guide.md", True),
            )
        ),
        *(
            (("capabilities", "overrides", "tools"), {"read": override}, False)
            for override in (
                None,
                [],
                {},
                {"action": "inherit"},
                {"action": "unknown"},
                {"action": "set"},
                {"action": "remove", "value": True},
            )
        ),
        (("capabilities", "accepted", "unknown"), {}, False),
        (("capabilities", "parent"), {}, False),
        *(
            (("capabilities", "parent", field), None, True)
            for field in ("name", "scope", "source", "path", "project")
        ),
        *(
            (("capabilities", "parent", field), bad, False)
            for field in ("name", "scope", "source", "path", "project")
            for bad in (None, [], 7)
        ),
        *(
            (("capabilities", field), bad, False)
            for field, bad in (
                ("materialized", []),
                ("revision", []),
                ("catalog", []),
                ("catalog", {"guide": {}}),
                ("ordinary", []),
                ("ordinary_local", []),
                ("ordinary_local", {"description": "true"}),
                ("governance_generation", True),
                ("governance_generation", []),
            )
        ),
        (("capabilities", "overrides", "unknown"), {}, False),
        (("capabilities", "accepted", "prompt"), {"wrong": "text"}, False),
        (("capabilities", "overrides", "model"), {"wrong": {"action": "remove"}}, False),
        (("capabilities",), None, False),
        ((), None, False),
        ((), [], False),
    ],
)
async def test_nested_corruption_fails_closed_without_writes(editor, path, replacement, delete):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, home, specs, _ = editor

    def corrupt():
        saved = save(service, enroll=True)
        name = spec_for(home, specs)["name"]
        state_path = home / "agent_model_state.json"
        state = json.loads(state_path.read_text())
        # Start with the real writer's schema, not a hand-built legacy format.
        assert agent_state.get_capabilities(name) == state[name]["capabilities"]
        target = state
        keys = (name, *path)
        for key in keys[:-1]:
            target = target[key]
        if delete:
            del target[keys[-1]]
        else:
            target[keys[-1]] = replacement
        state_path.write_text(json.dumps(state))
        before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        with pytest.raises(ValueError, match="^capability_state_invalid$"):
            agent_state.get_capabilities(name)
        with pytest.raises(ValueError, match="^capability_state_invalid$"):
            service.get("A")
        from kiro_crew.agent_capabilities import reconcile_member_capabilities

        with pytest.raises(ValueError, match="^capability_state_invalid$"):
            reconcile_member_capabilities("A")
        with pytest.raises(ValueError, match="^capability_state_invalid$"):
            prepare_member_capabilities("A")
        return saved["revision"], name, before

    revision, name, before = await asyncio.to_thread(corrupt)

    @web.middleware
    async def identity(request, handler):
        request["user"] = "owner"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    async with TestClient(TestServer(app)) as client:
        for method, suffix, body in (
            ("get", "", None),
            ("post", "/preview", {"revision": revision}),
            ("put", "", {"revision": revision, "preview_token": "untrusted"}),
        ):
            response = await getattr(client, method)(
                "/api/agents/A/capabilities" + suffix, json=body
            )
            assert response.status == 503
            assert await response.json() == {
                "error": "capabilities_unavailable",
                "code": "capabilities_unavailable",
            }
        for action in ("reset", "publish"):
            response = await client.post(
                f"/api/agents/detail/{name}/{action}", json={"crew": "A", "name": "published"}
            )
            assert response.status == 503
            assert await response.json() == {
                "error": "capabilities_unavailable",
                "code": "capabilities_unavailable",
            }
    after = await asyncio.to_thread(
        lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    )
    assert after == before


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "transport",
    [
        {
            "command": "search",
            "args": ["--read"],
            "env": {"MODE": "read"},
            "type": "stdio",
            "timeout": 30,
            "disabled": False,
            "disabledTools": ["write"],
        },
        {
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "opaque-secret"},
            "timeout": 0.5,
            "oauthScopes": [],
            "oauth": {
                "clientId": "client",
                "clientSecret": "opaque-secret",
                "redirectUri": "http://localhost/callback",
                "clientMetadataUrl": "https://example.test/client.json",
                "oauthScopes": ["read"],
                "issuer": "https://example.test",
            },
        },
        {"command": "search", "mountOnly": True},
        {"disabled": True},
        {"type": "registry"},
        {},
    ],
)
def test_source_transport_fields_survive_enrollment_and_reconciliation(editor, legacy, transport):
    from kiro_crew.agent_capabilities import reconcile_member_capabilities

    service, home, specs, parent = editor
    parent["mcpServers"] = {"search": {**transport, "autoApprove": ["read"]}}
    (specs / "parent.json").write_text(json.dumps(parent))
    if legacy:
        (specs / "private.json").write_text(json.dumps({**parent, "name": "private"}))
        agent_state.set_fork_info("private", "parent", "A")
        config = json.loads((home / "config.json").read_text())
        config["agents"]["A"]["kiro_agent"] = "private"
        (home / "config.json").write_text(json.dumps(config))
        loader._invalidate_config_cache()
    save(service, enroll=True)
    first = spec_for(home, specs)
    intent = agent_state.get_capabilities(first["name"])
    assert intent["accepted"]["mcpServers"]["search"] == transport
    assert intent["accepted"]["autoApprove"]["@search/read"] is True
    if legacy:
        assert intent["overrides"]["mcpServers"]["search"] == {"action": "set", "value": transport}
    reconcile_member_capabilities("A")
    assert spec_for(home, specs)["mcpServers"]["search"] == first["mcpServers"]["search"]
    row = next(row for row in service.get("A")["rows"] if row["section"] == "mcpServers")
    assert row["value"].get("headers", {}) == {
        key: "[REDACTED]" for key in transport.get("headers", {})
    }
    assert "opaque-secret" not in json.dumps(service.get("A"))


@pytest.mark.parametrize("name", ["kirocrew-core", "test-app:search"])
def test_resolved_managed_and_app_transports_keep_metadata(editor, monkeypatch, name):
    service, home, specs, parent = editor
    transport = {"command": "search", "args": [], "env": {"MODE": "read"}, "mountOnly": True}
    parent["mcpServers"] = {name: transport}
    (specs / "parent.json").write_text(json.dumps(parent))
    monkeypatch.setattr(agent, "_collect_app_mcp_servers", lambda **kwargs: {name: transport})
    save(
        service,
        [{"section": "mcpServers", "id": name, "action": "set", "value": {"disabled": True}}],
        enroll=True,
    )
    spec = spec_for(home, specs)
    expected = {**transport, "disabled": True}
    intent = agent_state.get_capabilities(spec["name"])
    assert intent["overrides"]["mcpServers"][name]["value"] == expected
    assert spec["mcpServers"][name] == expected
    with pytest.raises(CapabilityError, match="managed_transport_locked|app_transport_locked"):
        save(
            service,
            [
                {
                    "section": "mcpServers",
                    "id": name,
                    "action": "set",
                    "value": {"command": "replacement"},
                }
            ],
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_editor_rejects_nonfinite_transport_timeout_without_writes(editor, timeout):
    service, home, _, _ = editor
    body = {
        "revision": service.get("A")["revision"],
        "enroll": True,
        "operations": [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": {"command": "search", "timeout": timeout},
            }
        ],
    }
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    with pytest.raises(CapabilityError, match="^invalid_body$"):
        service.preview("A", body)
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["opaque-oauth-credential", "q7!", ""])
async def test_oauth_secret_http_projection_and_retention(editor, secret):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    _, home, specs, parent = editor
    configured_secret = "configured-oauth-credential"
    rotated_secret = "r8?" if secret == "q7!" else "rotated-oauth-credential"
    service = CapabilityService(
        connections=lambda: {
            "configured": {
                "url": "https://example.test/mcp",
                "oauth": {"clientSecret": configured_secret, "oauthScopes": ["read"]},
            }
        }
    )
    original = {
        "command": "search",
        "args": ["--credential=" + secret],
        "oauth": {"clientId": "public-client", "clientSecret": secret, "oauthScopes": ["read"]},
    }

    def setup():
        parent["mcpServers"] = {"search": original}
        (specs / "parent.json").write_text(json.dumps(parent))

    await asyncio.to_thread(setup)

    @web.middleware
    async def identity(request, handler):
        request["user"] = "owner"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    async with TestClient(TestServer(app)) as client:

        async def view(method, suffix="", body=None):
            response = await getattr(client, method)(
                "/api/agents/A/capabilities" + suffix, json=body
            )
            assert response.status == 200
            data = await response.json()
            serialized = json.dumps(data)
            for credential in (secret, configured_secret, rotated_secret):
                if credential:
                    assert credential not in serialized
            assert str(home) not in serialized
            assert "source_digest" not in serialized
            return data

        current = await view("get")
        assert current["connections"] == [
            {"id": "configured", "label": "configured", "managed": False}
        ]
        row = next(row for row in current["rows"] if row["section"] == "mcpServers")
        assert row["value"]["oauth"]["clientSecret"] == ("[REDACTED]" if secret else "")
        assert row["value"]["oauth"]["clientId"] == "public-client"
        edited = {**row["value"], "command": "search-updated"}
        paths = ["/oauth/clientSecret", "/args/0"] if secret else []
        body = {
            "revision": current["revision"],
            "enroll": True,
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": edited,
                    "retain_paths": paths,
                }
            ],
        }
        preview = await view("post", "/preview", body)
        saved = await view("put", body={**body, "preview_token": preview["preview_token"]})
        actual = await asyncio.to_thread(spec_for, home, specs)
        assert actual["mcpServers"]["search"] == {**original, "command": "search-updated"}
        assert "[REDACTED]" not in json.dumps(actual)

        def change_parent():
            parent["mcpServers"]["search"] = {
                **original,
                "oauth": {"clientId": "public-client", "clientSecret": rotated_secret},
                "args": [rotated_secret],
                "metadata": {"nested": ["prefix:" + rotated_secret]},
            }
            (specs / "parent.json").write_text(json.dumps(parent))

        await asyncio.to_thread(change_parent)
        current = await view("get")
        change = next(
            change for change in current["parent_changes"] if change["section"] == "mcpServers"
        )
        assert change["conflict"] is True
        assert change["before"]["oauth"]["clientSecret"] == ("[REDACTED]" if secret else "")
        assert change["after"]["oauth"]["clientSecret"] == "[REDACTED]"
        assert change["after"]["metadata"] == {"nested": ["[REDACTED]"]}
        body = {
            "revision": current["revision"],
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "configured",
                    "action": "set",
                    "connection_id": "configured",
                }
            ],
        }
        preview = await view("post", "/preview", body)
        saved = await view("put", body={**body, "preview_token": preview["preview_token"]})
        actual = await asyncio.to_thread(spec_for, home, specs)
        assert actual["mcpServers"]["search"]["oauth"]["clientSecret"] == secret
        assert actual["mcpServers"]["configured"]["oauth"]["clientSecret"] == configured_secret
        assert "[REDACTED]" not in json.dumps(actual)
        await view("get")

        # Retention still authorizes only a currently masked scalar leaf.
        for path in ("/oauth/clientId", "/oauth/missing", "/oauth/clientSecret~2", "/oauth"):
            value = {
                "command": "search",
                "oauth": {
                    "clientId": "[REDACTED]",
                    "missing": "[REDACTED]",
                    "clientSecret": "[REDACTED]",
                },
            }
            bad = {
                "revision": saved["revision"],
                "operations": [
                    {
                        "section": "mcpServers",
                        "id": "search",
                        "action": "set",
                        "value": value,
                        "retain_paths": [path],
                    }
                ],
            }
            before = await asyncio.to_thread(
                lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
            )
            response = await client.post("/api/agents/A/capabilities/preview", json=bad)
            assert response.status == 400
            assert (
                await asyncio.to_thread(
                    lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
                )
                == before
            )

        # A valid signed preview cannot retain an old credential after disk changes.
        current = await view("get")
        row = next(
            row
            for row in current["rows"]
            if row["section"] == "mcpServers" and row["id"] == "search"
        )
        body = {
            "revision": current["revision"],
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": {**row["value"], "command": "another-command"},
                    "retain_paths": paths,
                }
            ],
        }
        preview = await view("post", "/preview", body)
        await asyncio.to_thread(
            lambda: (specs / "parent.json").write_text(json.dumps({**parent, "prompt": "changed"}))
        )
        before = await asyncio.to_thread(
            lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        )
        response = await client.put(
            "/api/agents/A/capabilities", json={**body, "preview_token": preview["preview_token"]}
        )
        assert response.status == 409
        assert (
            await asyncio.to_thread(
                lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
            )
            == before
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["env", "headers"])
@pytest.mark.parametrize("secret", ["~", "p7!", "abc123!", "abcdefgh", ""])
async def test_short_transport_credentials_http_retention(editor, field, secret):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, home, specs, parent = editor
    original = {
        "command": "search",
        "args": ["--key=" + secret],
        field: {"TOKEN": secret, "EMPTY": ""},
    }

    def setup():
        parent["mcpServers"] = {"search": {**original, "metadata": {"copies": [secret]}}}
        (specs / "parent.json").write_text(json.dumps(parent))

    await asyncio.to_thread(setup)

    @web.middleware
    async def owner(request, handler):
        request["user"], request["app"] = "owner", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    async with TestClient(TestServer(app)) as client:

        async def checked(response):
            assert response.status == 200
            data = await response.json()
            if secret:
                assert secret not in json.dumps(data)
            row = next(row for row in data["rows"] if row["section"] == "mcpServers")
            assert row["value"][field] == {"TOKEN": "[REDACTED]" if secret else "", "EMPTY": ""}
            assert row["value"]["args"] == (["[REDACTED]"] if secret else original["args"])
            assert (
                next(row for row in data["rows"] if row["section"] == "prompt")["value"]
                == "original"
            )
            return data, row["value"]

        current, shown = await checked(await client.get("/api/agents/A/capabilities"))
        assert shown["metadata"] == {"copies": ["[REDACTED]" if secret else ""]}
        # Metadata is preserved on source reads, but remains outside editor requests.
        edited = {"command": "search-updated", "args": shown["args"], field: shown[field]}
        body = {
            "revision": current["revision"],
            "enroll": True,
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": edited,
                    "retain_paths": [f"/{field}/TOKEN", "/args/0"] if secret else [],
                }
            ],
        }
        preview, _ = await checked(
            await client.post("/api/agents/A/capabilities/preview", json=body)
        )
        await checked(
            await client.put(
                "/api/agents/A/capabilities",
                json={**body, "preview_token": preview["preview_token"]},
            )
        )
        await checked(await client.get("/api/agents/A/capabilities"))
        actual = await asyncio.to_thread(spec_for, home, specs)
        assert actual["mcpServers"]["search"] == {**original, "command": "search-updated"}
        assert actual["mcpServers"]["search"][field]["TOKEN"].encode() == secret.encode()
        assert "[REDACTED]" not in json.dumps(actual)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,damage",
    [
        (action, damage)
        for action in ("reset", "publish")
        for damage in ("invalid_json", "capabilities", "absent_intent")
    ]
    + [("publish", "receipt"), ("publish", "receipt_legacy")],
)
async def test_legacy_actions_bound_sidecar_read_errors(editor, action, damage):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, home, specs, _ = editor

    def setup():
        if damage in ("absent_intent", "receipt_legacy"):
            service.get("A")
            source = "parent"
        else:
            save(service, enroll=True)
            source = spec_for(home, specs)["name"]
        path = home / "agent_model_state.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if damage == "invalid_json":
            path.write_text('{"secret": "synthetic-private-data",')
        elif damage == "capabilities":
            state[source]["capabilities"]["accepted"] = {}
            path.write_text(json.dumps(state))
        elif damage in ("receipt", "receipt_legacy"):
            state["published"] = {"publish": {"source": "synthetic-private-data"}}
            path.write_text(json.dumps(state))
        return source, {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    source, before = await asyncio.to_thread(setup)
    caller = "viewer"

    @web.middleware
    async def identity(request, handler):
        request["user"], request["app"] = caller, ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    async with TestClient(TestServer(app)) as client:
        path = f"/api/agents/detail/{source}/{action}"
        body = {"crew": "A", "name": "published"}
        response = await client.post(path, json=body)
        assert response.status == 403
        caller = "owner"
        response = await client.post(path, json=body)
        if damage == "absent_intent":
            assert response.status == 409
            assert (await response.json())["code"] == "not_a_private_copy"
        else:
            assert response.status == 503
            assert await response.json() == {
                "error": "capabilities_unavailable",
                "code": "capabilities_unavailable",
            }
    assert (
        await asyncio.to_thread(lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()})
        == before
    )


@pytest.fixture
def capability_app(editor):
    from types import SimpleNamespace

    from aiohttp import web

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, _, _, _ = editor
    service.get("A")
    refreshes = []

    @web.middleware
    async def owner(request, handler):
        request["user"], request["app"] = "owner", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=refreshes.append)
    app[_SERVICE] = service
    register(app)
    return app, refreshes


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix", [("post", "/preview"), ("put", "")])
async def test_http_invalid_json_preserves_saved_capabilities(
    editor, capability_app, method, suffix
):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, _ = editor
    app, refreshes = capability_app
    await asyncio.to_thread(save, service, enroll=True)

    def snapshot():
        return {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    before = await asyncio.to_thread(snapshot)
    async with TestClient(TestServer(app)) as client:
        response = await getattr(client, method)(
            "/api/agents/A/capabilities" + suffix,
            data='{"revision":',
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 400
        assert await response.json() == {"error": "invalid_json", "code": "invalid_json"}
    assert await asyncio.to_thread(snapshot) == before
    assert refreshes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "publish"])
async def test_http_inherited_actions_reject_wrong_member(editor, capability_app, action):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, _ = editor
    app, refreshes = capability_app
    await asyncio.to_thread(save, service, enroll=True)
    target = (await asyncio.to_thread(spec_for, home, specs))["name"]

    def snapshot():
        return {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    before = await asyncio.to_thread(snapshot)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/agents/detail/{target}/{action}", json={"crew": "B", "name": "published"}
        )
        assert response.status == 409
        assert await response.json() == {"error": "stale_binding", "code": "stale_binding"}
    assert await asyncio.to_thread(snapshot) == before
    assert refreshes == []


@pytest.mark.asyncio
async def test_http_whole_reset_restores_parent_without_changing_peer(editor, capability_app):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, parent = editor
    app, refreshes = capability_app
    await asyncio.to_thread(
        save,
        service,
        [
            {"section": "prompt", "id": "prompt", "action": "set", "value": "mine"},
            {"section": "tools", "id": "read", "action": "remove"},
        ],
        enroll=True,
    )
    before = await asyncio.to_thread(spec_for, home, specs)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/agents/detail/{before['name']}/reset", json={"crew": "A"}
        )
        assert response.status == 200
        result = await response.json()
        saved = await asyncio.to_thread(spec_for, home, specs)
        assert result == {"ok": True, "template": saved["name"]}
        assert saved["name"] != before["name"]
        assert saved["prompt"] == parent["prompt"]
        assert saved["tools"] == parent["tools"]
        response = await client.get("/api/agents/A/capabilities")
        assert response.status == 200
        view = await response.json()
        assert view["mode"] == "inherited"
        assert view["runtime"]["status"] == "pending"
    assert await asyncio.to_thread(spec_for, home, specs, "B") == parent
    assert refreshes == ["agents"]


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["mcpServers", "tools", "allowedTools", "autoApprove"])
@pytest.mark.parametrize("action", ["reconcile", "accept", "restore", "reset"])
@pytest.mark.parametrize("ambient", [False, True, None])
async def test_resolved_removals_respect_ambient_mcp(
    editor, capability_app, section, action, ambient, monkeypatch
):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.agent_capabilities import reconcile_member_capabilities
    from kiro_crew.config import live as _live
    from kiro_crew.config.loader import KiroCrewConfig as _Cfg

    # The subject is capability reconcile. The ``autoApprove`` parametrization
    # hand-writes a grant on a user server, which the undeclared-grant floor drops
    # before reconcile sees it, so the opt-in is pinned on for every case.
    _cfg = _Cfg()
    _cfg.mcp.honour_auto_approve = True
    monkeypatch.setattr(_live, "snapshot", lambda: _cfg)

    service, home, specs, parent = editor
    app, refreshes = capability_app
    key = {
        "mcpServers": "search",
        "tools": "read",
        "allowedTools": "@search",
        "autoApprove": "@search/find",
    }[section]

    ambient_enabled = ambient is not False

    def setup():
        if ambient is None:
            parent.pop("includeMcpJson")
        else:
            parent["includeMcpJson"] = ambient
        if section == "allowedTools":
            parent[section] = [key]
        if section == "autoApprove":
            parent["mcpServers"]["search"]["autoApprove"] = ["find"]
        (specs / "parent.json").write_text(json.dumps(parent))
        operations = []
        if action in ("restore", "reset"):
            value = parent[section][key] if section == "mcpServers" else True
            operations = [{"section": section, "id": key, "action": "set", "value": value}]
        save(service, operations, enroll=True)
        if section == "mcpServers":
            parent[section].pop(key)
        elif section == "autoApprove":
            parent["mcpServers"]["search"].pop("autoApprove")
        else:
            parent[section].remove(key)
        (specs / "parent.json").write_text(json.dumps(parent))
        return spec_for(home, specs)["name"]

    target = await asyncio.to_thread(setup)

    def snapshot():
        return {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    before = await asyncio.to_thread(snapshot)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/agents/A/capabilities")
        assert response.status == 200
        current = await response.json()
        body = {"revision": current["revision"]}
        if action == "restore":
            body["operations"] = [{"section": section, "id": key, "action": "inherit"}]
        elif action == "accept":
            body["accept_parent"] = [{"section": section, "id": key}]
        if action == "reset":
            responses = [
                await client.post(f"/api/agents/detail/{target}/reset", json={"crew": "A"})
            ]
        else:
            preview = await client.post("/api/agents/A/capabilities/preview", json=body)
            projected = await preview.json()
            responses = [preview]
            responses.append(
                await client.put(
                    "/api/agents/A/capabilities",
                    json={**body, "preview_token": projected.get("preview_token", "untrusted")},
                )
            )
        for response in responses:
            assert response.status == (409 if ambient_enabled else 200)
            if ambient_enabled:
                assert (await response.json())["code"] == "global_mcp_exclusion_unrepresentable"
        if action == "reconcile":
            if ambient_enabled:
                with pytest.raises(CapabilityError, match="global_mcp_exclusion_unrepresentable"):
                    await asyncio.to_thread(reconcile_member_capabilities, "A")
            else:
                await asyncio.to_thread(reconcile_member_capabilities, "A")
    if ambient_enabled:
        assert await asyncio.to_thread(snapshot) == before
        assert refreshes == []
    else:
        actual = await asyncio.to_thread(spec_for, home, specs)
        if section == "autoApprove":
            assert "find" not in actual["mcpServers"]["search"].get("autoApprove", [])
        else:
            assert key not in actual[section]
        assert actual["includeMcpJson"] is False
        assert (await asyncio.to_thread(prepare_member_capabilities, "A"))["template"] == actual[
            "name"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,masked",
    [
        (["--api-key", "p7!", "--copy", "p7!"], [1, 3]),
        (["--token=p7!", "--copy=p7!"], [0, 1]),
        (["--client-secret", "~", "prefix:~"], [1, 2]),
        (["--password=p7!", "--timeout", "5"], [0]),
        (["--apiKey=p7!", "--accessToken", "p7!"], [0, 2]),
        (["--api-key", "", "--verbose"], []),
        (["-e", "DB_PASSWORD=p7!", "--copy=p7!"], [1, 2]),
        (["--env", "DB_PASSWORD=p7!"], [1]),
        (["DB_PASSWORD=p7!", "API_TOKEN=p7!"], [0, 1]),
        (["DB_PASSWORD=p7!=tail="], [0]),
        (["--", "API_TOKEN=p7!", "server", "--copy=p7!"], [1, 3]),
        (["--", "-e", "DB_PASSWORD=p7!=tail=", "--user", "demo:ordinary"], [2]),
        (["--", "--api-key", "ordinary", "--user", "demo:ordinary", "MODE=dev"], []),
        (["DB_PASSWORD=", "MODE=dev", "PASSWORD", "ordinary"], []),
        (["9DB_PASSWORD=ordinary", "DB.PASSWORD=ordinary"], []),
    ],
)
async def test_argument_only_credentials_http_roundtrip(editor, capability_app, args, masked):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, parent = editor
    app, _ = capability_app
    original = {"command": "search", "args": args}
    secret = "~" if "~" in args else "p7!" if masked else ""
    if any("p7!=tail=" in arg for arg in args):
        secret = "p7!=tail="

    def setup():
        parent["mcpServers"] = {"search": {**original, "metadata": {"copy": secret}}}
        (specs / "parent.json").write_text(json.dumps(parent))

    await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:

        async def checked(response):
            assert response.status == 200
            data = await response.json()
            if secret:
                assert secret not in json.dumps(data)
            row = next(r for r in data["rows"] if r["section"] == "mcpServers")
            assert row["value"]["args"] == [
                "[REDACTED]" if i in masked else arg for i, arg in enumerate(args)
            ]
            return data, row["value"]

        current, shown = await checked(await client.get("/api/agents/A/capabilities"))
        assert shown["metadata"]["copy"] == ("[REDACTED]" if secret else "")
        body = {
            "revision": current["revision"],
            "enroll": True,
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": {"command": "updated", "args": shown["args"]},
                    "retain_paths": [f"/args/{i}" for i in masked],
                }
            ],
        }
        preview, _ = await checked(
            await client.post("/api/agents/A/capabilities/preview", json=body)
        )
        if masked:
            before = await asyncio.to_thread(_capability_data_snapshot, home)
            for paths, code in (
                ([], "unretained_redacted_value"),
                (["/args"], "invalid_retain_path"),
            ):
                invalid = {
                    **body,
                    "preview_token": preview["preview_token"],
                    "operations": [{**body["operations"][0], "retain_paths": paths}],
                }
                response = await client.put("/api/agents/A/capabilities", json=invalid)
                assert response.status == 400
                assert (await response.json())["code"] == code
                assert await asyncio.to_thread(_capability_data_snapshot, home) == before
        await checked(
            await client.put(
                "/api/agents/A/capabilities",
                json={**body, "preview_token": preview["preview_token"]},
            )
        )
        actual = await asyncio.to_thread(spec_for, home, specs)
        assert actual["mcpServers"]["search"] == {**original, "command": "updated"}
        assert [arg.encode() for arg in actual["mcpServers"]["search"]["args"]] == [
            arg.encode() for arg in args
        ]
        await checked(await client.get("/api/agents/A/capabilities"))
        assert (await asyncio.to_thread(prepare_member_capabilities, "A"))["template"] == actual[
            "name"
        ]
        # Even a valid retain pointer cannot be replayed across revisions.
        before = await asyncio.to_thread(
            lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        )
        response = await client.put(
            "/api/agents/A/capabilities", json={**body, "preview_token": preview["preview_token"]}
        )
        assert response.status == 409
        assert (await response.json())["code"] == "stale_revision"
        assert (
            await asyncio.to_thread(
                lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
            )
            == before
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["name", "scope", "source", "path", "project", "parent", "publish"]
)
async def test_publish_receipt_descriptor_corruption_is_bounded(editor, capability_app, field):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, _ = editor
    app, refreshes = capability_app

    def setup():
        save(service, enroll=True)
        service.publish("A", spec_for(home, specs)["name"], "published")
        state = agent_state._read(strict=True)
        if field == "publish":
            state["published"][field] = None
        elif field == "parent":
            state["published"]["publish"][field] = {}
        else:
            del state["published"]["publish"]["parent"][field]
        agent_state._write(state)
        return {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    before = await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/agents/detail/published/publish", json={"crew": "A", "name": "published"}
        )
        assert response.status == 503
        assert await response.json() == {
            "error": "capabilities_unavailable",
            "code": "capabilities_unavailable",
        }
    assert (
        await asyncio.to_thread(lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()})
        == before
    )
    assert refreshes == []


def test_optional_capability_metadata_and_empty_project_remain_valid(editor):
    service, home, specs, _ = editor
    save(service, enroll=True)
    name = spec_for(home, specs)["name"]
    state = agent_state._read(strict=True)
    intent = state[name]["capabilities"]
    assert intent["parent"]["project"] == ""
    for key in (
        "revision",
        "materialized",
        "catalog",
        "ordinary",
        "ordinary_local",
        "governance_generation",
    ):
        intent.pop(key, None)
    agent_state._write(state)
    assert agent_state.get_capabilities(name) == intent
    assert agent_state.get_capabilities("legacy") is None
    assert agent_state.get_publish_info("legacy") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("final_ambient", [False, True])
async def test_reset_checks_final_ambient_mode(editor, capability_app, final_ambient):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, parent = editor
    app, refreshes = capability_app

    def setup():
        parent["includeMcpJson"] = not final_ambient
        (specs / "parent.json").write_text(json.dumps(parent))
        save(
            service,
            [{"section": "tools", "id": "local", "action": "set", "value": True}],
            enroll=True,
        )
        target = spec_for(home, specs)["name"]
        parent["includeMcpJson"] = final_ambient
        (specs / "parent.json").write_text(json.dumps(parent))
        return target, {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    target, before = await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/agents/detail/{target}/reset", json={"crew": "A"})
        assert response.status == (409 if final_ambient else 200)
        if final_ambient:
            assert (await response.json())["code"] == "global_mcp_exclusion_unrepresentable"
            assert (
                await asyncio.to_thread(
                    lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
                )
                == before
            )
            assert refreshes == []
        else:
            actual = await asyncio.to_thread(spec_for, home, specs)
            assert actual["includeMcpJson"] is False
            assert "local" not in actual["tools"]
            assert (await asyncio.to_thread(prepare_member_capabilities, "A"))[
                "template"
            ] == actual["name"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section,kind",
    [
        ("mcpServers", "changed"),
        ("mcpServers", "removed"),
        ("mcpServers", "added"),
        ("prompt", "changed"),
        ("allowedTools", "added"),
    ],
)
@pytest.mark.parametrize("peer_action", [None, "set", "remove"])
async def test_http_inherit_and_accept_same_parent_change(
    editor, capability_app, section, kind, peer_action
):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.agent_capabilities import _rows

    service, home, specs, parent = editor
    app, refreshes = capability_app
    key = {"mcpServers": "search", "prompt": "prompt", "allowedTools": "@search/find"}[section]
    ref = {"section": section, "id": key}
    local_value = {"mcpServers": {"command": "local"}, "prompt": "local", "allowedTools": True}[
        section
    ]

    def setup():
        if kind == "added" and section == "mcpServers":
            parent[section].pop(key)
            (specs / "parent.json").write_text(json.dumps(parent))
        operation = {**ref, "action": "set", "value": local_value}
        save(service, [operation], enroll=True)
        peer_op = {**ref, "action": peer_action or "set"}
        if peer_op["action"] == "set":
            peer_op["value"] = local_value
        body = {"revision": service.get("B")["revision"], "enroll": True, "operations": [peer_op]}
        preview = service.preview("B", body)
        service.put("B", {**body, "preview_token": preview["preview_token"]})
        if section == "mcpServers":
            if kind == "removed":
                parent[section].pop(key)
            else:
                parent[section][key] = {"command": "upstream"}
        elif section == "prompt":
            parent[section] = "upstream"
        else:
            parent[section].append(key)
        parent["tools"].append("unreviewed")
        (specs / "parent.json").write_text(json.dumps(parent))
        peer = spec_for(home, specs, "B")
        return peer, agent_state.get_capabilities(peer["name"])

    peer_before, peer_intent = await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/agents/A/capabilities")
        current = await response.json()
        assert any(
            c["section"] == section and c["id"] == key and c["kind"] == kind
            for c in current["parent_changes"]
        )
        body = {
            "revision": current["revision"],
            "operations": [{**ref, "action": "inherit"}],
            "accept_parent": [ref],
            "accept_members": ["B"] if peer_action else [],
        }
        before = await asyncio.to_thread(
            lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        )
        response = await client.post("/api/agents/A/capabilities/preview", json=body)
        preview = await response.json()
        assert response.status == 200, preview
        assert (
            await asyncio.to_thread(
                lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
            )
            == before
        )
        response = await client.put(
            "/api/agents/A/capabilities", json={**body, "preview_token": preview["preview_token"]}
        )
        assert response.status == 200, await response.json()

    saved = await asyncio.to_thread(spec_for, home, specs)
    intent = await asyncio.to_thread(agent_state.get_capabilities, saved["name"])
    expected = _rows(parent, {})[section].get(key)
    assert _rows(saved, {})[section].get(key) == expected
    assert key not in intent["overrides"][section]
    assert intent["accepted"][section].get(key) == expected
    assert "unreviewed" not in saved["tools"]
    peer_after = await asyncio.to_thread(spec_for, home, specs, "B")
    assert (peer_after["name"] != peer_before["name"]) is bool(peer_action)
    assert peer_after == {**peer_before, "name": peer_after["name"]}
    assert (
        await asyncio.to_thread(
            lambda: json.loads((specs / (peer_before["name"] + ".json")).read_text())
        )
        == peer_before
    )
    if peer_action:
        if expected is None:
            peer_intent["accepted"][section].pop(key, None)
        else:
            peer_intent["accepted"][section][key] = expected
    actual_peer_intent = await asyncio.to_thread(agent_state.get_capabilities, peer_after["name"])
    assert actual_peer_intent["accepted"] == peer_intent["accepted"]
    assert actual_peer_intent["overrides"] == peer_intent["overrides"]
    assert refreshes == ["agents"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "unchanged", "peer_already_accepted"])
async def test_http_inherit_and_accept_rejects_missing_change(editor, capability_app, invalid):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, parent = editor
    app, refreshes = capability_app
    ref = {"section": "prompt", "id": "prompt"}
    if invalid == "missing":
        ref = {"section": "mcpServers", "id": "nonexistent"}

    def setup():
        save(service, enroll=True)
        if invalid == "peer_already_accepted":
            parent["prompt"] = "upstream"
            (specs / "parent.json").write_text(json.dumps(parent))
            body = {"revision": service.get("B")["revision"], "enroll": True}
            preview = service.preview("B", body)
            service.put("B", {**body, "preview_token": preview["preview_token"]})

    await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:
        current = await (await client.get("/api/agents/A/capabilities")).json()
        body = {
            "revision": current["revision"],
            "operations": [{**ref, "action": "inherit"}],
            "accept_parent": [ref],
            "accept_members": ["B"] if invalid == "peer_already_accepted" else [],
        }
        before = await asyncio.to_thread(
            lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        )
        for method, suffix in (("post", "/preview"), ("put", "")):
            response = await getattr(client, method)(
                "/api/agents/A/capabilities" + suffix,
                json={**body, **({"preview_token": "untrusted"} if method == "put" else {})},
            )
            assert response.status == 409
            assert (await response.json())["code"] == "parent_change_missing"
        assert (
            await asyncio.to_thread(
                lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
            )
            == before
        )
    assert refreshes == []


@pytest.mark.asyncio
async def test_http_inherit_and_accept_stale_revision_writes_nothing(editor, capability_app):
    from aiohttp.test_utils import TestClient, TestServer

    service, home, specs, parent = editor
    app, refreshes = capability_app
    ref = {"section": "mcpServers", "id": "search"}

    def setup():
        save(service, [{**ref, "action": "remove"}], enroll=True)
        body = {"revision": service.get("B")["revision"], "enroll": True}
        preview = service.preview("B", body)
        service.put("B", {**body, "preview_token": preview["preview_token"]})
        parent["mcpServers"]["search"] = {"command": "upstream"}
        (specs / "parent.json").write_text(json.dumps(parent))

    await asyncio.to_thread(setup)
    async with TestClient(TestServer(app)) as client:
        current = await (await client.get("/api/agents/A/capabilities")).json()
        body = {
            "revision": current["revision"],
            "operations": [{**ref, "action": "inherit"}],
            "accept_parent": [ref],
            "accept_members": ["B"],
        }
        response = await client.post("/api/agents/A/capabilities/preview", json=body)
        preview = await response.json()
        assert response.status == 200, preview
        parent["mcpServers"]["search"] = {"command": "newer"}
        await asyncio.to_thread((specs / "parent.json").write_text, json.dumps(parent))
        before = await asyncio.to_thread(
            lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        )
        for method, suffix in (("post", "/preview"), ("put", "")):
            response = await getattr(client, method)(
                "/api/agents/A/capabilities" + suffix,
                json={
                    **body,
                    **({"preview_token": preview["preview_token"]} if method == "put" else {}),
                },
            )
            assert response.status == 409
            assert (await response.json())["code"] == "stale_revision"
        assert (
            await asyncio.to_thread(
                lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
            )
            == before
        )
    assert refreshes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option,attached",
    [("-u", False), ("-u", True), ("--user", False), ("--user=", True)],
    ids=["split", "attached", "long-split", "long-equals"],
)
@pytest.mark.parametrize("credential", ["alice:p7!", "alice:", ":p7!", "alice: p7! ***"])
async def test_basic_auth_http_projection_retention_and_stale_refusal(
    editor, capability_app, option, attached, credential
):
    from aiohttp.test_utils import TestClient, TestServer

    _, home, specs, parent = editor
    app, refreshes = capability_app
    args = [option + credential] if attached else [option, credential]
    index = 0 if attached else 1
    shown_args = ["[REDACTED]"] if attached else [option, "[REDACTED]"]
    args.append("--copy=" + credential)
    shown_args.append("[REDACTED]")
    original = {"command": "curl", "args": args}
    parent["mcpServers"] = {"search": original}
    await asyncio.to_thread(lambda: (specs / "parent.json").write_text(json.dumps(parent)))
    endpoint = "/api/agents/A/capabilities"

    async def snapshot():
        return await asyncio.to_thread(
            lambda: {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        )

    async def checked(response):
        assert response.status == 200
        data = await response.json()
        assert credential not in json.dumps(data)
        assert "warnings" not in data
        assert set(data["runtime"]) == {"status", "saved_revision", "sessions"}
        row = next(row for row in data["rows"] if row["section"] == "mcpServers")
        assert row["value"]["args"] == shown_args
        return data, row["value"]

    async with TestClient(TestServer(app)) as client:
        current, shown = await checked(await client.get(endpoint))
        body = {
            "revision": current["revision"],
            "enroll": True,
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "search",
                    "action": "set",
                    "value": {**shown, "timeout": 30},
                    "retain_paths": [f"/args/{index}", f"/args/{index + 1}"],
                }
            ],
        }
        before = await snapshot()
        preview, _ = await checked(await client.post(endpoint + "/preview", json=body))
        assert await snapshot() == before
        assert refreshes == []
        for paths, code in [([], "unretained_redacted_value"), (["/args"], "invalid_retain_path")]:
            bad = {**body, "operations": [{**body["operations"][0], "retain_paths": paths}]}
            for method, suffix in [("post", "/preview"), ("put", "")]:
                response = await getattr(client, method)(
                    endpoint + suffix, json={**bad, "preview_token": preview["preview_token"]}
                )
                assert response.status == 400
                assert (await response.json())["code"] == code
                assert await snapshot() == before
                assert refreshes == []
        saved, _ = await checked(
            await client.put(endpoint, json={**body, "preview_token": preview["preview_token"]})
        )
        actual = await asyncio.to_thread(spec_for, home, specs)
        assert actual["mcpServers"]["search"] == {**original, "timeout": 30}
        assert [arg.encode() for arg in actual["mcpServers"]["search"]["args"]] == [
            arg.encode() for arg in args
        ]
        assert refreshes == ["agents"]
        await checked(await client.get(endpoint))
        body["revision"] = saved["revision"]
        body["operations"][0]["value"]["timeout"] = 40
        preview, _ = await checked(await client.post(endpoint + "/preview", json=body))
        parent["prompt"] = "changed since preview"
        await asyncio.to_thread(lambda: (specs / "parent.json").write_text(json.dumps(parent)))
        before = await snapshot()
        for method, suffix in [("post", "/preview"), ("put", "")]:
            response = await getattr(client, method)(
                endpoint + suffix, json={**body, "preview_token": preview["preview_token"]}
            )
            assert response.status == 409
            assert (await response.json())["code"] == "stale_revision"
            assert await snapshot() == before
            assert refreshes == ["agents"]


@pytest.mark.parametrize(
    "args",
    [
        ["-u", "script.py"],
        ["-u"],
        ["-u", ""],
        ["-ualice"],
        ["--", "-u", "alice:p7!"],
        ["--user", "alice"],
        ["--user=alice"],
        ["--user"],
        ["--user", ""],
        ["--user="],
        ["--", "--user", "alice:p7!"],
        ["--", "--user=alice:p7!"],
        ["--username", "alice:p7!"],
    ],
)
def test_basic_auth_short_option_does_not_guess_other_argv(args):
    from kiro_crew.agent_capabilities import safe_view

    transport = {"command": "python", "args": args}
    assert safe_view(transport) == transport


@pytest.mark.parametrize("args", [["--user", ":x"], ["--user=:x"]])
def test_basic_auth_nested_reflection_retains_original_scalars(args):
    from kiro_crew.agent_capabilities import _retain_transport, safe_view

    original = {"command": "other-program", "args": args, "metadata": {"copies": ["copy=:x"]}}
    shown = safe_view(original)
    index = len(args) - 1
    assert shown["args"][index] == "[REDACTED]"
    assert shown["metadata"] == {"copies": ["[REDACTED]"]}
    assert ":x" not in json.dumps(shown)
    assert _retain_transport(shown, [f"/args/{index}", "/metadata/copies/0"], original) == original


@pytest.mark.parametrize(
    "url",
    [
        "https://:q7!@example.test/mcp",
        "https://:x@example.test/mcp",
        "wss://:q7!@example.test/mcp",
        "wss://:x@example.test/mcp",
        "custom://:x@example.test/mcp",
    ],
)
def test_password_only_url_projection_and_retention(editor, url):
    service, home, specs, parent = editor
    parent["mcpServers"] = {"search": {"url": url}}
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    current = service.get("A")
    row = next(row for row in current["rows"] if row["section"] == "mcpServers")
    assert row["value"]["url"] == "[REDACTED]"
    body = {
        "revision": current["revision"],
        "enroll": True,
        "operations": [
            {
                "section": "mcpServers",
                "id": "search",
                "action": "set",
                "value": {"url": "[REDACTED]", "timeout": 30},
                "retain_paths": ["/url"],
            }
        ],
    }
    preview = service.preview("A", body)
    saved = service.put("A", {**body, "preview_token": preview["preview_token"]})
    for projection in (current, preview, saved):
        assert url not in json.dumps(projection)
        projected = next(row for row in projection["rows"] if row["section"] == "mcpServers")
        assert projected["value"]["url"] == "[REDACTED]"
    assert spec_for(home, specs)["mcpServers"]["search"] == {"url": url, "timeout": 30}


def _legacy_guard_fixture(editor):
    """A real private legacy copy; snapshot data, not advisory lock files."""
    _, home, specs, parent = editor
    (specs / "private.json").write_text(json.dumps({**parent, "name": "private"}))
    agent_state.set_fork_info("private", "parent", "A")
    loader.update_config_locked(
        mutate=lambda data: {
            **data,
            "agents": {**data["agents"], "A": {**data["agents"]["A"], "kiro_agent": "private"}},
        }
    )
    return home / "agent_model_state.json"


def _capability_data_snapshot(home):
    return {p: p.read_bytes() for p in home.rglob("*.json") if p.is_file()}


async def _legacy_guard_request(client, action):
    if action == "patch":
        return await client.patch("/api/agents/detail/private", json={"model": "auto"})
    if action in ("switch", "generic"):
        body = {"kiro_agent": "parent"}
        if action == "generic":
            body["description"] = "updated"
        return await client.put("/api/agents/A", json=body)
    return await client.post(
        f"/api/agents/detail/private/{action}", json={"crew": "A", "name": "published"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["patch", "switch", "generic", "reset", "publish"])
@pytest.mark.parametrize("damage", ["json", "shape", "unreadable"])
async def test_legacy_guard_http_unavailable_no_data_write(
    editor, capability_app, monkeypatch, action, damage
):
    from aiohttp.test_utils import TestClient, TestServer

    _, home, _, _ = editor
    app, refreshes = capability_app
    path = await asyncio.to_thread(_legacy_guard_fixture, editor)
    if damage != "unreadable":
        await asyncio.to_thread(path.write_text, "{" if damage == "json" else "[]")
    else:
        real_open = agent_state.os.open

        def deny_state(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError("synthetic-private-data")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(agent_state.os, "open", deny_state)
    before = await asyncio.to_thread(_capability_data_snapshot, home)
    async with TestClient(TestServer(app)) as client:
        response = await _legacy_guard_request(client, action)
        assert response.status == 503
        assert await response.json() == {
            "error": "capabilities_unavailable",
            "code": "capabilities_unavailable",
        }
    assert await asyncio.to_thread(_capability_data_snapshot, home) == before
    assert refreshes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["patch", "switch", "generic"])
@pytest.mark.parametrize("enrolled", [False, True])
async def test_legacy_guard_http_healthy_controls(
    editor, capability_app, monkeypatch, action, enrolled
):
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import agents as handlers

    service, home, specs, _ = editor
    app, _ = capability_app
    await asyncio.to_thread(_legacy_guard_fixture, editor)

    def pin_model():
        path = specs / "private.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["model"] = "test-pinned-model"
        path.write_text(json.dumps(spec), encoding="utf-8")
        agent_state.set_model_managed("private", False)

    await asyncio.to_thread(pin_model)
    monkeypatch.setattr(handlers, "agent_skill_keys", lambda *args: [])
    monkeypatch.setattr(handlers, "_refresh_session_defaults", AsyncMock())
    if enrolled:
        await asyncio.to_thread(save, service, enroll=True)
    target = (await asyncio.to_thread(spec_for, home, specs))["name"]
    before = await asyncio.to_thread(_capability_data_snapshot, home)
    async with TestClient(TestServer(app)) as client:
        if action == "patch":
            response = await client.patch(f"/api/agents/detail/{target}", json={"model": ""})
        else:
            response = await _legacy_guard_request(client, action)
        assert response.status == (409 if enrolled else 200), await response.text()
    if enrolled:
        assert await asyncio.to_thread(_capability_data_snapshot, home) == before
    elif action == "patch":
        assert "model" not in (await asyncio.to_thread(spec_for, home, specs))
        assert await asyncio.to_thread(agent_state.get_model_managed, target) is True
    else:
        assert (await asyncio.to_thread(spec_for, home, specs))["name"] == "parent"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "enrolled"])
async def test_legacy_patch_final_guard_precedes_bookkeeping(
    editor, capability_app, monkeypatch, failure
):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import agents as handlers

    _, home, _, _ = editor
    app, refreshes = capability_app
    await asyncio.to_thread(_legacy_guard_fixture, editor)
    before = await asyncio.to_thread(_capability_data_snapshot, home)
    original = handlers.require_unmanaged_template
    calls = 0

    def final_guard(name):
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "enrolled":
                raise CapabilityError("capabilities_editor_required")
            with monkeypatch.context() as patch:

                def unreadable(**kwargs):
                    raise OSError("synthetic-private-data")

                patch.setattr(agent_state, "_read", unreadable)
                return original(name)
        return original(name)

    monkeypatch.setattr(handlers, "require_unmanaged_template", final_guard)
    monkeypatch.setattr(handlers, "agent_skill_keys", lambda *args: [])
    async with TestClient(TestServer(app)) as client:
        response = await _legacy_guard_request(client, "patch")
        assert response.status == (503 if failure == "unavailable" else 409)
    assert calls == 2
    assert await asyncio.to_thread(_capability_data_snapshot, home) == before
    assert refreshes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["switch", "reset", "publish"])
async def test_legacy_rebind_late_unavailable_preserves_binding_and_rolls_back(
    editor, capability_app, monkeypatch, action
):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import agents as handlers

    _, home, specs, _ = editor
    app, refreshes = capability_app
    await asyncio.to_thread(_legacy_guard_fixture, editor)
    before = await asyncio.to_thread(_capability_data_snapshot, home)
    original = handlers._rebind_crew_locked
    staged = []

    def fail_at_rebind(*args, **kwargs):
        staged.append((specs / "published.json").exists())
        with monkeypatch.context() as patch:

            def unreadable(name):
                raise OSError("synthetic-private-data")

            patch.setattr(agent_state, "get_capabilities", unreadable)
            return original(*args, **kwargs)

    monkeypatch.setattr(handlers, "_rebind_crew_locked", fail_at_rebind)
    async with TestClient(TestServer(app)) as client:
        response = await _legacy_guard_request(client, action)
        assert response.status == 503
        assert await response.json() == {
            "error": "capabilities_unavailable",
            "code": "capabilities_unavailable",
        }
    # Publish had staged a private destination, so this is rollback, not zero writes.
    assert staged == [action == "publish"]
    assert await asyncio.to_thread(_capability_data_snapshot, home) == before
    assert refreshes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["stem", "declared"])
@pytest.mark.parametrize("enrolled", [False, True])
async def test_legacy_patch_divergent_file_identity(
    editor, capability_app, monkeypatch, address, enrolled
):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import agents as handlers

    service, home, specs, parent = editor
    app, refreshes = capability_app

    def setup():
        if enrolled:
            save(service, enroll=True)
            spec = spec_for(home, specs)
        else:
            spec = {**parent, "name": "private"}
        path = specs / (spec["name"] + ".json")
        spec["name"] = "hand-edited-name"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    path = await asyncio.to_thread(setup)
    before = await asyncio.to_thread(_capability_data_snapshot, home)
    bookkeeping = []
    for method in ("set_model_managed", "lift_and_strip_bookkeeping"):
        original = getattr(agent_state, method)

        def record(*args, _method=method, _original=original, **kwargs):
            bookkeeping.append(_method)
            return _original(*args, **kwargs)

        monkeypatch.setattr(agent_state, method, record)
    monkeypatch.setattr(handlers, "agent_skill_keys", lambda *args: [])
    target = path.stem if address == "stem" else "hand-edited-name"
    async with TestClient(TestServer(app)) as client:
        response = await client.patch(f"/api/agents/detail/{target}", json={"model": "auto"})
        assert response.status == (409 if enrolled else 200), await response.text()
        if enrolled:
            assert await response.json() == {
                "error": "capabilities_editor_required",
                "code": "capabilities_editor_required",
            }
    if enrolled:
        assert await asyncio.to_thread(_capability_data_snapshot, home) == before
        assert bookkeeping == []
        assert refreshes == []
    else:
        saved = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        assert saved["name"] == "hand-edited-name"
        assert saved["model"] == "auto"
        assert await asyncio.to_thread(agent_state.get_model_managed, "hand-edited-name") is False
        assert bookkeeping == ["set_model_managed", "lift_and_strip_bookkeeping"]
        assert refreshes == ["agents"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change_on_read", [1, 2, 3], ids=["initial", "reread", "final"])
@pytest.mark.parametrize("model", ["auto", ""])
async def test_legacy_patch_checks_fresh_declared_identity_before_bookkeeping(
    editor, capability_app, monkeypatch, change_on_read, model
):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import agents as handlers

    service, home, specs, parent = editor
    app, refreshes = capability_app
    await asyncio.to_thread(save, service, enroll=True)
    managed = (await asyncio.to_thread(spec_for, home, specs))["name"]
    path = specs / "private.json"
    await asyncio.to_thread(path.write_text, json.dumps({**parent, "name": "private"}))
    original = handlers._read_agent_spec
    reads = 0
    changed_snapshot = None

    def read_with_name_change(file, **kwargs):
        nonlocal reads, changed_snapshot
        if file == path:
            reads += 1
            if reads == change_on_read:
                # A real file edit between request lookup and the final locked read.
                data = json.loads(path.read_text(encoding="utf-8"))
                data["name"] = managed
                path.write_text(json.dumps(data), encoding="utf-8")
                changed_snapshot = _capability_data_snapshot(home)
        return original(file, **kwargs)

    def unexpected_bookkeeping(*args, **kwargs):
        pytest.fail("managed identity must be checked before model or lineage writes")

    monkeypatch.setattr(handlers, "_read_agent_spec", read_with_name_change)
    monkeypatch.setattr(handlers, "agent_skill_keys", lambda *args: [])
    monkeypatch.setattr(handlers, "clear_model_pin", unexpected_bookkeeping)
    monkeypatch.setattr(agent_state, "set_model_managed", unexpected_bookkeeping)
    monkeypatch.setattr(agent_state, "lift_and_strip_bookkeeping", unexpected_bookkeeping)
    async with TestClient(TestServer(app)) as client:
        response = await client.patch("/api/agents/detail/private", json={"model": model})
        assert response.status == 409, await response.text()
        assert await response.json() == {
            "error": "capabilities_editor_required",
            "code": "capabilities_editor_required",
        }
    assert changed_snapshot is not None
    assert await asyncio.to_thread(_capability_data_snapshot, home) == changed_snapshot
    assert refreshes == []
