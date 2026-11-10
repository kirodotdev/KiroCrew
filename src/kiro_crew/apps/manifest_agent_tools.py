"""The manifest ``agentTools`` block: MCP tools an app serves from its own process.

An app may give the agents the ``kirocrew-core`` MCP server serves a few tools of its
own. The manifest declares each tool as data (its name, the model-facing description and
the JSON Schema of its arguments) and names ONE route of the app's own API that serves
every call. The app's code answers the call where the app's code already runs: its
spawned backend process, or its own routes in the gateway. No app code runs in the
``kirocrew-core`` process, which holds the gateway's internal secret; that server only
lists the declared tools and forwards a call (``apps/app_agent_tools.py``).

What a declaration can and cannot do is decided here, before anything reads it:

- a tool name begins with the app's namespace and ``_``, so an app names tools in its own
  namespace only. The namespace is the app's own name (``-`` read as ``_``) unless the
  block states one (``namespace``), which a tool name a person or a policy already knows
  needs: an app keeps the names its tools had before they moved onto this seam;
- the route is a plain path below the app's own API, never a URL, a query or ``..``;
- an argument schema uses the small JSON Schema subset the core validator enforces
  (``string`` with ``enum`` / ``maxLength``, ``integer``, ``number``, ``boolean``, and
  arrays of strings or integers), so every declared constraint is one the core checks;
- ``strictSession: true`` asks the core to forward a call only from a session it
  identifies directly, never one a subagent's ancestor walk lands on, and to send
  that session's key with the call. Only an app whose routes run in the gateway may
  declare it: the proxy to a process backend strips that header.

A tool also states how long the core waits for its answer (``timeoutSeconds``) and
whether that answer is JSON or the app's own text (``result``, below); the core redacts
either.

A malformed block is kept with its errors: ``AppManifest.validate`` refuses the install,
and the reader skips every tool of a block that carries an error.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

#: At most this many tools per app: each one costs every session's context.
MAX_AGENT_TOOLS_PER_APP = 16
#: Per argument schema.
MAX_TOOL_PROPERTIES = 32
MAX_TOOL_DESCRIPTION_CHARS = 4_000
MAX_TOOL_TITLE_CHARS = 80
#: The answer a forwarded call returns is cut here at most; the default is the cap the
#: shipped declarations used before this seam existed.
MAX_RESULT_CHARS = 60_000
DEFAULT_RESULT_CHARS = 20_000
#: How long the core waits for a forwarded call by default, and at most: the default is
#: the wait every core-to-gateway post already has.
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 600
#: ``json``: the app answers with any JSON object, which the core serializes and redacts.
#: ``text``: the app answers with the model-facing text it formatted, which the core
#: redacts and relays as text; only a credential the answer names is kept verbatim.
RESULT_MODES = ("json", "text")

#: The longest string argument a tool may accept when its schema states no
#: ``maxLength``, and the largest ``maxLength`` it may state.
MAX_STRING_ARGUMENT_CHARS = 65_536
#: The most items an array argument may hold when its schema states no ``maxItems``,
#: and the largest ``maxItems`` it may state.
MAX_ARRAY_ITEMS = 256

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
MAX_NAMESPACE_CHARS = 32
#: A route below the app's own API: lowercase segments, no scheme, host, query or dot.
ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}(?:/[a-z0-9][a-z0-9_-]{0,63}){0,3}$")

_BLOCK_KEYS = frozenset({"namespace", "route", "tools"})
_TOOL_KEYS = frozenset(
    {
        "name",
        "title",
        "description",
        "inputSchema",
        "maxResultChars",
        "strictSession",
        "timeoutSeconds",
        "result",
    }
)
_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})
_SCALAR_KEYS: dict[str, frozenset[str]] = {
    "string": frozenset({"type", "description", "enum", "maxLength"}),
    "integer": frozenset({"type", "description", "minimum", "maximum"}),
    "number": frozenset({"type", "description", "minimum", "maximum"}),
    "boolean": frozenset({"type", "description"}),
    "array": frozenset({"type", "description", "items", "maxItems"}),
}
_ITEM_KEYS: dict[str, frozenset[str]] = {
    "string": frozenset({"type", "maxLength"}),
    "integer": frozenset({"type"}),
}


def tool_namespace(app_name: str, namespace: str = "") -> str:
    """The prefix every tool of *app_name* begins with: ``issue-radar`` -> ``issue_radar_``.

    A block that states a *namespace* uses it instead: ``pod`` -> ``pod_``.
    """
    return (namespace or app_name.replace("-", "_")) + "_"


@dataclass(frozen=True)
class AgentToolDecl:
    """One declared tool, as the manifest states it."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(hash=False)
    title: str = ""
    max_result_chars: int = DEFAULT_RESULT_CHARS
    #: The call is forwarded only from a strictly identified session, whose key it carries.
    strict_session: bool = False
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    result: str = "json"

    def descriptor(self) -> dict[str, Any]:
        """The ``tools/list`` descriptor."""
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": copy.deepcopy(self.input_schema),
        }
        if self.title:
            out["title"] = self.title
        return out


