"""The app agent tool seam: an installed app's manifest ``agentTools``, end to end.

An app declares MCP tools in its manifest and serves them from its own process; the core
lists them in ``kirocrew-core``, validates each call against the declared schema and
forwards it to the one gateway route that hands it to the app. These tests use
synthetic apps written into the isolated test home, so they name no real app:

- the manifest block: what parses, and every declaration outside the rules is refused;
- the reader: first-party apps only (a third-party app's block lists nothing, since
  ``kirocrew-core`` is auto-approved as a whole server), a broken block and a contested
  name drop out, and the cache follows a manifest edit;
- the ``app_tools`` MCP domain: listing, the enablement gate, the core-name precedence,
  validation through the field engine, and the forwarded call;
- the gateway handler: the call reaches only the declaring app, only through its
  declared route, and only with arguments the declaration allows; through the real
  signed proxy, the app's backend receives the exact body that was checked;
- ``strictSession``: such a tool is posted only from a strictly identified session,
  with that session's key, and the gateway refuses one that arrives without a key;
  an answer of exactly ``{"text": ...}`` is the tool's text.

What one app's tool does is pinned in that app's own ``tests/``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps import manager as apps_manager
from kiro_crew.apps.discovery import _manifest_to_builtin_dict
from kiro_crew.apps.manifest import AgentToolsConfig, AppManifest
from kiro_crew.dashboard.handlers import app_agent_tools as handler_mod
from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
from kiro_crew.mcp_tools import app_tools, build_tool_list, build_tool_names, dispatch

_APP = "fake-app"


def _tool(**overrides: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": "fake_app_note",
        "title": "Post a note",
        "description": "Post one note to an item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "maxLength": 64},
                "note": {"type": "string", "maxLength": 128},
                "mood": {"type": "string", "enum": ["calm", "busy"]},
                "count": {"type": "integer", "minimum": 0, "maximum": 9},
                "labels": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "slots": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["item_id"],
        },
        "maxResultChars": 50,
    }
    tool.update(overrides)
    return tool


def _block(*tools: dict[str, Any], route: str = "agent-tools") -> dict[str, Any]:
    return {"route": route, "tools": list(tools) or [_tool()]}


@pytest.fixture(autouse=True)
def first_party(_floor_monkeypatch) -> dict[str, bool]:
    """The synthetic apps count as first party unless a test sets ``state["all"]`` False.

    First-party provenance needs a shipped manifest, which a synthetic app cannot have,
    so the provenance reads are stood in for: every installed app is trusted and ships,
    and its declaration is read from the data home. With the flag off, the real reads
    answer. The deny-universe read is never cached here, so each test sees its own apps.
    """
    state = {"all": True}
    real_apps = app_agent_tools._first_party_apps
    real_manifest = app_agent_tools._first_party_manifest
    real_shipped = app_agent_tools.shipped_tools.__wrapped__

    def _installed() -> frozenset[str]:
        root = apps_manager.apps_dir()
        return frozenset(p.name for p in root.iterdir()) if root.is_dir() else frozenset()

    def _apps() -> frozenset[str]:
        return _installed() if state["all"] else real_apps()

    def _manifest(name: str) -> Any:
        return apps_manager.get_app_manifest(name) if state["all"] else real_manifest(name)

    def _shipped() -> tuple[app_agent_tools.DeclaredTool, ...]:
        if not state["all"]:
            return real_shipped()
        return app_agent_tools._collect(sorted(_installed()), drop_clashes=False)

    _floor_monkeypatch.setattr(app_agent_tools, "_first_party_apps", _apps)
    _floor_monkeypatch.setattr(app_agent_tools, "_first_party_manifest", _manifest)
    _floor_monkeypatch.setattr(app_agent_tools, "shipped_tools", _shipped)
    return state


def _install(
    name: str = _APP,
    block: dict[str, Any] | None = None,
    *,
    enabled: bool = True,
    backend: dict[str, Any] | None = None,
    manifest_name: str | None = None,
) -> None:
    """A real installed app in the isolated test home."""
    root = apps_manager.app_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": manifest_name or name,
        "version": "1.0.0",
        "displayName": name,
        "description": "synthetic",
        "agentTools": _block() if block is None else block,
    }
    if backend:
        manifest["backend"] = backend
    (root / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / apps_manager.INSTALLED_META_FILENAME).write_text(
        json.dumps({"name": name, "enabled": enabled}), encoding="utf-8"
    )


# ── The manifest block ───────────────────────────────────────────────────────


def test_a_valid_block_parses_and_describes_the_tool():
    block = AgentToolsConfig.from_value(_block())
    assert block.validate(_APP) == []
    assert block.route == "agent-tools"
    (tool,) = block.tools
    assert tool.descriptor() == {
        "name": "fake_app_note",
        "title": "Post a note",
        "description": "Post one note to an item.",
        "inputSchema": _tool()["inputSchema"],
    }
    assert tool.max_result_chars == 50


def _schema_with(arg: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {"x": arg}}


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (["not", "an", "object"], "must be an object"),
        ({"route": "agent-tools"}, "non-empty list"),
        (_block(_tool(name="other_app_note")), "the app's own namespace"),
        (_block(_tool(name="pod_up")), "the app's own namespace"),
        (_block(route="../core"), "plain path"),
        (_block(route="/agent-tools"), "plain path"),
        (_block(route="//evil.example/x"), "plain path"),
        (_block(route="tools?x=1"), "plain path"),
        (_block(route="Tools"), "plain path"),
        (_block(_tool(), _tool()), "declared twice"),
        (_block(*[_tool(name=f"fake_app_t{i}") for i in range(17)]), "at most 16"),
        (_block(_tool(maxResultChars=0)), "maxResultChars"),
        ({**_block(), "namespace": "Pod"}, "namespace"),
        ({**_block(), "namespace": "pod-x"}, "namespace"),
        ({**_block(), "namespace": "a" * 33}, "namespace"),
        ({**_block(), "namespace": 7}, "namespace"),
        (_block(_tool(timeoutSeconds=0)), "timeoutSeconds"),
        (_block(_tool(timeoutSeconds=601)), "timeoutSeconds"),
        (_block(_tool(timeoutSeconds=True)), "timeoutSeconds"),
        (_block(_tool(result="html")), "result"),
        (
            _block(
                _tool(
                    inputSchema=_schema_with(
                        {"type": "array", "items": {"type": "string"}, "maxItems": 257}
                    )
                )
            ),
            "maxItems",
        ),
        (_block(_tool(maxResultChars=60_001)), "maxResultChars"),
        (_block(_tool(strictSession="yes")), "strictSession"),
        (_block(_tool(handler="x.y:z")), "unknown keys"),
        (_block(_tool(description="")), "description"),
        (_block(_tool(title="two\nlines")), "title"),
        (_block(_tool(inputSchema={"type": "string"})), "object schema"),
        (
            _block(_tool(inputSchema={"type": "object", "additionalProperties": True})),
            "additional properties",
        ),
        (_block(_tool(inputSchema=_schema_with({"type": "object"}))), "type must be"),
        (
            _block(_tool(inputSchema=_schema_with({"type": "string", "pattern": ".*"}))),
            "unsupported keys",
        ),
        (
            _block(_tool(inputSchema=_schema_with({"type": "string", "maxLength": 10**6}))),
            "maxLength",
        ),
        (
            _block(_tool(inputSchema=_schema_with({"type": "array", "items": {"type": "object"}}))),
            "items must be",
        ),
        (
            _block(
                _tool(inputSchema={"type": "object", "properties": {}, "required": ["missing"]})
            ),
            "required",
        ),
    ],
)
def test_a_declaration_outside_the_rules_is_refused(block, message):
    errors = AgentToolsConfig.from_value(block).validate(_APP)
    assert any(message in e for e in errors), errors


def test_the_block_is_validated_signed_and_carried_through_every_round_trip():
    raw = {
        "name": _APP,
        "version": "1.0.0",
        "displayName": "Fake",
        "description": "d",
        "agentTools": _block(),
    }
    manifest = AppManifest.from_dict(raw)
    assert manifest.validate() == []
    assert manifest.to_dict()["agentTools"] == raw["agentTools"]
    assert _manifest_to_builtin_dict(manifest)["agentTools"] == raw["agentTools"]
    assert json.loads(manifest.signing_payload())["agentTools"] == raw["agentTools"]
    bad = AppManifest.from_dict({**raw, "agentTools": _block(_tool(name="x_note"))})
    assert any("namespace" in e for e in bad.validate())
    plain = AppManifest.from_dict({k: v for k, v in raw.items() if k != "agentTools"})
    assert "agentTools" not in json.loads(plain.signing_payload())


# ── The reader ───────────────────────────────────────────────────────────────


def test_no_installed_app_declares_nothing():
    assert app_agent_tools.declared_tools() == ()
    assert app_tools.schemas() == []
    assert dict(app_tools.HANDLERS) == {}


def test_an_installed_declaration_is_read_and_follows_an_edit():
    _install()
    (tool,) = app_agent_tools.declared_tools()
    assert (tool.app, tool.route, tool.name) == (_APP, "agent-tools", "fake_app_note")
    _install(block=_block(_tool(), _tool(name="fake_app_other")))
    assert [t.name for t in app_agent_tools.declared_tools()] == [
        "fake_app_note",
        "fake_app_other",
    ]


def test_a_third_party_app_lists_nothing(first_party):
    """``kirocrew-core`` is auto-approved as a whole server: a third-party tool never joins it."""
    first_party["all"] = False
    _install()
    assert (
        AppManifest.from_dict(
            json.loads((apps_manager.app_dir(_APP) / "app.json").read_text(encoding="utf-8"))
        ).agentTools.validate(_APP)
        == []
    )
    assert app_agent_tools.declared_tools() == ()
    assert "fake_app_note" not in {t["name"] for t in app_tools.schemas()}
    assert app_agent_tools.declared_tool("fake_app_note") is None


def test_a_block_with_any_error_contributes_none_of_its_tools():
    _install(block=_block(_tool(), _tool(name="another_app_x")))
    assert app_agent_tools.declared_tools() == ()


def test_a_manifest_that_is_not_its_directorys_app_is_skipped():
    _install("fake-app", manifest_name="other-app")
    assert app_agent_tools.declared_tools() == ()


def test_a_name_two_apps_declare_belongs_to_neither():
    # ``fake`` may name ``fake_app_note`` too: its namespace is ``fake_``.
    _install("fake-app")
    _install("fake")
    assert app_agent_tools.declared_tools() == ()


# ── The MCP domain ───────────────────────────────────────────────────────────


def test_the_listing_follows_enablement_and_the_names_build_does_not():
    _install(enabled=False)
    assert "fake_app_note" not in {t["name"] for t in build_tool_list()}
    assert "fake_app_note" in build_tool_names()
    assert [t["name"] for t in app_tools.schemas()] == ["fake_app_note"]
    _install(enabled=True)
    listed = {t["name"]: t for t in build_tool_list()}
    assert listed["fake_app_note"]["title"] == "Post a note"
    assert set(app_tools.HANDLERS) == {"fake_app_note"}


def test_a_tool_is_in_the_deny_universe_before_its_app_is_installed(monkeypatch):
    """A scoped app agent's exclude list is fixed when it is materialized.

    So the names build must already name a shipped first-party tool whose app holds no
    first-party slot yet; otherwise installing that app later would hand the tool to
    every agent whose ``permissions.mcpTools`` left it out.
    """
    from kiro_crew.mcp_discovery_managed import managed_tool_names_for_policy

    _install()
    monkeypatch.setattr(app_agent_tools, "_first_party_apps", frozenset)
    assert app_agent_tools.declared_tools() == ()
    assert "fake_app_note" in build_tool_names()
    assert "fake_app_note" in (managed_tool_names_for_policy("kirocrew-core") or [])
    assert "fake_app_note" not in {t["name"] for t in build_tool_list()}
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    assert mcp_core._call_tool("fake_app_note", {"item_id": "i"}).startswith("Error")
    assert post.calls == []


def test_a_core_tool_name_is_never_an_apps(monkeypatch):
    """An app named ``spawn`` may declare ``spawn_run``; the core's tool keeps the name."""
    _install("spawn", block=_block(_tool(name="spawn_run")))
    assert app_agent_tools.declared_tool("spawn_run") is not None
    assert app_tools.schemas() == []
    assert app_tools.declared("spawn_run") is None
    assert app_tools.tool_schema("spawn_run") is None
    assert "spawn_run" not in app_tools.HANDLERS
    assert [t["name"] for t in build_tool_list(names_only=True)].count("spawn_run") == 1


