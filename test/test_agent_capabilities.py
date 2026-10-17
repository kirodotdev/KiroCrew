"""Real-file capability editing with isolated config, sidecar and agent homes.

This file holds the service-level edits: preview, enrollment, tombstones and accepted
parent changes, resets, project parents, transports and retained secrets, overlays
and reconciliation, the publish lifecycle at the service, and two HTTP checks (a
failed batch retried, the owner boundary). ``test_agent_capabilities_publication.py``,
``..._http.py`` and ``..._stop_following.py`` hold publication over HTTP and the
projection, the remaining capability routes, and forks and Stop following; shared
fixtures live in ``agent_capabilities_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import capability_app_fixture  # noqa: F401
from agent_capabilities_helpers import editor_fixture, save, spec_for

from kiro_crew import agent, agent_state
from kiro_crew.agent_capabilities import (
    CapabilityError,
    CapabilityService,
    prepare_member_capabilities,
    validate_request,
)
from kiro_crew.config import loader

#: ``test_materialization_restamp_probe.py`` imports the ``editor`` fixture by this name.
editor = editor_fixture


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


def test_a_dotted_parent_template_can_be_enrolled(editor):
    service, home, specs, parent = editor
    parent["name"] = "reviewer.v2"
    (specs / "parent.json").unlink()
    (specs / "reviewer.v2.json").write_text(json.dumps(parent), encoding="utf-8")
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "reviewer.v2"
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()

    result = save(service, enroll=True)

    assert result["runtime"]["status"] == "pending"
    assert result["template"]["name"] == "reviewer.v2"
    assert spec_for(home, specs)["tools"] == ["read"]


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


@pytest.mark.parametrize("codepoint", [0x00E9, 0x63D0, 0x1F600, 0xDCFF])
def test_a_non_ascii_preview_token_is_refused_not_raised(editor, codepoint):
    """A non-ASCII token must be refused like any other stale one.

    ``hmac.compare_digest`` rejects a ``str`` holding a non-ASCII character by
    raising ``TypeError``. The token travels in the request body, and the
    handler catches only ``CapabilityError`` and ``(OSError, ValueError)``, so a
    raise escapes as a 500 instead of the ``stale_preview`` refusal that a wrong
    ASCII token produces. Code points are built rather than written literally
    because ``0xDCFF`` is a lone surrogate, which cannot appear in source.
    """
    service, home, specs, parent = editor
    body = {"revision": service.get("A")["revision"], "enroll": True}
    service.preview("A", body)
    with pytest.raises(CapabilityError, match="stale_preview"):
        service.put("A", {**body, "preview_token": chr(codepoint)})


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
                ("post", "/api/agents/A/capabilities/detach"),
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
        current = await (await client.get("/api/agents/A/capabilities")).json()
        response = await client.post(
            "/api/agents/A/capabilities/detach", json={"revision": current["revision"]}
        )
        assert response.status == 200
        assert (await response.json())["mode"] == "legacy_snapshot"


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


def _approval_preview(service, *refs):
    body = {
        "revision": service.get("A")["revision"],
        "enroll": True,
        "operations": [
            {"section": section, "id": ref, "action": "set", "value": True} for section, ref in refs
        ],
        "accept_parent": [],
        "accept_members": [],
    }
    return service.preview("A", body)


def test_preview_names_a_governance_withheld_approval(editor):
    from dataclasses import replace

    from kiro_crew.platform.context import current_context, set_context
    from kiro_crew.platform.governance import parse_policy

    service, _, _, _ = editor
    original = current_context()
    policy = parse_policy(
        {"version": 1, "boot": {"fail_closed": True}, "mcp": {"mode": "deny", "deny": ["@search"]}}
    )
    try:
        set_context(replace(original, governance=policy))
        preview = _approval_preview(
            service, ("autoApprove", "@search/find"), ("allowedTools", "@search/find")
        )
    finally:
        set_context(original)
    assert preview["withheld_approvals"] == [
        {"section": "autoApprove", "id": "@search/find", "reason": "governed"},
        {"section": "allowedTools", "id": "@search/find", "reason": "governed"},
    ]


def test_preview_names_an_approval_for_a_server_the_member_lacks(editor):
    service, _, _, _ = editor
    preview = _approval_preview(
        service,
        ("autoApprove", "@absent/tool"),
        ("allowedTools", "@absent/tool"),
        ("autoApprove", "@search/find"),
        ("allowedTools", "read"),
    )
    assert preview["withheld_approvals"] == [
        {"section": "autoApprove", "id": "@absent/tool", "reason": "not_connected"},
        {"section": "allowedTools", "id": "@absent/tool", "reason": "not_connected"},
    ]


def test_preview_names_an_approval_for_a_disabled_connection(editor):
    service, _, specs, parent = editor
    parent["mcpServers"]["search"]["disabled"] = True
    (specs / "parent.json").write_text(json.dumps(parent))
    preview = _approval_preview(service, ("allowedTools", "@search/find"))
    assert preview["withheld_approvals"] == [
        {"section": "allowedTools", "id": "@search/find", "reason": "not_connected"},
    ]


def test_withheld_reason_is_read_from_the_projected_spec():
    from kiro_crew.agent_capabilities import SECTIONS, _withheld_approvals

    refs = ("@gone/a", "@off/a", "@kept/a", "@kirocrew-mate/a", "@other/a", "@ruled/a")
    request = {
        "operations": [
            {"section": "autoApprove", "id": ref, "action": "set", "value": True} for ref in refs
        ]
    }
    spec = {
        "mcpServers": {
            "off": {"command": "off", "disabled": True},
            "kept": {"command": "kept", "autoApprove": ["a"]},
            "kirocrew-mate": {"command": "mate"},
            "other": {"command": "other"},
            "ruled": {"command": "ruled"},
        }
    }
    intent = {"overrides": {section: {} for section in SECTIONS}}
    intent["overrides"]["autoApprove"]["@ruled/a"] = {"action": "remove"}
    assert _withheld_approvals(request, spec, intent, {}) == [
        {"section": "autoApprove", "id": "@gone/a", "reason": "not_connected"},
        {"section": "autoApprove", "id": "@off/a", "reason": "not_connected"},
        {"section": "autoApprove", "id": "@kirocrew-mate/a", "reason": "managed"},
        {"section": "autoApprove", "id": "@other/a", "reason": "unexplained"},
        {"section": "autoApprove", "id": "@ruled/a", "reason": "governed"},
    ]


def test_preview_refuses_a_server_only_auto_approve_with_a_400(editor):
    service, _, _, _ = editor
    with pytest.raises(CapabilityError) as raised:
        _approval_preview(service, ("autoApprove", "@search"))
    assert (raised.value.code, raised.value.status, raised.value.ref) == (
        "invalid_approval_ref",
        400,
        "@search",
    )


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


def _use_workspace_project(home, monkeypatch, project):
    config = json.loads((home / "config.json").read_text())
    config["workspaces"] = {"default": {"dir": str(project)}}
    (home / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities.default_project_dir", loader.default_project_dir
    )
    loader._invalidate_config_cache()


def test_global_parent_starts_from_any_project_that_does_not_shadow_it(
    editor, tmp_path, monkeypatch
):
    service, home, specs, parent = editor
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _use_workspace_project(home, monkeypatch, workspace)
    save(service, enroll=True)
    saved = prepare_member_capabilities("A", workspace)
    folder = tmp_path / "folder-project"
    folder.mkdir()
    other = prepare_member_capabilities("A", folder)
    assert other["template"] == saved["template"]
    assert other["revision"] == saved["revision"]
    assert prepare_member_capabilities("A", None)["revision"] == saved["revision"]


def test_project_that_shadows_a_global_parent_refuses_the_start(editor, tmp_path, monkeypatch):
    service, home, specs, parent = editor
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _use_workspace_project(home, monkeypatch, workspace)
    save(service, enroll=True)
    folder = tmp_path / "folder-project"
    (folder / ".kiro" / "agents").mkdir(parents=True)
    (folder / ".kiro" / "agents" / "parent.json").write_text(
        json.dumps({**parent, "prompt": "folder parent"})
    )
    with pytest.raises(CapabilityError, match="^project_identity_changed$"):
        prepare_member_capabilities("A", folder)
    assert prepare_member_capabilities("A", workspace)["revision"]


def test_project_parent_refuses_a_start_from_another_project(editor, tmp_path, monkeypatch):
    service, home, specs, parent = editor
    workspace = tmp_path / "workspace"
    (workspace / ".kiro" / "agents").mkdir(parents=True)
    (workspace / ".kiro" / "agents" / "parent.json").write_text(
        json.dumps({**parent, "prompt": "project parent"})
    )
    _use_workspace_project(home, monkeypatch, workspace)
    save(service, enroll=True)
    assert prepare_member_capabilities("A", workspace)["revision"]
    folder = tmp_path / "folder-project"
    folder.mkdir()
    with pytest.raises(CapabilityError, match="^project_identity_changed$"):
        prepare_member_capabilities("A", folder)
    (workspace / ".kiro" / "agents" / "parent.json").unlink()
    with pytest.raises(CapabilityError, match="^parent_identity_changed$"):
        prepare_member_capabilities("A", workspace)


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
