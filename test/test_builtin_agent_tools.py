"""The agent-tool seam: what a shipped built-in declares, and what the core builds from it.

``kiro_crew.apps.builtin_agent_tools`` reads each built-in's ``agent_tools`` data module.
These tests hold the seam itself to its contract, with synthetic declarations, so they
name no app:

- with no built-in declaring a tool, every answer is empty;
- a declaration reaches the tool table, the app gate, the validation registry and the
  internal-path table through the one loader;
- every malformed or overreaching declaration raises at load.

What one app's tool does is pinned in that app's own ``tests/``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import builtin_agent_tools as seam
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS
from kiro_crew.mcp_tools import apps as apps_tools
from kiro_crew.validation import MCP_CORE_SCHEMAS


def _tool(**overrides: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": "fake_app_note",
        "description": "Post one note.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}, "note": {"type": "string"}},
            "required": ["item_id"],
        },
        "fields": (
            {"name": "item_id", "required": True, "max_len": 64},
            {"name": "note", "max_len": 128, "default": ""},
        ),
        "route": "/note",
        "query": (("id", "item_id"),),
        "body_constants": {"role": "agent"},
        "body_fields": ("note",),
        "response_cap": 50,
    }
    tool.update(overrides)
    return tool


def _declaration(*tools: dict[str, Any], app: str = "fake-app") -> dict[str, Any]:
    return {"app": app, "tools": tools or (_tool(),)}


@pytest.fixture
def fake_builtins(monkeypatch):
    """Feed the loader synthetic declarations; the cached real read is restored."""

    def install(packages: dict[str, object]) -> tuple[seam.AgentTool, ...]:
        declared = [(package, raw) for package, raw in packages.items() if raw is not None]
        monkeypatch.setattr(seam, "_declarations", lambda: declared)
        seam.agent_tools.cache_clear()
        return seam.agent_tools()

    yield install
    seam.agent_tools.cache_clear()


def test_the_loader_reads_exactly_the_directories_that_ship_a_declaration():
    from kiro_crew.apps import builtins

    root = Path(builtins.__file__).resolve().parent
    shipped = sorted(p.name for p in root.iterdir() if (p / "agent_tools.py").is_file())
    assert [package for package, _ in seam._declarations()] == shipped


def test_no_declaration_means_every_answer_is_empty(fake_builtins):
    assert fake_builtins({"plain_app": None}) == ()
    assert seam.internal_paths() == frozenset()
    assert seam.tool_schemas() == {}
    with pytest.raises(KeyError):
        seam.agent_tool("plain_app_note")


def test_a_declaration_builds_the_tool_its_schema_and_its_one_path(fake_builtins):
    (tool,) = fake_builtins({"fake_app": _declaration(), "other_app": None})
    assert tool.app == "fake-app"
    assert tool.url == "/apps/fake-app/api/note"
    assert seam.agent_tool("fake_app_note") is tool
    assert seam.internal_paths() == frozenset({"/apps/fake-app/api/note"})
    assert tool.descriptor() == {
        "name": "fake_app_note",
        "description": "Post one note.",
        "inputSchema": _tool()["inputSchema"],
    }
    schema = seam.tool_schemas()["fake_app_note"]
    assert [(f.name, f.type, f.required, f.max_len, f.default) for f in schema.fields] == [
        ("item_id", str, True, 64, None),
        ("note", str, False, 128, ""),
    ]


def test_a_descriptor_is_a_copy(fake_builtins):
    (tool,) = fake_builtins({"fake_app": _declaration()})
    tool.descriptor()["inputSchema"]["properties"].clear()
    assert tool.descriptor()["inputSchema"]["properties"]


def test_the_shipped_declarations_reach_every_core_table():
    """Each consumer reads the one loader, so a declared tool is complete or absent."""
    tools = seam.agent_tools()
    for tool in tools:
        assert tool.descriptor() in apps_tools.schemas()
        assert apps_tools.HANDLERS[tool.name] is apps_tools.builtin_agent_tool
        assert apps_tools.TOOL_APPS[tool.name] == tool.app
        assert MCP_CORE_SCHEMAS[tool.name] == seam.tool_schemas()[tool.name]
        assert tool.url in _MIXED_INTERNAL_API_PATHS


class _FakePost:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any]) -> object:
        self.calls.append((url, copy.deepcopy(body)))
        return self.answer


@pytest.mark.parametrize(
    ("args", "url", "body"),
    [
        (
            {"item_id": "a/b?#", "note": "hi"},
            "/apps/fake-app/api/note?id=a%2Fb%3F%23",
            {"role": "agent", "note": "hi"},
        ),
        ({"item_id": "x", "note": ""}, "/apps/fake-app/api/note?id=x", {"role": "agent"}),
        # A required argument is sent even empty; the backend is its grammar's authority.
        ({"item_id": ""}, "/apps/fake-app/api/note?id=", {"role": "agent"}),
    ],
)
def test_the_handler_posts_the_declared_route_and_body(fake_builtins, monkeypatch, args, url, body):
    fake_builtins({"fake_app": _declaration()})
    post = _FakePost({"ok": True})
    monkeypatch.setattr(apps_tools.mcp_core, "_post", post)
    assert apps_tools.builtin_agent_tool("fake_app_note", args) == '{"ok": true}'
    assert post.calls == [(url, body)]


def test_an_optional_query_argument_is_sent_only_when_given(fake_builtins, monkeypatch):
    tool = _tool(
        query=(("id", "item_id"), ("n", "note")),
        body_fields=(),
    )
    fake_builtins({"fake_app": _declaration(tool)})
    post = _FakePost({})
    monkeypatch.setattr(apps_tools.mcp_core, "_post", post)
    apps_tools.builtin_agent_tool("fake_app_note", {"item_id": "i", "note": ""})
    apps_tools.builtin_agent_tool("fake_app_note", {"item_id": "i", "note": "a b"})
    assert [c[0] for c in post.calls] == [
        "/apps/fake-app/api/note?id=i",
        "/apps/fake-app/api/note?id=i&n=a%20b",
    ]


def test_the_answer_is_redacted_then_capped(fake_builtins, monkeypatch):
    fake_builtins({"fake_app": _declaration()})
    monkeypatch.setattr(apps_tools.mcp_core, "_post", _FakePost({"t": "z" * 80}))
    monkeypatch.setattr(apps_tools, "redact", lambda text: text.replace("z", "y"))
    out = apps_tools.builtin_agent_tool("fake_app_note", {"item_id": "i"})
    full = '{"t": "' + "y" * 80 + '"}'
    assert out == full[:50] + f"\n… truncated ({len(full)} chars total)."


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        (["not", "a", "mapping"], "must be a mapping"),
        ({**_declaration(), "extra": 1}, "unknown keys"),
        (_declaration(app="other-app"), "only its own"),
        ({"app": "fake-app", "tools": ()}, "non-empty sequence"),
        (_declaration(_tool(name="core_tool")), "begin with fake_app_"),
        (_declaration(_tool(name="Fake_App")), "lowercase identifier"),
        (_declaration(_tool(surprise=True)), "unknown keys"),
        (_declaration(_tool(description=" ")), "description"),
        (_declaration(_tool(route="/../api/x")), "plain path"),
        (_declaration(_tool(route="/note?x=1")), "plain path"),
        (_declaration(_tool(route="note")), "plain path"),
        (_declaration(_tool(route="/note/")), "plain path"),
        (_declaration(_tool(fields=[{"name": "item_id", "type": "int"}])), "unknown keys"),
        (
            _declaration(_tool(fields=({"name": "item_id"}, {"name": "item_id"}))),
            "twice",
        ),
        (_declaration(_tool(fields=({"name": "item_id", "max_len": -1},))), "max_len"),
        (_declaration(_tool(fields=({"name": "item_id", "allowed": ()},))), "allowed"),
        (_declaration(_tool(fields=({"name": "item_id", "default": 3},))), "default"),
        (
            _declaration(_tool(inputSchema={"type": "object", "properties": {}})),
            "exactly the declared fields",
        ),
        (
            _declaration(
                _tool(
                    inputSchema={
                        **_tool()["inputSchema"],
                        "required": [],
                    }
                )
            ),
            "'required' must match",
        ),
        (_declaration(_tool(query=(("id", "secret"),))), "undeclared arguments"),
        (_declaration(_tool(body_fields=("secret",))), "undeclared arguments"),
        (_declaration(_tool(query=(("id",),))), "two non-empty strings"),
        (_declaration(_tool(body_fields=("note",), body_constants={"note": "x"})), "both"),
        (_declaration(_tool(response_cap=0)), "response_cap"),
    ],
)
def test_a_malformed_declaration_raises_at_load(fake_builtins, declaration, message):
    with pytest.raises(ValueError, match=message):
        fake_builtins({"fake_app": declaration})


def test_a_tool_cannot_be_declared_twice(fake_builtins):
    """Each name carries its own package prefix, so a clash can only be within one app."""
    with pytest.raises(ValueError, match="declared twice"):
        fake_builtins({"fake_app": _declaration(_tool(), _tool())})
