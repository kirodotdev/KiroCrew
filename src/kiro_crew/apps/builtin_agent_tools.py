"""The MCP tools a SHIPPED built-in app declares for its agent, read through one seam.

Some built-in apps hand their agent work whose progress the agent must report back to
one of the app's own backend routes. The agent cannot call that route itself: it sits
behind the gateway's app reverse proxy (``/apps/<app>/api/<route>``), which needs a
dashboard credential no agent session holds. The ``kirocrew-core`` MCP server holds the
gateway's internal secret, so the tool runs there, and the gateway admits that one
route to internal-secret callers. Spelled as core code, such a tool names its app in
the tool table, the validation registry and the internal-path table, and the app could
not leave this repository without a core edit.

So the core names no app. A built-in app directory MAY ship an ``agent_tools``
submodule holding one ``AGENT_TOOLS`` mapping of plain data; this module reads every
such mapping once, and the core builds each tool from it:

- ``mcp_tools.apps`` advertises the descriptor, hides it while the app is switched off,
  and runs ONE handler of its own for every such tool: POST the declared body to the
  declared route, redact the answer, cap it;
- ``validation`` checks a call against the declared fields;
- ``dashboard.server_internal_paths`` admits exactly the declared route to
  internal-secret callers, as a mixed path.

With no built-in shipping one, every answer is empty.

Trust. Only directories under ``kiro_crew/apps/builtins/`` are read, so a declaration is
package code reviewed in this repository, never a manifest field an installed or
registry app could claim. The handler is the core's, so a declaration chooses data
only: which of its OWN routes the tool posts to, and which validated arguments reach
it. Guard rails, each raising at load rather than skipping the row, because a shipped
tool that silently vanished would fail toward a broken app with no signal:

- ``app`` must be the directory's installed name;
- a tool name must begin with the package name and ``_``, so it cannot shadow a core
  tool or another app's;
- a route is a plain path, so ``/apps/<app>/api<route>`` stays under the app's own
  proxy namespace;
- the advertised ``inputSchema`` must name exactly the validated fields, and every
  forwarded argument must be one of them.

``agent_tools`` is a data module by contract. It is imported in the MCP servers, the
validation registry and the gateway's path table, so it imports nothing, and the
loader finds it by file so that no other app's package is imported.
"""

from __future__ import annotations

import copy
import functools
import importlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.validation_parts.field_engine import ToolSchema

_SUBMODULE = "agent_tools"
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ROUTE_RE = re.compile(r"^(?:/[A-Za-z0-9_-]+)+$")
_TOOL_KEYS = frozenset(
    {
        "name",
        "description",
        "inputSchema",
        "fields",
        "route",
        "query",
        "body_constants",
        "body_fields",
        "response_cap",
    }
)
_FIELD_KEYS = frozenset({"name", "required", "max_len", "allowed", "default"})


@dataclass(frozen=True)
class AgentToolField:
    """One string argument a tool accepts, as ``validation`` checks it."""

    name: str
    required: bool = False
    #: The longest value accepted; ``0`` is no limit.
    max_len: int = 0
    #: The only values accepted, or ``None`` for any.
    allowed: frozenset[str] | None = None
    #: What an omitted argument becomes; ``None`` leaves it absent.
    default: str | None = None