def test_a_stated_namespace_replaces_the_app_name_and_a_core_name_still_wins():
    """``namespace`` lets an app keep tool names it had before the seam, never a core one."""
    block = AgentToolsConfig.from_value({**_block(_tool(name="pod_note")), "namespace": "pod"})
    assert block.validate(_APP) == []
    assert block.namespace == "pod"
    wrong = AgentToolsConfig.from_value({**_block(_tool()), "namespace": "pod"})
    assert any("'pod_'" in e for e in wrong.validate(_APP))
    _install(block={**_block(_tool(name="spawn_run")), "namespace": "spawn"})
    assert app_agent_tools.declared_tool("spawn_run") is not None
    assert app_tools.declared("spawn_run") is None


class _FakePost:
    def __init__(self, resp: dict[str, Any]) -> None:
        self.resp = resp
        self.calls: list[tuple[str, Any]] = []
        self.timeouts: list[Any] = []

    def __call__(self, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((path, copy.deepcopy(body)))
        self.timeouts.append(kw.get("timeout"))
        return dict(self.resp)


def test_a_valid_call_is_forwarded_as_tool_and_arguments(monkeypatch):
    _install()
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    out = mcp_core._call_tool("fake_app_note", {"item_id": "i-1", "labels": ["a"], "count": 2})
    assert out == '{"ok": true}'
    assert post.calls == [
        (
            app_tools._CALL_PATH,
            {"tool": "fake_app_note", "arguments": {"item_id": "i-1", "labels": ["a"], "count": 2}},
        )
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"item_id": "i", "route": "/other"},  # an argument the schema does not declare
        {"note": "no id"},  # a required argument missing
        {"item_id": "i", "mood": "angry"},  # outside the enum
        {"item_id": "i" * 65},  # past maxLength
        {"item_id": "i", "count": 10},  # past maximum
        {"item_id": "i", "count": True},  # a bool is not an integer
        {"item_id": "i", "labels": ["a", "b", "c", "d"]},  # past maxItems
        {"item_id": "i", "labels": [1]},  # wrong item type
        {"item_id": "i", "slots": [1, True]},  # a bool is not an integer item
        {"item_id": "i", "slots": list(range(257))},  # past the default item cap
    ],
)
def test_a_call_beyond_the_declaration_never_leaves_the_server(monkeypatch, args):
    _install()
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    out = mcp_core._call_tool("fake_app_note", args)
    assert out.startswith("Error"), out
    assert post.calls == []


