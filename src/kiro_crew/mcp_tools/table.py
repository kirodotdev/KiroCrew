"""One table that is a Kiro Crew MCP server's whole tool surface.

A server built on :class:`ToolTable` declares each tool once, as a :class:`Tool`
row: its advertised descriptor, the identity it requires, the dashboard routes
it may reach, and the function that runs it. The table answers both halves of
the protocol from those rows:

* :meth:`ToolTable.list` is the ``tools/list`` answer, in row order, with each
  tool's declared display title (``mcp_tool_titles``) filled in.
* :meth:`ToolTable.call` is one ``tools/call`` frame: argument validation and
  the SEL invocation record (``mcp_shared.call_tool_with_logging``), the unknown
  tool reply, the row's identity gate, a second validation pass, then the row's
  ``run``.

``run(args, ctx)`` receives arguments already validated against the server's
schema registry and a :class:`ToolContext` whose client reaches only the
routes the row declares. A ``"strict"`` row runs only after the table's strict
gate has verified the caller, and finds the verified key in
``ctx.caller_key``; on a refusal the gate's text is the reply and nothing is
sent.

Identity, per row:

* ``"attribution"``: its SEL record carries the frame's attribution key (the
  lenient resolver, falling back to the server name). The tool body applies any
  narrower check itself, through ``ctx.caller``.
* ``"strict"``: the same record, and the strict gate runs before the body.

The caller is a port too. :class:`GatewayCaller` is production -- ``mcp_core``'s
resolvers, read at call time -- and :class:`Caller` is an identity fixed up
front, for tests. ``require_strict_session_key`` keeps the gate's contract:
``(key, "")`` when the caller is strictly identified, else ``("", refusal +
diagnosis)``.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from kiro_crew import mcp_core
from kiro_crew.mcp_shared import call_tool_with_logging
from kiro_crew.mcp_tool_titles import with_titles
from kiro_crew.mcp_tools.dashboard_client import (
    DashboardClient,
    LoopbackDashboardClient,
    restrict_routes,
)
from kiro_crew.validation import ToolSchema, validate_tool_args

Identity = Literal["attribution", "strict"]
_IDENTITIES: frozenset[str] = frozenset({"attribution", "strict"})


class CallerIdentity(Protocol):
    """Who is calling, as one tools/call frame can establish it."""

    def attribution(self) -> str:
        """The key the frame is attributed to, or ``""``. Never an authorization."""
        ...

    def require_strict_session_key(self, refusal: str) -> tuple[str, str]:
        """``(key, "")`` for a strictly identified caller, else ``("", refusal + why)``."""
        ...


@dataclass(frozen=True)
class Caller:
    """A caller identity fixed up front.

    ``Caller.strict(key)`` is a caller the gateway vouches for.
    ``Caller.unverified(key, diagnosis)`` is one only the lenient resolver can
    name -- a spawned subagent whose process walk lands on its parent's slot,
    say -- so every strict check refuses it, appending ``diagnosis``.
    """

    key: str = ""
    verified: bool = False
    diagnosis: str = ""

    @classmethod
    def strict(cls, key: str) -> Caller:
        return cls(key=key, verified=True)

    @classmethod
    def unverified(cls, key: str = "", diagnosis: str = "") -> Caller:
        return cls(key=key, verified=False, diagnosis=diagnosis)

    def attribution(self) -> str:
        return self.key

    def require_strict_session_key(self, refusal: str) -> tuple[str, str]:
        if self.verified and self.key:
            return self.key, ""
        return "", refusal + self.diagnosis


class GatewayCaller:
    """Production identity for ``server``: ``mcp_core``'s resolvers, per call.

    Read as module attributes when asked, so the per-frame caller block the
    stdio loop installs is what they see, and so does any test that rebinds a
    resolver in ``mcp_core``.
    """

    def __init__(self, server: str) -> None:
        self.server = server

    def attribution(self) -> str:
        return mcp_core._resolve_session_key()

    def require_strict_session_key(self, refusal: str) -> tuple[str, str]:
        return mcp_core.require_strict_session_key(refusal, server=self.server)


@dataclass(frozen=True)
class ToolContext:
    """What one tools/call frame hands a tool: its dashboard and its caller.

    ``caller_key`` is set by :meth:`ToolTable.call` for a ``"strict"`` row: the
    key its gate verified, which every request the row sends on the caller's
    authority must carry. It is ``""`` for every other row.
    """

    client: DashboardClient
    caller: CallerIdentity
    caller_key: str = ""

    @classmethod
    def for_frame(cls, server: str) -> ToolContext:
        """The production context for one frame of ``server``."""
        return cls(client=LoopbackDashboardClient(), caller=GatewayCaller(server))


#: A tool body: validated arguments and the frame's context in, reply text out.
Run = Callable[[dict[str, Any], ToolContext], str]

#: A server's strict gate: ``(verified key, "")`` or ``("", refusal text)``.
StrictGate = Callable[[ToolContext], tuple[str, str]]


@dataclass(frozen=True)
class Tool:
    """One tool: what is advertised, who may call it, what it reaches, what runs.

    ``routes`` lists every dashboard route the body may send, as ``"METHOD
    /api/path"`` with ``{name}`` for an interpolated part and no query string.
    The client the body receives refuses anything else before sending it.
    """

    name: str
    description: str
    schema: Mapping[str, Any]
    run: Run
    identity: Identity
    routes: tuple[str, ...] = ()

    def descriptor(self) -> dict[str, Any]:
        """The ``tools/list`` entry, freshly built so a caller may mutate it."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": copy.deepcopy(dict(self.schema)),
        }