@dataclass(frozen=True)
class AgentTool:
    """One tool a built-in app declares, as the core builds and runs it."""

    #: The app's installed name (``app.json`` ``name``); the tool is hidden while that
    #: app is switched off.
    app: str
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(hash=False)
    fields: tuple[AgentToolField, ...]
    #: The app backend route, below the proxy's ``/apps/<app>/api``.
    route: str
    #: ``(query parameter, argument)`` pairs, in URL order. A required argument is
    #: always sent; an optional one only when it is non-empty.
    query: tuple[tuple[str, str], ...] = ()
    #: Fixed ``(key, value)`` pairs every request body starts with.
    body_constants: tuple[tuple[str, str], ...] = ()
    #: Arguments copied into the body, in order, when they are non-empty.
    body_fields: tuple[str, ...] = ()
    #: Characters of the redacted answer returned before it is cut.
    response_cap: int = 20_000

    @property
    def url(self) -> str:
        """The gateway path the tool posts to, and the one path it is admitted on."""
        return f"/apps/{self.app}/api{self.route}"

    def descriptor(self) -> dict[str, Any]:
        """The ``tools/list`` descriptor: name, description and input schema."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": copy.deepcopy(dict(self.input_schema)),
        }


def _installed_name(package: str) -> str:
    return package.replace("_", "-")


def _string_pairs(where: str, raw: object, *, mapping: bool) -> tuple[tuple[str, str], ...]:
    items = list(raw.items()) if mapping and isinstance(raw, Mapping) else raw
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"{where} must be a {'mapping' if mapping else 'sequence of pairs'}")
    pairs = []
    for item in items:
        if not (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and all(isinstance(s, str) and s for s in item)
        ):
            raise ValueError(f"{where} holds {item!r}; each entry pairs two non-empty strings")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _parse_field(where: str, raw: object) -> AgentToolField:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping")
    unknown = set(raw) - _FIELD_KEYS
    if unknown:
        raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
        raise ValueError(f"{where}['name'] must be a lowercase identifier")
    required = raw.get("required", False)
    max_len = raw.get("max_len", 0)
    allowed = raw.get("allowed")
    default = raw.get("default")
    if not isinstance(required, bool):
        raise ValueError(f"{where}['required'] must be a bool")
    if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len < 0:
        raise ValueError(f"{where}['max_len'] must be a non-negative int")
    if allowed is not None and (
        not isinstance(allowed, (list, tuple, frozenset, set))
        or not allowed
        or not all(isinstance(v, str) and v for v in allowed)
    ):
        raise ValueError(f"{where}['allowed'] must be a non-empty set of strings")
    if default is not None and not isinstance(default, str):
        raise ValueError(f"{where}['default'] must be a string")
    return AgentToolField(
        name=name,
        required=required,
        max_len=max_len,
        allowed=frozenset(allowed) if allowed is not None else None,
        default=default,
    )


def _parse_tool(package: str, app: str, where: str, raw: object) -> AgentTool:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping")
    unknown = set(raw) - _TOOL_KEYS
    if unknown:
        raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
        raise ValueError(f"{where}['name'] must be a lowercase identifier")
    if not name.startswith(f"{package}_"):
        raise ValueError(f"{where} names {name!r}; a built-in's tools begin with {package}_")
    where = f"{where}[{name!r}]"
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{where}['description'] must be a non-empty string")
    route = raw.get("route")
    if not isinstance(route, str) or not _ROUTE_RE.match(route):
        raise ValueError(f"{where}['route'] {route!r} must be a plain path such as '/thread'")
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, (list, tuple)):
        raise ValueError(f"{where}['fields'] must be a sequence")
    fields_ = tuple(_parse_field(f"{where}['fields'][{i}]", f) for i, f in enumerate(raw_fields))
    names = [f.name for f in fields_]
    if len(set(names)) != len(names):
        raise ValueError(f"{where}['fields'] names an argument twice")
    schema = raw.get("inputSchema")
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise ValueError(f"{where}['inputSchema'] must be an object schema")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != set(names):
        raise ValueError(f"{where}['inputSchema'] must advertise exactly the declared fields")
    if set(schema.get("required", ())) != {f.name for f in fields_ if f.required}:
        raise ValueError(f"{where}['inputSchema'] 'required' must match the required fields")
    query = _string_pairs(f"{where}['query']", raw.get("query", ()), mapping=False)
    body_constants = _string_pairs(
        f"{where}['body_constants']", raw.get("body_constants", {}), mapping=True
    )
    body_fields = raw.get("body_fields", ())
    if not isinstance(body_fields, (list, tuple)) or not all(
        isinstance(b, str) for b in body_fields
    ):
        raise ValueError(f"{where}['body_fields'] must be a sequence of argument names")
    forwarded = [arg for _, arg in query] + list(body_fields)
    stray = sorted(set(forwarded) - set(names))
    if stray:
        raise ValueError(f"{where} forwards undeclared arguments: {stray}")
    clash = sorted({key for key, _ in body_constants} & set(body_fields))
    if clash:
        raise ValueError(f"{where} sets body keys both fixed and forwarded: {clash}")
    cap = raw.get("response_cap", 20_000)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        raise ValueError(f"{where}['response_cap'] must be a positive int")
    return AgentTool(
        app=app,
        name=name,
        description=description,
        input_schema=copy.deepcopy(dict(schema)),
        fields=fields_,
        route=route,
        query=query,
        body_constants=body_constants,
        body_fields=tuple(body_fields),
        response_cap=cap,
    )


def _parse(package: str, raw: object) -> tuple[AgentTool, ...]:
    """One ``AGENT_TOOLS`` mapping as its tools, or ``ValueError``."""
    where = f"kiro_crew.apps.builtins.{package}.{_SUBMODULE}.AGENT_TOOLS"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping, not {type(raw).__name__}")
    unknown = set(raw) - {"app", "tools"}
    if unknown:
        raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")
    app = raw.get("app")
    if app != _installed_name(package):
        raise ValueError(f"{where} declares tools for {app!r}; a built-in may declare only its own")
    tools = raw.get("tools")
    if not isinstance(tools, (list, tuple)) or not tools:
        raise ValueError(f"{where}['tools'] must be a non-empty sequence")
    return tuple(
        _parse_tool(package, app, f"{where}['tools'][{i}]", t) for i, t in enumerate(tools)
    )


def _declarations() -> list[tuple[str, object]]:
    """``(package, AGENT_TOOLS)`` for each built-in directory that ships ``agent_tools``."""
    from kiro_crew.apps import builtins

    root = Path(builtins.__file__).resolve().parent
    # Looked for on disk before importing: an import would run each built-in's package
    # ``__init__`` on the MCP server and validation paths, for packages that declare
    # nothing.
    packages = sorted(p.name for p in root.iterdir() if (p / f"{_SUBMODULE}.py").is_file())
    return [
        (
            package,
            getattr(
                importlib.import_module(f"{builtins.__name__}.{package}.{_SUBMODULE}"),
                "AGENT_TOOLS",
                None,
            ),
        )
        for package in packages
    ]


@functools.lru_cache(maxsize=1)
def agent_tools() -> tuple[AgentTool, ...]:
    """Every shipped built-in's declared agent tools, by package name. Read once."""
    out = [tool for package, raw in _declarations() for tool in _parse(package, raw)]
    names = [t.name for t in out]
    if len(set(names)) != len(names):
        raise ValueError(f"an agent tool is declared twice: {sorted(names)}")
    return tuple(out)


def agent_tool(name: str) -> AgentTool:
    """The declared tool called *name*; ``KeyError`` when no built-in declares it."""
    for tool in agent_tools():
        if tool.name == name:
            return tool
    raise KeyError(name)


def internal_paths() -> frozenset[str]:
    """The gateway paths the declared tools post to, admitted to internal-secret callers."""
    return frozenset(t.url for t in agent_tools())


def tool_schemas() -> dict[str, ToolSchema]:
    """The validation schema of each declared tool, by tool name."""
    from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema

    return {
        t.name: ToolSchema(
            tool_name=t.name,
            fields=[
                FieldSpec(
                    f.name,
                    str,
                    required=f.required,
                    max_len=f.max_len,
                    allowed=f.allowed,
                    default=f.default,
                )
                for f in t.fields
            ],
        )
        for t in agent_tools()
    }