def test_an_undeclared_tool_is_unknown(monkeypatch):
    _install()
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    assert dispatch("fake_app_delete_everything", {}).startswith("Unknown tool")
    assert post.calls == []


def test_the_answer_is_redacted_then_capped(monkeypatch):
    _install()
    monkeypatch.setattr(mcp_core, "_post", _FakePost({"t": "z" * 80}))
    monkeypatch.setattr(app_tools, "redact", lambda text: text.replace("z", "y"))
    out = app_tools.app_agent_tool("fake_app_note", {"item_id": "i"})
    full = '{"t": "' + "y" * 80 + '"}'
    assert out == full[:50] + f"\n… truncated ({len(full)} chars total)."


@pytest.mark.parametrize(("declared", "expected"), [({}, 30), ({"timeoutSeconds": 210}, 210)])
def test_the_core_waits_the_declared_time(monkeypatch, declared, expected):
    _install(block=_block(_tool(**declared)))
    post = _FakePost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    app_tools.app_agent_tool("fake_app_note", {"item_id": "i"})
    assert post.timeouts == [expected]


def _redact_marked(text: str) -> str:
    return f"<{text}>"


def test_a_text_tool_answer_is_redacted_text(monkeypatch):
    _install(block=_block(_tool(result="text", maxResultChars=60_000)))
    monkeypatch.setattr(app_tools, "redact", _redact_marked)
    monkeypatch.setattr(mcp_core, "_post", _FakePost({"text": "token: tok-1\nopen: x"}))
    assert app_tools.app_agent_tool("fake_app_note", {"item_id": "i"}) == "<token: tok-1\nopen: x>"


