"""Agent-spec ``@server`` tool refs that name nothing the session will have.

One defect class has shipped three times, on three different harnesses, and each
time it was diagnosed from scratch by someone who did not know it had happened
before. `providers/mirrors/README.md` names the shape: a session comes up holding
``tools: ["@kirocrew-core", ...]`` while nothing in its effective ``mcpServers``
defines ``kirocrew-core`` -- refs naming nothing, every Crew tool silently absent,
the harness otherwise working and no error anywhere. KAS hit it, then
claude-agent-acp, then codex, which is in that state on a plain build today
(``AcpClient._codex_session_mcp_servers`` returns ``[]``).

A mirror is the FIX for one backend. This module is the DETECTOR for all of them,
so the fourth occurrence cannot be silent: it compares what the spec asks for
against what the session is actually about to be handed, and the answer is a log
line plus a row on the session's MCP report.

**It never changes the array and never fails the session.** A ref naming nothing
is a configuration fact, not a reason to deny someone their session -- and the
whole complaint about this defect class was that it was invisible, not that it was
tolerated. Visible and survivable is the fix.

**Who satisfies a ref depends on the backend, and there are exactly two answers.**
kiro-cli is handed ``--agent`` and reads the spec itself, so for it a ref is
satisfied by the spec's OWN ``mcpServers`` definition -- Crew passes that backend
an empty array by design, and reading its refs against the wire would report every
single one as unresolved. Every other harness reads no agent file, so the wire
array is the whole MCP surface of the session and the only thing that can satisfy a
ref. A session-injected broker stub satisfies a ref on either backend, because it
arrives on the wire under the same name as the entry it wraps.

**Two ref spellings are not server refs and must not be reported.** A bare tool
name (``fs_read``, ``execute_bash``) carries no ``@`` and names a built-in.
``@builtin`` carries one but addresses kiro's built-in namespace rather than a
server (kiro's own configuration reference documents it beside ``@server``), so
reporting it would put a permanent false warning on every spec that uses it.
``*`` grants every server that IS defined; it defines none, so it neither
satisfies nor produces a ref.

Its answer is advisory, which decides one detail deliberately: the wire roster is
read through :func:`~kiro_crew.acp.mcp_session_report.roster_names`, the same
reader the session report renders from. Judging against a different reading of the
same array is how the dashboard ends up warning that a server is missing while
listing it as present two rows down.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.acp.mcp_session_report import roster_names
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

logger = logging.getLogger(__name__)

#: The ``tools`` entry that grants every DEFINED MCP server. It defines none, so
#: it is neither a ref nor a satisfier here. Only the bare ``*``: this repo's own
#: readers parse ``@*`` as a server LITERALLY named ``*`` (see
#: ``connections.tool_aliases._parse_tool_refs`` and
#: ``kas_permissions._mcp_pattern``), and a ref to a server named ``*`` resolves
#: to nothing, which is the truth.
_GRANT_ALL = "*"

#: Marks an MCP server (or one of its tools) in a ``tools`` entry.
_MCP_PREFIX = "@"

#: ``@`` names that address a kiro namespace rather than an MCP server. kiro's
#: configuration reference lists ``@builtin`` ("all built-in only") alongside
#: ``@server`` and ``@server/tool``, so a spec written to that reference is
#: correct and must not be warned about.
RESERVED_TOOL_NAMESPACES = frozenset({"builtin"})

#: Cap on how many refs one warning names. A spec is hand-editable and its
#: ``tools`` list is unbounded; a log line and a dashboard payload are not.
_REPORT_CAP = 32


def parse_tools_refs(tools: Any) -> tuple[bool, list[str]]:
    """Split a spec ``tools`` list into ``(grants_every_server, server names)``.

    The ONE reader of the ``tools`` ref vocabulary for MCP-server questions, so
    ``session_mcp._tools_grant`` and this module cannot drift into disagreeing
    about what an entry names. Names keep first-seen order and are de-duplicated,
    which makes a derived warning stable across two sessions on one spec.

    ``@server`` and ``@server/tool`` both name ``server``: whether the spec grants
    a whole server or one of its tools, the server has to exist either way.
    Entries that name no server -- a bare tool name, ``@`` alone, ``@/tool`` --
    are skipped rather than reported, and ``@builtin`` is left IN: exclusions
    belong to the caller asking the question, and a server genuinely called
    ``builtin`` must still be mountable (see :func:`unresolved_server_refs`).

    Never raises. The spec is hand-editable JSON, so a non-list ``tools`` or a
    non-string entry is ordinary input here, not an error.
    """
    grant_all = False
    names: list[str] = []
    for item in tools if isinstance(tools, (list, tuple)) else ():
        if not isinstance(item, str):
            continue
        if item == _GRANT_ALL:
            grant_all = True
            continue
        if not item.startswith(_MCP_PREFIX):
            continue
        server = item[len(_MCP_PREFIX) :].partition("/")[0]
        if server and server not in names:
            names.append(server)
    return grant_all, names


def _spec_server_names(spec: Any) -> set[str]:
    """Server names an agent spec DEFINES, whatever the projection later does."""
    servers = spec.get("mcpServers") if isinstance(spec, Mapping) else None
    if not isinstance(servers, Mapping):
        return set()
    return {str(name) for name in servers}


def unresolved_server_refs(
    spec: Any,
    wire_servers: Any,
    *,
    backend: str,
) -> list[str]:
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

    Never raises: a malformed spec or a wire array of an unexpected shape yields
    no finding rather than an exception on a session-establishment path.
    """
    _grant_all, refs = parse_tools_refs(spec.get("tools") if isinstance(spec, Mapping) else None)
    if not refs:
        return []
    satisfied = set(roster_names(wire_servers))
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


def warn_unresolved_server_refs(
    spec: Any,
    wire_servers: Any,
    *,
    backend: str,
    agent: str,
    gateway_enabled: bool,
) -> list[str]:
    """Evaluate the guard and log ONE structured line when it finds something.

    Returns the refs so a caller can also record them where a user will see them;
    logging here rather than at the call site is what keeps the wording identical
    across ``session/new`` and the ``session/load`` that resumes the same session.

    ``gateway_enabled`` rides along because it decides the remedy rather than the
    finding: with the shared MCP gateway on, a wrapped server arrives as a broker
    stub and the fix may be to route it, while with the gateway off the only
    channel is the projection. Reading the log line without knowing which world it
    came from is what made this defect take three diagnoses.
    """
    unresolved = unresolved_server_refs(spec, wire_servers, backend=backend)
    if not unresolved:
        return []
    shown = unresolved[:_REPORT_CAP]
    logger.warning(
        "agent-spec tool refs name no MCP server this session receives: "
        "backend=%r agent=%r unresolved=%s mcp_gateway=%s. Those tools are absent "
        "from the session with nothing else to say so -- the harness still works. "
        "A backend that reads no agent file needs a mirror "
        "(src/kiro_crew/providers/mirrors/) to project the spec onto its "
        "session/new mcpServers array.",
        backend,
        agent,
        ", ".join(shown)
        + (f" (+{len(unresolved) - len(shown)} more)" if len(shown) < len(unresolved) else ""),
        "on" if gateway_enabled else "off",
    )
    return unresolved
