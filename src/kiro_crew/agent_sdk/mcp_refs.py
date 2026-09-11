"""Which of an agent spec's ``@server`` tool refs name nothing a session gets.

One defect class has shipped three times, on three different harnesses, and each
time it was diagnosed from scratch by someone who did not know it had happened
before. ``providers/mirrors/README.md`` names the shape: a session comes up
holding ``tools: ["@kirocrew-core", ...]`` while nothing in its effective
``mcpServers`` defines ``kirocrew-core`` -- refs naming nothing, every Crew tool
silently absent, the harness otherwise working and no error anywhere. KAS hit it,
then claude-agent-acp, then codex, which is in that state on a plain build today.

A mirror is the FIX for one backend. This is the DETECTOR, and it is worth being
exact about its reach rather than claiming the tidier thing: the resolver is
provider-neutral, but the runtime call site is ``AcpClient``'s ``session/new`` /
``session/load`` composition, so it covers kiro-cli, claude and codex. **KAS runs
on ``AcpRuntime``, which composes its array elsewhere and never reaches this
detector** -- so a KAS session's refs are checked only by ``kirocrew doctor``, and
wiring that second transport is a separate change. Saying "all of them" here would
be the same kind of unexamined claim the mirrors folder exists to stop.

**It lives in the SDK rather than in the ACP layer because the question is not an
ACP question.** Given a spec, the server array a session is about to receive and
a backend id, "which refs resolve to nothing" is answerable from plain data --
which is what lets ``kirocrew doctor`` ask it without importing the backend at
all (the agent-sdk-boundary gate refuses a consumer a new ACP edge). Nothing here
imports :mod:`kiro_crew.acp`; the ACP layer's own logging wrapper
(:mod:`kiro_crew.acp.mcp_ref_guard`) and the runtime call sites import THIS.

**Who satisfies a ref depends on the backend, and there are exactly two
answers.** kiro-cli is handed ``--agent`` and reads the spec itself, so for it a
ref is satisfied by the spec's OWN ``mcpServers`` definition -- Crew passes that
backend an empty array by design, and reading its refs against the wire would
report every single one as unresolved on the healthiest install there is. Every
other harness reads no agent file, so the wire array is the whole MCP surface of
the session and the only thing that can satisfy a ref. A session-injected broker
stub satisfies a ref on either backend, because it arrives on the wire under the
same name as the entry it wraps.

**Two ref spellings are not server refs and must not be reported.** A bare tool
name (``fs_read``, ``execute_bash``) carries no ``@`` and names a built-in.
``@builtin`` carries one but addresses kiro's built-in namespace rather than a
server (kiro's own configuration reference documents it beside ``@server``), so
reporting it would put a permanent false warning on every spec that uses it.
``*`` grants every server that IS defined; it defines none, so it neither
satisfies nor produces a ref.

Nothing here raises. Both inputs are shapes a hand-editable JSON file and a wire
payload can hold, and the callers are session-establishment paths where an
exception would cost the session over a diagnostic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

#: The ``tools`` entry that grants every DEFINED MCP server. It defines none, so
#: it is neither a ref nor a satisfier here. Only the bare ``*``: this repo's own
#: readers parse ``@*`` as a server LITERALLY named ``*`` (see
#: ``connections.tool_aliases._parse_tool_refs`` and
#: ``acp.kas_permissions._mcp_pattern``), and a ref to a server named ``*``
#: resolves to nothing, which is the truth.
_GRANT_ALL = "*"

#: Marks an MCP server (or one of its tools) in a ``tools`` entry.
_MCP_PREFIX = "@"

#: ``@`` names that address a kiro namespace rather than an MCP server. kiro's
#: configuration reference lists ``@builtin`` ("all built-in only") alongside
#: ``@server`` and ``@server/tool``, so a spec written to that reference is
#: correct and must not be warned about.
RESERVED_TOOL_NAMESPACES = frozenset({"builtin"})


def parse_tools_refs(tools: Any) -> tuple[bool, list[str]]:
    """Split a spec ``tools`` list into ``(grants_every_server, server names)``.

    The ONE reader of the ``tools`` ref vocabulary for MCP-server questions, so
    ``acp.session_mcp._tools_grant`` and this module cannot drift into
    disagreeing about what an entry names -- a detector reading ``@srv`` where
    the projection read nothing reports a ref as unresolved while the server
    mounts, and the reverse mounts a server the detector called absent. Both
    directions are the same defect. Names keep first-seen order and are
    de-duplicated, which makes a derived warning stable across two sessions on
    one spec, and the pass is LINEAR in the number of entries -- a session waits
    on this, and the spec it reads is only size-capped, not entry-capped.

    ``@server`` and ``@server/tool`` both name ``server``: whether the spec
    grants a whole server or one of its tools, the server has to exist either
    way. Entries that name no server -- a bare tool name, ``@`` alone,
    ``@/tool`` -- are skipped rather than reported, and ``@builtin`` is left IN:
    exclusions belong to the caller asking the question, and a server genuinely
    called ``builtin`` must still be mountable (see
    :func:`unresolved_server_refs`).
    """
    grant_all = False
    names: list[str] = []
    # A SET decides membership; the list only preserves order. Testing ``in`` against
    # the growing list instead would be a linear scan per entry, so a spec with N
    # distinct refs would cost O(N**2) -- and this runs synchronously on the
    # session-establishment path, off a file whose size cap (50 MB, ``hooks``)
    # permits millions of entries. The two structures cannot disagree: nothing adds
    # to one without the other.
    seen: set[str] = set()
    for item in tools if isinstance(tools, (list, tuple)) else ():
        if not isinstance(item, str):
            continue
        if item == _GRANT_ALL:
            grant_all = True
            continue
        if not item.startswith(_MCP_PREFIX):
            continue
        server = item[len(_MCP_PREFIX) :].partition("/")[0]
        if server and server not in seen:
            seen.add(server)
            names.append(server)
    return grant_all, names


def wire_server_names(servers: Any) -> list[str]:
    """Server names in a ``session/new`` / ``session/load`` ``mcpServers`` array.

    Accepts the wire shape directly (a list of dicts with a ``name``) and
    tolerates anything else by returning empty, so a caller can hand over
    whatever it sent without pre-validating it.

    Deliberately NOT
    :func:`kiro_crew.acp.mcp_session_report.roster_names`, which reads the same
    array for the dashboard: importing it would give this module an ACP edge and
    cost ``kirocrew doctor`` its boundary-clean route to the resolver. The two
    must still AGREE -- a detector warning that a server is missing while the
    panel lists it two rows down is worse than no detector -- so their agreement
    is pinned by a test instead of by a shared import. This one applies no cap:
    the report bounds a payload that reaches a browser, while a name dropped
    here would make a satisfied ref read as unresolved.
    """
    out: list[str] = []
    # Set-backed membership, ordered output -- see :func:`parse_tools_refs` for why
    # a linear ``in`` over the growing list is the wrong shape on a path a session
    # waits on. Uncapped here (unlike the report's reader) because a name dropped at
    # a cap would make a satisfied ref read as unresolved.
    seen: set[str] = set()
    for entry in servers if isinstance(servers, (list, tuple)) else ():
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _spec_server_names(spec: Any) -> set[str]:
    """Server names an agent spec DEFINES, whatever the projection later does."""
    servers = spec.get("mcpServers") if isinstance(spec, Mapping) else None
    if not isinstance(servers, Mapping):
        return set()
    return {str(name) for name in servers}


def unresolved_server_refs(spec: Any, wire_servers: Any, *, backend: str) -> list[str]:
    """The spec's ``@server`` refs that no server this session gets can satisfy.

    *spec* is the agent spec as read from disk (``tools``, ``mcpServers``, and
    the per-entry ``disabledTools`` inside them); *wire_servers* is the FINAL
    ``mcpServers`` array about to go out on ``session/new`` / ``session/load``,
    spec projection and broker stubs together; *backend* is the id the session
    runs on.

    Returned in the ``@name`` spelling the spec used, sorted, so two readings of
    one spec produce the same line. Empty is the healthy answer.

    ``disabledTools`` deliberately changes nothing here, and saying so is the
    point: it turns individual TOOLS off within a server that is still mounted,
    so it can narrow what a satisfied ref delivers but can never be what makes a
    ref name nothing. A server whose every tool is disabled is a separate
    question this function does not claim to answer.
    """
    _grant_all, refs = parse_tools_refs(spec.get("tools") if isinstance(spec, Mapping) else None)
    if not refs:
        return []
    satisfied = set(wire_server_names(wire_servers))
    if backend == ACP_BACKEND_KIRO:
        # kiro-cli resolves --agent and loads the spec's own servers, which is why
        # Crew passes it an empty array. Judging its refs against the wire alone
        # would report every ref on the healthiest install there is.
        satisfied |= _spec_server_names(spec)
    return sorted(
        f"{_MCP_PREFIX}{name}"
        for name in refs
        if name not in satisfied and name not in RESERVED_TOOL_NAMESPACES
    )