def test_a_text_tool_keeps_only_the_credential_it_names(monkeypatch):
    """Each piece around the credential is redacted; the credential is put back verbatim."""
    cred = "c" * 16
    _install(block=_block(_tool(result="text", maxResultChars=60_000)))
    monkeypatch.setattr(app_tools, "redact", _redact_marked)
    monkeypatch.setattr(
        mcp_core,
        "_post",
        _FakePost({"text": ["token: ", "\nopen: ?token=", "\nend"], "credential": cred}),
    )
    assert app_tools.app_agent_tool("fake_app_note", {"item_id": "i"}) == (
        f"<token: >{cred}<\nopen: ?token=>{cred}<\nend>"
    )


@pytest.mark.parametrize(
    ("resp", "expected"),
    [
        (
            {"error": "gateway not reachable", "transport_error": True},
            "Error: fake_app_note failed: <gateway not reachable>",
        ),
        (
            {"code": "app_not_enabled", "error": "app 'fake-app' is not enabled"},
            "Error: fake_app_note failed [app_not_enabled]: <app 'fake-app' is not enabled>",
        ),
        (
            {"code": "Not A Code!", "error": "refused"},
            "Error: fake_app_note failed: <refused>",
        ),
        (
            {"text": "x", "extra": 1},
            'Error: fake_app_note returned no result: <{"text": "x", "extra": 1}>',
        ),
        ({"text": 7}, 'Error: fake_app_note returned no result: <{"text": 7}>'),
        (
            {"text": ["a", "b"], "credential": "short"},
            'Error: fake_app_note returned no result: <{"text": ["a", "b"], "credential": "short"}>',
        ),
        (
            {"text": "a", "credential": "c" * 16},
            "Error: fake_app_note returned no result: <"
            + json.dumps({"text": "a", "credential": "c" * 16})
            + ">",
        ),
        (
            {"text": ["a"] * 9, "credential": "c" * 16},
            "Error: fake_app_note returned no result: <"
            + json.dumps({"text": ["a"] * 9, "credential": "c" * 16})
            + ">",
        ),
        (
            {"text": ["a", "b"], "credential": "has a space in it"},
            "Error: fake_app_note returned no result: <"
            + json.dumps({"text": ["a", "b"], "credential": "has a space in it"})
            + ">",
        ),
    ],
)
def test_any_other_answer_of_a_text_tool_is_an_error_line(monkeypatch, resp, expected):
    """A timeout or a refusal must read ``Error:``: the tool-call audit keys its outcome on it."""
    _install(block=_block(_tool(result="text", maxResultChars=60_000)))
    monkeypatch.setattr(app_tools, "redact", _redact_marked)
    monkeypatch.setattr(mcp_core, "_post", _FakePost(resp))
    assert app_tools.app_agent_tool("fake_app_note", {"item_id": "i"}) == expected


