"""A third-party app's manifest ``agentTools``: listed by ``kirocrew-apps``, each call asked.

``kirocrew-core`` is auto-approved as a whole server, so it lists only first-party
apps' tools (``test_app_agent_tools.py``). A third-party app's are listed by the
``kirocrew-apps`` server instead, which no ``allowedTools`` grants. These tests use
synthetic apps written into the isolated test home, so they name no real app:

- the reader: which third-party blocks list, and which list nothing (a stated foreign
  ``namespace``, a ``strictSession`` tool, a built-in's name, a first-party name);
- the server: listing, the enablement gate, validation, and the forwarded call, posted
  with no session key;
- the gateway: each route resolves only its own set of declarations;
- the grant: the server is emitted into the default spec and never auto-approved.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import agent, mcp_apps
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps import manager as apps_manager
from kiro_crew.dashboard.handlers import app_agent_tools as handler_mod
from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
from kiro_crew.validation_parts.field_engine import ValidationError

_APP = "fake-app"


def _tool(**overrides: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": "fake_app_note",
        "description": "Post one note to an item.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string", "maxLength": 64}},
            "required": ["item_id"],
        },
        "maxResultChars": 50,
    }
    tool.update(overrides)
    return tool


def _block(*tools: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"route": "agent-tools", "tools": list(tools) or [_tool()], **extra}


def _install(name: str = _APP, block: dict[str, Any] | None = None, *, enabled: bool = True):
    root = apps_manager.app_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "synthetic",
        "agentTools": _block() if block is None else block,
    }
    (root / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / apps_manager.INSTALLED_META_FILENAME).write_text(
        json.dumps({"name": name, "enabled": enabled}), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def provenance(_floor_monkeypatch) -> dict[str, Any]:
    """Every synthetic app is third party unless a test names it first party or shipped.

    ``first_party`` stands in for ``execution.builtin_app_names()`` and reads its
    declaration from the data home; ``shipped`` stands in for the names a shipped
    manifest declares. The caches are cleared so each test sees its own apps.
    """
    state: dict[str, Any] = {"first_party": set(), "shipped": set()}

    def _first_party_manifest(name: str) -> Any:
        return apps_manager.get_app_manifest(name)

    def _shipped_tools() -> tuple[app_agent_tools.DeclaredTool, ...]:
        return app_agent_tools._collect(sorted(state["shipped"]), drop_clashes=False)

    _floor_monkeypatch.setattr(
        app_agent_tools, "_first_party_apps", lambda: frozenset(state["first_party"])
    )
    _floor_monkeypatch.setattr(app_agent_tools, "_first_party_manifest", _first_party_manifest)
    _floor_monkeypatch.setattr(app_agent_tools, "shipped_tools", _shipped_tools)
    _floor_monkeypatch.setattr(
        app_agent_tools,
        "_shipped_first_party_roots",
        lambda: {name: apps_manager.app_dir(name) for name in state["shipped"]},
    )
    _floor_monkeypatch.setattr(app_agent_tools, "_cache", None)
    _floor_monkeypatch.setattr(app_agent_tools, "_third_party_cache", None)
    return state


class _FakePost:
    def __init__(self, resp: dict[str, Any]) -> None:
        self.resp = resp
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def __call__(self, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((path, copy.deepcopy(body), kw))
        return dict(self.resp)


# ── The reader ───────────────────────────────────────────────────────────────


def test_a_third_party_declaration_is_read_and_follows_an_edit():
    _install()
    (tool,) = app_agent_tools.third_party_tools()
    assert (tool.app, tool.route, tool.name) == (_APP, "agent-tools", "fake_app_note")
    assert app_agent_tools.declared_tools() == ()
    _install(block=_block(_tool(), _tool(name="fake_app_other")))
    assert [t.name for t in app_agent_tools.third_party_tools()] == [
        "fake_app_note",
        "fake_app_other",
    ]


def test_a_first_party_app_is_never_read_as_third_party(provenance):
    provenance["first_party"].add(_APP)
    _install()
    assert app_agent_tools.third_party_tools() == ()
    assert [t.name for t in app_agent_tools.declared_tools()] == ["fake_app_note"]


def test_a_record_under_a_shipped_name_lists_nothing(provenance):
    """A third-party record in a built-in's slot is not the built-in, and not listed here."""
    provenance["shipped"].add(_APP)
    _install()
    assert app_agent_tools.third_party_tools() == ()


def test_a_stated_foreign_namespace_lists_nothing():
    _install(block=_block(_tool(name="pod_up"), namespace="pod"))
    assert app_agent_tools.third_party_tools() == ()


def test_a_stated_namespace_equal_to_the_apps_own_is_read():
    _install(block=_block(namespace="fake_app"))
    assert [t.name for t in app_agent_tools.third_party_tools()] == ["fake_app_note"]


def test_a_block_with_a_strict_session_tool_lists_nothing():
    """No session's key is handed to a third-party app, so none of its block lists."""
    _install(block=_block(_tool(), _tool(name="fake_app_mine", strictSession=True)))
    assert app_agent_tools.third_party_tools() == ()


