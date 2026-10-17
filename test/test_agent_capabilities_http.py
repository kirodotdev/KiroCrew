"""Capability editing over HTTP with isolated config, sidecar and agent homes.

The agent capability routes behind ``capability_app``: invalid bodies, inherited
actions, whole reset, ambient-MCP removals, credential round trips, inherit and
accept, basic-auth and password-only projections, and the guards around a legacy
private copy.
Fixtures and helpers come from ``agent_capabilities_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import capability_app_fixture  # noqa: F401
from agent_capabilities_helpers import editor_fixture  # noqa: F401
from agent_capabilities_helpers import save, spec_for

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import CapabilityError, prepare_member_capabilities
from kiro_crew.config import loader


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
async def test_http_invalid_approval_ref_names_the_refused_entry(editor, capability_app):
    from aiohttp.test_utils import TestClient, TestServer

    service, _, _, _ = editor
    app, _ = capability_app
    body = {
        "revision": (await asyncio.to_thread(service.get, "A"))["revision"],
        "enroll": True,
        "operations": [{"section": "autoApprove", "id": "@search", "action": "set", "value": True}],
        "accept_parent": [],
        "accept_members": [],
    }
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/agents/A/capabilities/preview", json=body)
        assert response.status == 400
        assert await response.json() == {
            "error": "invalid_approval_ref",
            "code": "invalid_approval_ref",
            "ref": "@search",
        }


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