def test_a_json_tool_never_relays_text_unredacted(monkeypatch):
    _install(block=_block(_tool(maxResultChars=60_000)))
    monkeypatch.setattr(app_tools, "redact", lambda text: "REDACTED")
    monkeypatch.setattr(mcp_core, "_post", _FakePost({"text": "tok-1"}))
    assert app_tools.app_agent_tool("fake_app_note", {"item_id": "i"}) == "REDACTED"


# ── The gateway handler ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    class _Sel:
        def log_api_access(self, **_kw: Any) -> None:
            pass

    _floor_monkeypatch.setattr(handler_mod, "sel", lambda: _Sel())


def _request(body: Any, *, internal: bool = True) -> web.Request:
    req = make_mocked_request("POST", app_tools._CALL_PATH)
    if internal:
        req["internal_auth"] = True
    req._read_bytes = json.dumps(body).encode()
    return req


class _Registry:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, Any]] = []
        #: ``(app, method, path)`` the fake reports as an app's own registered route.
        self.routes: set[tuple[str, str, str]] = set()

    def serves(self, app_name: str, method: str, path: str) -> bool:
        return (app_name, method, path.lstrip("/")) in self.routes

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


def test_the_forwarding_route_is_strict_internal():
    assert app_tools._CALL_PATH in _STRICT_INTERNAL_API_PATHS