def test_a_first_party_name_stays_the_first_party_apps(provenance):
    """``fake`` may name ``fake_app_note`` too: its namespace is ``fake_``."""
    provenance["first_party"].add("fake")
    _install("fake")
    _install(_APP)
    assert app_agent_tools.third_party_tools() == ()
    assert [(t.app, t.name) for t in app_agent_tools.declared_tools()] == [
        ("fake", "fake_app_note")
    ]


def test_a_shipped_name_stays_the_first_party_apps_while_it_is_not_installed(provenance):
    provenance["shipped"].add("fake")
    _install("fake")
    _install(_APP)
    assert app_agent_tools.third_party_tools() == ()


def test_a_name_two_third_party_apps_declare_belongs_to_neither():
    _install("fake")
    _install(_APP)
    assert app_agent_tools.third_party_tools() == ()


# ── The server ───────────────────────────────────────────────────────────────


def test_the_server_lists_each_tool_led_by_the_app_that_serves_it():
    _install()
    (listed,) = mcp_apps._list_tools()
    assert listed["name"] == "fake_app_note"
    assert listed["description"] == (
        "Served by the installed app 'fake-app', not by Kiro Crew.\n\n" "Post one note to an item."
    )
    assert listed["inputSchema"] == _tool()["inputSchema"]


def test_the_server_hides_a_disabled_app_and_lists_nothing_with_none_installed():
    assert mcp_apps._list_tools() == []
    _install(enabled=False)
    assert mcp_apps._list_tools() == []


def test_a_call_is_validated_against_the_declaration():
    _install()
    assert mcp_apps._validate_args("fake_app_note", {"item_id": "i-1"}) == {"item_id": "i-1"}
    with pytest.raises(ValidationError):
        mcp_apps._validate_args("fake_app_note", {"item_id": "i-1", "extra": 1})
    with pytest.raises(ValidationError):
        mcp_apps._validate_args("fake_app_note", {"item_id": "x" * 65})
    with pytest.raises(ValidationError):
        mcp_apps._validate_args("fake_app_other", {})


def test_a_call_is_posted_to_the_prompted_route_with_the_lenient_identity(monkeypatch):
    """No ``session_key`` override: the post carries the key the gateway reads the
    calling app from, which the cross-app refusal needs."""
    _install(block=_block(_tool(timeoutSeconds=45)))
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_apps, "_post", post)
    assert mcp_apps._call_tool_inner("fake_app_note", {"item_id": "i-1"}) == '{"ok": true}'
    assert post.calls == [
        (
            "/api/app-tools/prompted-call",
            {"tool": "fake_app_note", "arguments": {"item_id": "i-1"}},
            {"timeout": 45},
        )
    ]


def test_the_answer_is_redacted_then_capped(monkeypatch):
    _install()
    monkeypatch.setattr(mcp_apps, "_post", _FakePost({"t": "z" * 80}))
    out = mcp_apps._call_tool_inner("fake_app_note", {"item_id": "i-1"})
    assert out.startswith('{"t": "' + "z" * 43)
    assert "truncated" in out


def test_a_third_party_text_answer_never_hands_over_a_credential_verbatim(monkeypatch):
    """The ``{"text": [...], "credential": ...}`` shape keeps one string unredacted for a
    first-party app; a third-party app's answer in that shape is redacted whole."""
    from kiro_crew.mcp_tools import app_tools

    _install(block=_block(_tool(result="text", maxResultChars=60_000)))
    token = "vendor-token-" + "x" * 24
    monkeypatch.setattr(mcp_apps, "_post", _FakePost({"text": ["key: ", ""], "credential": token}))
    monkeypatch.setattr(app_tools, "redact", lambda text: text.replace(token, "[REDACTED]"))
    out = mcp_apps._call_tool_inner("fake_app_note", {"item_id": "i-1"})
    assert token not in out
    assert out.startswith("Error: fake_app_note returned no result")


def test_an_undeclared_tool_posts_nothing(monkeypatch):
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_apps, "_post", post)
    assert mcp_apps._call_tool_inner("fake_app_note", {}).startswith("Error:")
    assert post.calls == []


# ── The gateway ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    class _Sel:
        def log_api_access(self, **_kw: Any) -> None:
            pass

    _floor_monkeypatch.setattr(handler_mod, "sel", lambda: _Sel())