class ToolTable:
    """A server's tools, by name, answering ``tools/list`` and ``tools/call``.

    ``validators`` is the server's schema registry in ``validation``; a tool it
    does not name has its arguments passed through as sent, as before. A
    ``"strict"`` row requires ``strict_gate``. The schema check runs a second
    time on the already-validated arguments, after the strict gate and before
    the body: ``validation.sanitize_string`` is not idempotent (NFC runs before
    the format-character strip, so a second pass can still compose what the
    first pass exposed), and the bodies are specified on the
    twice-validated value. Duplicate names, an unknown identity, and a strict
    row with no gate are refused at construction.
    """

    def __init__(
        self,
        server: str,
        tools: Iterable[Tool],
        *,
        validators: Mapping[str, ToolSchema] | None = None,
        strict_gate: StrictGate | None = None,
    ) -> None:
        self.server = server
        self._tools: tuple[Tool, ...] = tuple(tools)
        self._by_name: dict[str, Tool] = {}
        for tool in self._tools:
            if tool.name in self._by_name:
                raise ValueError(f"{server}: tool {tool.name!r} is declared twice")
            if tool.identity not in _IDENTITIES:
                raise ValueError(f"{server}: tool {tool.name!r} has identity {tool.identity!r}")
            if tool.identity == "strict" and strict_gate is None:
                raise ValueError(f"{server}: strict tool {tool.name!r} needs a strict_gate")
            self._by_name[tool.name] = tool
        self._validators: Mapping[str, ToolSchema] = validators or {}
        self._strict_gate = strict_gate

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools)

    def names(self, identity: Identity | None = None) -> tuple[str, ...]:
        """Tool names in row order, optionally only those with ``identity``."""
        return tuple(t.name for t in self._tools if identity is None or t.identity == identity)

    def list(self) -> list[dict[str, Any]]:
        """The ``tools/list`` answer: every descriptor, titled, in row order."""
        return with_titles(self.server, [t.descriptor() for t in self._tools])

    def validate(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """``args`` checked against ``name``'s registered schema, or as sent."""
        schema = self._validators.get(name)
        if schema:
            return validate_tool_args(args, schema)
        return args

    def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> str:
        """One ``tools/call`` frame, start to finish. Never raises a refusal.

        A body's exceptions propagate, exactly as the stdio loop has always
        received them.
        """
        tool = self._by_name.get(name)
        session_key = ctx.caller.attribution() or self.server

        def _dispatch(_name: str, valid: dict[str, Any]) -> str:
            if tool is None:
                return f"Error: unknown tool '{name}'"
            # Scoped before the gate as well, so nothing this row's call runs can
            # send a route the row did not declare. ``caller_key`` is only ever
            # the strict gate's verified key: a value the caller put there is
            # cleared, so a non-strict body cannot mistake it for one.
            scoped = dataclasses.replace(
                ctx, client=restrict_routes(ctx.client, tool.routes), caller_key=""
            )
            if tool.identity == "strict":
                assert self._strict_gate is not None  # checked at construction
                key, refusal = self._strict_gate(scoped)
                if not key:
                    return refusal
                scoped = dataclasses.replace(scoped, caller_key=key)
            return tool.run(self.validate(name, valid), scoped)

        return call_tool_with_logging(
            name,
            args,
            self.validate,
            _dispatch,
            session_key=session_key,
            downstream_service=self.server,
        )
