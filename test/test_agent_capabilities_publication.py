"""Capability publication and projection with isolated config, sidecar and agent homes.

Publish receipts retried over HTTP and the binding recheck before staging, corrupt
nested data failing closed, transport metadata through enrollment and
reconciliation, and credential retention across the HTTP projection.
Fixtures and helpers come from ``agent_capabilities_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import editor_fixture  # noqa: F401
from agent_capabilities_helpers import save, spec_for

from kiro_crew import agent, agent_state
from kiro_crew.agent_capabilities import (
    CapabilityError,
    CapabilityService,
    prepare_member_capabilities,
)
from kiro_crew.config import loader


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