class _Registry:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, Any]] = []

    def serves(self, app_name: str, method: str, path: str) -> bool:
        return True

    async def dispatch(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.seen.append((request.match_info["app_name"], request.match_info["path"], body))
        return web.json_response({"served": True})


@pytest.fixture
def registry(monkeypatch) -> _Registry:
    from kiro_crew.apps import hooks_integration

    reg = _Registry()
    monkeypatch.setattr(hooks_integration, "get_route_registry", lambda: reg)
    return reg


def _request(path: str, body: Any, *, internal: bool = True, calling_app: str = "") -> web.Request:
    req = make_mocked_request("POST", path)
    if internal:
        req["internal_auth"] = True
    if calling_app:
        # What token_auth publishes for a session an app owns.
        req["app"] = calling_app
    req._read_bytes = json.dumps(body).encode()
    return req


_CALL = {"tool": "fake_app_note", "arguments": {"item_id": "i-1"}}


def test_the_prompted_route_is_strict_internal():
    assert mcp_apps.CALL_PATH in _STRICT_INTERNAL_API_PATHS


@pytest.mark.asyncio
async def test_the_prompted_route_serves_a_third_party_tool(registry):
    _install()
    resp = await handler_mod.api_prompted_app_tool_call(_request(mcp_apps.CALL_PATH, _CALL))
    assert (resp.status, json.loads(resp.body)) == (200, {"served": True})
    assert registry.seen == [(_APP, "agent-tools", _CALL)]


@pytest.mark.asyncio
async def test_the_core_route_never_reaches_a_third_party_tool(registry):
    _install()
    resp = await handler_mod.api_app_agent_tool_call(_request("/api/app-tools/call", _CALL))
    assert (resp.status, json.loads(resp.body)["code"]) == (404, "unknown_tool")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_the_prompted_route_never_reaches_a_first_party_tool(provenance, registry):
    provenance["first_party"].add(_APP)
    _install()
    resp = await handler_mod.api_prompted_app_tool_call(_request(mcp_apps.CALL_PATH, _CALL))
    assert (resp.status, json.loads(resp.body)["code"]) == (404, "unknown_tool")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_the_prompted_route_refuses_without_the_internal_secret(registry):
    _install()
    resp = await handler_mod.api_prompted_app_tool_call(
        _request(mcp_apps.CALL_PATH, _CALL, internal=False)
    )
    assert (resp.status, json.loads(resp.body)["code"]) == (403, "loopback_only")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_the_prompted_route_refuses_a_disabled_app(registry):
    _install(enabled=False)
    resp = await handler_mod.api_prompted_app_tool_call(_request(mcp_apps.CALL_PATH, _CALL))
    assert (resp.status, json.loads(resp.body)["code"]) == (403, "app_not_enabled")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_the_prompted_route_refuses_arguments_beyond_the_declaration(registry):
    _install()
    body = {"tool": "fake_app_note", "arguments": {"item_id": "i-1", "extra": 1}}
    resp = await handler_mod.api_prompted_app_tool_call(_request(mcp_apps.CALL_PATH, body))
    assert (resp.status, json.loads(resp.body)["code"]) == (400, "invalid_arguments")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_a_session_another_app_owns_is_refused(registry):
    """The registry does not check the calling app, so the route does."""
    _install()
    resp = await handler_mod.api_prompted_app_tool_call(
        _request(mcp_apps.CALL_PATH, _CALL, calling_app="other-app")
    )
    assert (resp.status, json.loads(resp.body)["code"]) == (403, "cross_app")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_a_session_the_declaring_app_owns_is_served(registry):
    _install()
    resp = await handler_mod.api_prompted_app_tool_call(
        _request(mcp_apps.CALL_PATH, _CALL, calling_app=_APP)
    )
    assert resp.status == 200
    assert registry.seen == [(_APP, "agent-tools", _CALL)]


# ── A scoped app agent ───────────────────────────────────────────────────────


def test_a_scoped_app_agent_has_the_whole_server_withheld():
    """Its tool names are those of the apps installed at call time, so no exclusion
    built when the agent is materialized can name an app installed or enabled later."""
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.apps.permissions import managed_tool_exclusion

    scoped = AppManifest.from_dict(
        {
            "name": "scoped-app",
            "version": "1.0.0",
            "permissions": {"mcpTools": ["spawn_run"]},
        }
    )
    assert "kirocrew-apps" in managed_tool_exclusion(scoped).servers
    unscoped = AppManifest.from_dict({"name": "open-app", "version": "1.0.0"})
    assert managed_tool_exclusion(unscoped).servers == []


# ── The grant ────────────────────────────────────────────────────────────────


def test_the_server_is_opt_in_and_never_auto_approved():
    """No default agent reaches third-party app code: the server mounts only on a spec
    granted ``@kirocrew-apps``, and nothing pre-approves its calls."""
    from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS, OPT_IN_BIN_MCP_SERVERS

    spec = agent._MANAGED_MCP_SERVERS["kirocrew-apps"]
    assert spec.get("opt_in") is True
    assert "autoApprove" not in spec
    assert "kirocrew-apps" in OPT_IN_BIN_MCP_SERVERS
    assert "kirocrew-apps" not in ALWAYS_ON_BIN_MCP_SERVERS
    defaults = agent.get_shipped_tools()
    assert not [t for t in defaults["tools"] if t.startswith("@kirocrew-apps")]
    assert not [t for t in defaults["allowedTools"] if t.startswith("@kirocrew-apps")]