@dataclass
class AgentToolsConfig:
    """The ``agentTools`` block: one serving route and the tools it serves."""

    route: str = ""
    tools: list[AgentToolDecl] = field(default_factory=list)
    namespace: str = ""
    errors: list[str] = field(default_factory=list, compare=False)
    #: The block as written, kept so a round trip and the signed payload carry it.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def validate(self, app_name: str, process_backed: bool = False) -> list[str]:
        """Shape errors plus the rules that need the app: its namespace, and that a
        ``strictSession`` tool is served in the gateway (the signed proxy to a process
        backend strips ``X-Session-Key``, so such an app would never see the key)."""
        errors = list(self.errors)
        prefix = tool_namespace(app_name, self.namespace)
        for tool in self.tools:
            if (app_name or self.namespace) and not tool.name.startswith(prefix):
                errors.append(
                    f"agentTools: tool {tool.name!r} must begin with {prefix!r}, "
                    "the app's own namespace"
                )
            if process_backed and tool.strict_session:
                errors.append(
                    f"agentTools: tool {tool.name!r} declares strictSession, which a "
                    "process-backed app cannot receive"
                )
        return errors

    @classmethod
    def from_value(cls, raw: Any) -> AgentToolsConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            # Refused by ``validate``; nothing of a non-object is worth carrying on.
            return cls(errors=["agentTools: must be an object"])
        errors: list[str] = []
        unknown = sorted(set(raw) - _BLOCK_KEYS)
        if unknown:
            errors.append(f"agentTools: unknown keys {unknown}")
        namespace = raw.get("namespace", "")
        if not isinstance(namespace, str) or (
            namespace
            and (len(namespace) > MAX_NAMESPACE_CHARS or not NAMESPACE_RE.fullmatch(namespace))
        ):
            errors.append(
                f"agentTools.namespace must be a lowercase identifier of at most "
                f"{MAX_NAMESPACE_CHARS} characters, such as 'pod'"
            )
            namespace = ""
        route = raw.get("route", "")
        if not isinstance(route, str) or not ROUTE_RE.fullmatch(route):
            errors.append(
                f"agentTools.route {route!r} must be a plain path below the app's API, "
                "such as 'agent-tools'"
            )
            route = ""
        tools_raw = raw.get("tools")
        tools: list[AgentToolDecl] = []
        if not isinstance(tools_raw, list) or not tools_raw:
            errors.append("agentTools.tools must be a non-empty list")
        elif len(tools_raw) > MAX_AGENT_TOOLS_PER_APP:
            errors.append(
                f"agentTools.tools: at most {MAX_AGENT_TOOLS_PER_APP} tools "
                f"(declared {len(tools_raw)})"
            )
        else:
            for index, item in enumerate(tools_raw):
                tool, tool_errors = _parse_tool(f"agentTools.tools[{index}]", item)
                errors.extend(tool_errors)
                if tool is not None:
                    tools.append(tool)
            names = [t.name for t in tools]
            if len(set(names)) != len(names):
                errors.append("agentTools.tools: a tool name is declared twice")
        return cls(
            route=route, tools=tools, namespace=namespace, errors=errors, raw=copy.deepcopy(raw)
        )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_tool(where: str, raw: Any) -> tuple[AgentToolDecl | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{where} must be an object"]
    errors: list[str] = []
    unknown = sorted(set(raw) - _TOOL_KEYS)
    if unknown:
        errors.append(f"{where}: unknown keys {unknown}")
    name = raw.get("name")
    if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
        return None, [*errors, f"{where}.name must be a lowercase identifier"]
    where = f"agentTools tool {name!r}"
    description = raw.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > MAX_TOOL_DESCRIPTION_CHARS
    ):
        errors.append(
            f"{where}: description must be non-empty text of at most "
            f"{MAX_TOOL_DESCRIPTION_CHARS} characters"
        )
    title = raw.get("title", "")
    if (
        not isinstance(title, str)
        or len(title) > MAX_TOOL_TITLE_CHARS
        or any(c in title for c in "\r\n")
    ):
        errors.append(f"{where}: title must be one line of at most {MAX_TOOL_TITLE_CHARS} chars")
    cap = raw.get("maxResultChars", DEFAULT_RESULT_CHARS)
    if not _is_int(cap) or not 0 < cap <= MAX_RESULT_CHARS:
        errors.append(f"{where}: maxResultChars must be an integer from 1 to {MAX_RESULT_CHARS}")
    strict_session = raw.get("strictSession", False)
    if not isinstance(strict_session, bool):
        errors.append(f"{where}: strictSession must be true or false")
    timeout = raw.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    if not _is_int(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        errors.append(f"{where}: timeoutSeconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    result = raw.get("result", "json")
    if result not in RESULT_MODES:
        errors.append(f"{where}: result must be one of {list(RESULT_MODES)}")
    schema = raw.get("inputSchema")
    errors.extend(_schema_errors(where, schema))
    if errors:
        return None, errors
    return (
        AgentToolDecl(
            name=name,
            description=description,  # type: ignore[arg-type]
            input_schema=copy.deepcopy(schema),  # type: ignore[arg-type]
            title=title,
            max_result_chars=cap,
            strict_session=strict_session,
            timeout_seconds=timeout,
            result=result,
        ),
        [],
    )


def _schema_errors(where: str, schema: Any) -> list[str]:
    """Why *schema* is outside the subset the core validator enforces, if it is."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return [f"{where}: inputSchema must be an object schema"]
    errors: list[str] = []
    unknown = sorted(set(schema) - _OBJECT_KEYS)
    if unknown:
        errors.append(f"{where}: inputSchema has unsupported keys {unknown}")
    if schema.get("additionalProperties", False) is not False:
        errors.append(f"{where}: inputSchema may not admit additional properties")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > MAX_TOOL_PROPERTIES:
        return [
            *errors,
            f"{where}: inputSchema.properties must be an object of at most "
            f"{MAX_TOOL_PROPERTIES} arguments",
        ]
    for arg, prop in properties.items():
        if not isinstance(arg, str) or not TOOL_NAME_RE.fullmatch(arg):
            errors.append(f"{where}: argument {arg!r} must be a lowercase identifier")
            continue
        errors.extend(_property_errors(f"{where} argument {arg!r}", prop))
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or not all(isinstance(r, str) for r in required)
        or not set(required) <= set(properties)
        or len(set(required)) != len(required)
    ):
        errors.append(f"{where}: inputSchema.required must list declared arguments once each")
    return errors


def _property_errors(where: str, prop: Any) -> list[str]:
    if not isinstance(prop, dict) or prop.get("type") not in _SCALAR_KEYS:
        return [f"{where}: type must be one of {sorted(_SCALAR_KEYS)}"]
    kind = prop["type"]
    errors: list[str] = []
    unknown = sorted(set(prop) - _SCALAR_KEYS[kind])
    if unknown:
        errors.append(f"{where}: unsupported keys {unknown}")
    if "description" in prop and not isinstance(prop["description"], str):
        errors.append(f"{where}: description must be a string")
    if "maxLength" in prop and (
        not _is_int(prop["maxLength"]) or not 0 < prop["maxLength"] <= MAX_STRING_ARGUMENT_CHARS
    ):
        errors.append(
            f"{where}: maxLength must be an integer from 1 to {MAX_STRING_ARGUMENT_CHARS}"
        )
    if "enum" in prop and (
        not isinstance(prop["enum"], list)
        or not prop["enum"]
        or not all(isinstance(v, str) and v for v in prop["enum"])
    ):
        errors.append(f"{where}: enum must be a non-empty list of non-empty strings")
    for bound in ("minimum", "maximum"):
        if bound in prop and (
            isinstance(prop[bound], bool) or not isinstance(prop[bound], (int, float))
        ):
            errors.append(f"{where}: {bound} must be a number")
    if kind == "array":
        items = prop.get("items")
        if not isinstance(items, dict) or items.get("type") not in _ITEM_KEYS:
            errors.append(f"{where}: items must be a string or integer schema")
        else:
            stray = sorted(set(items) - _ITEM_KEYS[items["type"]])
            if stray:
                errors.append(f"{where}: items has unsupported keys {stray}")
            if "maxLength" in items and (
                not _is_int(items["maxLength"])
                or not 0 < items["maxLength"] <= MAX_STRING_ARGUMENT_CHARS
            ):
                errors.append(f"{where}: items.maxLength must be from 1 to 65536")
        if "maxItems" in prop and (
            not _is_int(prop["maxItems"]) or not 0 < prop["maxItems"] <= MAX_ARRAY_ITEMS
        ):
            errors.append(f"{where}: maxItems must be an integer from 1 to {MAX_ARRAY_ITEMS}")
    return errors