async def _status(body: Any, **kw: Any) -> tuple[int, dict[str, Any]]:
    resp = await handler_mod.api_app_agent_tool_call(_request(body, **kw))
    return resp.status, json.loads(resp.body)


_CALL = {"tool": "fake_app_note", "arguments": {"item_id": "i-1"}}


@pytest.mark.asyncio
async def test_an_in_gateway_app_is_served_by_its_declared_route_only(registry):
    _install()
    status, body = await _status(_CALL)
    assert (status, body) == (200, {"served": True})
    assert registry.seen == [(_APP, "agent-tools", _CALL)]


@pytest.mark.asyncio
async def test_a_backend_app_is_served_through_the_signed_proxy(monkeypatch):
    from kiro_crew.apps import routes as app_routes

    _install(backend={"entryPoint": "server.py", "port": "auto"})
    seen: list[tuple[str, str]] = []

    async def _proxy(request: web.Request, *, read_body: bytes | None = None) -> web.Response:
        seen.append((request.match_info["name"], request.match_info["path"], read_body))
        return web.json_response({"proxied": True})

    monkeypatch.setattr(app_routes, "handle_app_api_proxy", _proxy)
    status, body = await _status(_CALL)
    assert (status, body) == (200, {"proxied": True})
    assert seen == [(_APP, "agent-tools", json.dumps(_CALL).encode())]


@pytest.mark.asyncio
async def test_a_backend_app_with_its_own_gateway_route_is_served_there(monkeypatch, registry):
    """A tool that needs the gateway's host namespaces is served by the app's gateway route."""
    from kiro_crew.apps import routes as app_routes

    _install(backend={"entryPoint": "server.py", "port": "auto"})
    registry.routes.add((_APP, "POST", "agent-tools"))
    monkeypatch.setattr(
        app_routes, "handle_app_api_proxy", lambda *_a, **_k: pytest.fail("not proxied")
    )
    status, body = await _status(_CALL)
    assert (status, body) == (200, {"served": True})
    assert registry.seen == [(_APP, "agent-tools", _CALL)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [("other-app", "POST", "agent-tools"), (_APP, "GET", "agent-tools"), (_APP, "POST", "x")],
)
async def test_any_other_gateway_route_leaves_a_backend_app_on_its_proxy(
    monkeypatch, registry, route
):
    from kiro_crew.apps import routes as app_routes

    _install(backend={"entryPoint": "server.py", "port": "auto"})
    registry.routes.add(route)

    async def _proxy(request: web.Request, *, read_body: bytes | None = None) -> web.Response:
        return web.json_response({"proxied": True})

    monkeypatch.setattr(app_routes, "handle_app_api_proxy", _proxy)
    assert await _status(_CALL) == (200, {"proxied": True})
    assert registry.seen == []


def test_the_registry_reports_only_an_exact_registered_route():
    from kiro_crew.apps.route_registry import RouteRegistry, _RegisteredRoute

    reg = RouteRegistry(web.Application())

    async def _h(_r: Any, _c: Any) -> web.Response:
        return web.json_response({})

    reg._routes[_APP] = [
        _RegisteredRoute("POST", "/agent-tools", _h, has_params=False),
        _RegisteredRoute("POST", "/items/{item}", _h, has_params=True),
    ]
    assert reg.serves(_APP, "POST", "agent-tools")
    assert reg.serves(_APP, "POST", "/agent-tools")
    assert not reg.serves(_APP, "GET", "agent-tools")
    assert not reg.serves(_APP, "POST", "items/{item}")
    assert not reg.serves("other-app", "POST", "agent-tools")


@pytest.mark.asyncio
async def test_the_backend_receives_the_checked_body_through_the_real_proxy(monkeypatch):
    """End to end over sockets: the handler reads the body, so the proxy must forward it."""
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.apps import routes_proxy

    _install(backend={"entryPoint": "server.py", "port": "auto"})
    received: list[tuple[str, bool, bytes]] = []

    async def _backend(request: web.Request) -> web.Response:
        signed = "X-KiroCrew-Proxy" in request.headers
        received.append((request.path, signed, await request.read()))
        return web.json_response({"served": True})

    @web.middleware
    async def _internal(request: web.Request, handler: Any) -> web.StreamResponse:
        request["internal_auth"] = True
        return await handler(request)

    backend_app = web.Application()
    backend_app.router.add_post("/api/agent-tools", _backend)
    gateway = web.Application(middlewares=[_internal])
    gateway.router.add_post(app_tools._CALL_PATH, handler_mod.api_app_agent_tool_call)
    raw = json.dumps(_CALL).encode()
    async with TestServer(backend_app) as backend:
        base = str(backend.make_url("")).rstrip("/")
        monkeypatch.setattr(routes_proxy, "_resolve_app_backend_url", lambda _name: base)
        monkeypatch.setattr(routes_proxy, "_get_app_secret", lambda _name: "s" * 32)
        async with TestClient(TestServer(gateway)) as client:
            resp = await client.post(
                app_tools._CALL_PATH, data=raw, headers={"Content-Type": "application/json"}
            )
            assert (resp.status, await resp.json()) == (200, {"served": True})
    assert received == [("/api/agent-tools", True, raw)]


@pytest.mark.parametrize(
    ("call", "internal", "status", "code"),
    [
        (_CALL, False, 403, "loopback_only"),
        ({"tool": "fake_app_other", "arguments": {}}, True, 404, "unknown_tool"),
        ({**_CALL, "app": "core"}, True, 400, "invalid_json"),
        (
            {"tool": "fake_app_note", "arguments": {"item_id": "i", "x": 1}},
            True,
            400,
            "invalid_arguments",
        ),
        (
            {"tool": "fake_app_note", "arguments": {"item_id": "i" * 65}},
            True,
            400,
            "invalid_arguments",
        ),
        # Valid only after sanitizing: the bytes forwarded must be the bytes checked.
        (
            {"tool": "fake_app_note", "arguments": {"item_id": " i "}},
            True,
            400,
            "invalid_arguments",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_gateway_refuses_a_call_beyond_the_declaration(
    registry, call, internal, status, code
):
    _install()
    got_status, body = await _status(call, internal=internal)
    assert (got_status, body["code"]) == (status, code)
    assert registry.seen == []


@pytest.mark.asyncio
async def test_a_disabled_app_is_not_served(registry):
    _install(enabled=False)
    status, body = await _status(_CALL)
    assert (status, body["code"]) == (403, "app_not_enabled")
    assert registry.seen == []


# ── strictSession and a text answer ──────────────────────────────────────────


class _KeyedPost(_FakePost):
    """A fake ``_post`` that also records the ``session_key`` each call passed."""

    def __init__(self, resp: dict[str, Any]) -> None:
        super().__init__(resp)
        self.keys: list[Any] = []

    def __call__(self, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
        self.keys.append(kw.get("session_key", "<lenient>"))
        return super().__call__(path, body, **kw)


def test_a_strict_session_declaration_parses():
    (tool,) = AgentToolsConfig.from_value(_block(_tool(strictSession=True))).tools
    assert tool.strict_session is True
    (plain,) = AgentToolsConfig.from_value(_block()).tools
    assert plain.strict_session is False
    # The flag is how the core forwards, not something the model reads.
    assert "strictSession" not in tool.descriptor()


def test_a_process_backed_app_cannot_declare_strict_session():
    """The signed proxy strips ``X-Session-Key``, so a process backend would never see it."""
    raw = {
        "name": _APP,
        "version": "1.0.0",
        "displayName": "Fake",
        "description": "d",
        "agentTools": _block(_tool(strictSession=True)),
    }
    assert AppManifest.from_dict(raw).validate() == []
    backed = {**raw, "backend": {"entryPoint": "server.py", "port": "auto"}}
    assert any("strictSession" in e for e in AppManifest.from_dict(backed).validate())
    plain = {**backed, "agentTools": _block()}
    assert not any("strictSession" in e for e in AppManifest.from_dict(plain).validate())


def test_a_strict_session_tool_is_posted_with_the_strict_key(monkeypatch):
    _install(block=_block(_tool(strictSession=True)))
    post = _KeyedPost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:slot-a")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:parent")
    assert app_tools.app_agent_tool("fake_app_note", {"item_id": "i"}) == '{"ok": true}'
    assert post.keys == ["dashboard:slot-a"]


def test_a_strict_session_tool_is_not_posted_without_a_strict_key(monkeypatch):
    _install(block=_block(_tool(strictSession=True)))
    post = _KeyedPost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    out = app_tools.app_agent_tool("fake_app_note", {"item_id": "i"})
    assert out.startswith("Error: this tool needs a directly-identified"), out
    assert post.calls == []


def test_a_plain_tool_keeps_the_lenient_identity(monkeypatch):
    _install()
    post = _KeyedPost({"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    app_tools.app_agent_tool("fake_app_note", {"item_id": "i"})
    assert post.keys == ["<lenient>"]


def test_a_text_answer_is_the_tools_text_redacted_and_capped(monkeypatch):
    _install()
    monkeypatch.setattr(mcp_core, "_post", _FakePost({"text": "Recorded " + "z" * 60}))
    monkeypatch.setattr(app_tools, "redact", lambda text: text.replace("z", "y"))
    out = app_tools.app_agent_tool("fake_app_note", {"item_id": "i"})
    full = "Recorded " + "y" * 60
    assert out == full[:50] + f"\n… truncated ({len(full)} chars total)."


@pytest.mark.parametrize("resp", [{"text": "t", "extra": 1}, {"text": 5}, {"error": "no"}])
def test_any_other_answer_stays_json(monkeypatch, resp):
    _install(block=_block(_tool(maxResultChars=1000)))
    monkeypatch.setattr(mcp_core, "_post", _FakePost(resp))
    assert app_tools.app_agent_tool("fake_app_note", {"item_id": "i"}) == json.dumps(resp)


@pytest.mark.asyncio
async def test_the_gateway_refuses_a_strict_session_call_without_a_key(registry):
    _install(block=_block(_tool(strictSession=True)))
    status, body = await _status(_CALL)
    assert (status, body["code"]) == (403, "session_required")
    assert registry.seen == []


@pytest.mark.asyncio
async def test_the_gateway_serves_a_strict_session_call_with_its_key(registry):
    _install(block=_block(_tool(strictSession=True)))
    req = make_mocked_request(
        "POST", app_tools._CALL_PATH, headers={"X-Session-Key": "dashboard:slot-a"}
    )
    req["internal_auth"] = True
    req._read_bytes = json.dumps(_CALL).encode()
    resp = await handler_mod.api_app_agent_tool_call(req)
    assert (resp.status, json.loads(resp.body)) == (200, {"served": True})
    assert registry.seen == [(_APP, "agent-tools", _CALL)]
