"""A crewmate worker session's spec on the mirrored hosts' session MCP projection.

Split out of :mod:`kiro_crew.acp.session_mcp`, which imports it back under the
same name: a crewmate's worker runs a spec built per session from its crewmate's
file, and this is where the array-backed hosts read it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _crewmate_worker_spec(
    agent: str | None,
    spec: dict[str, Any] | None,
    session_key: str,
    *,
    tool_limits_honoured: bool = False,
    spec_policy_honoured: bool = False,
    mcp_tool_limits_honoured: bool = False,
    native_tool_limits_honoured: bool = False,
) -> dict[str, Any] | None:
    """*spec*, or for a crewmate's worker session the worker spec it runs on.

    The on-disk ``kirocrew-mate-worker`` is the base every crewmate's worker shares;
    the session's own worker is that base plus its crewmate's file
    (``mate_worker.worker_session_spec``, read from the session's execution record,
    the seam KAS's projection reads too). Every later read below is of THIS spec, so
    the array, the withhold set and the per-tool restrictions are the worker's own.
    Any other agent, or no session key, keeps *spec*. A worker session whose base
    is absent or unreadable raises :class:`~kiro_crew.mate_worker.WorkerKitUnreadable`
    (fail closed, as KAS's projection does) rather than run without its kit. On a
    host that does not honour single-tool limits (*tool_limits_honoured* False, the
    default), a crewmate file that sets any refuses with
    ``capability_tool_limits_unsupported``, judged on the read this spec is built from;
    one that honours the MCP half (*mcp_tool_limits_honoured*) refuses only a limit
    no MCP deny carries (``mate_worker.uncarried_tool_limits_of``), and one that also
    honours native-tool excludes (*native_tool_limits_honoured*) refuses only a native
    entry its built-in table does not cover.
    On a host that applies no ``hooks``, ``permissions`` or ``toolsSettings``
    (*spec_policy_honoured* False, the default), a crewmate file that sets any
    refuses the worker, naming them.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, WorkerKitUnreadable, worker_session_spec

    if agent != MATE_WORKER_AGENT or not session_key:
        return spec
    if not isinstance(spec, dict):
        raise WorkerKitUnreadable(
            f"worker session {session_key}: the worker base spec {MATE_WORKER_AGENT!r} is "
            f"missing or unreadable, so the worker would run without its kit"
        )
    return worker_session_spec(
        dict(spec),
        session_key,
        refuse_tool_limits=not tool_limits_honoured,
        refuse_spec_policy=not spec_policy_honoured,
        mcp_tool_limits_carried=mcp_tool_limits_honoured,
        native_tool_limits_carried=native_tool_limits_honoured,
    )


def crewmate_worker_deny_rules(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    session_key: str,
    backend: str,
) -> list[str] | None:
    """The ``permissions.deny`` rules a crewmate worker's claude settings carry.

    ``None`` for any session but a crewmate's worker, which keeps
    :func:`~kiro_crew.acp.session_mcp.session_mcp_deny_rules`, the spec read by name.
    A worker's limits live in the spec built for its session, not in the base on
    disk, so its rules come from :func:`~kiro_crew.acp.session_mcp.session_mcp_projection`
    keyed by *session_key*, the read the session's array is built from (see
    :func:`worker_deny_rules`). A worker whose limits *backend* cannot carry is
    refused there (``capability_tool_limits_unsupported``), and an unreadable kit
    raises :class:`~kiro_crew.mate_worker.WorkerKitUnreadable`, so the seed never
    writes a worker's settings without its limits. Blocking.
    """
    from kiro_crew.acp import session_mcp
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS,
        ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS,
        ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
    )
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    if agent != MATE_WORKER_AGENT or not session_key:
        return None
    projection = session_mcp.session_mcp_projection(
        agent,
        work_dir=work_dir,
        session_key=session_key,
        worker_tool_limits_honoured=backend in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
        worker_mcp_tool_limits_honoured=backend in ACP_BACKENDS_MATE_WORKER_MCP_TOOL_LIMITS,
        worker_native_tool_limits_honoured=backend in ACP_BACKENDS_MATE_WORKER_NATIVE_TOOL_LIMITS,
    )
    return worker_deny_rules(projection.disabled_tools, projection.agent_spec)


def worker_deny_rules(
    disabled_tools: frozenset[tuple[str, str]], spec: dict[str, Any] | None
) -> list[str]:
    """Claude deny rules for a worker spec's MCP limits, sorted and de-duplicated.

    Each ``(server, tool)`` its entries and the global settings switch off
    (*disabled_tools*), plus each ``@server/tool`` in its ``excludedTools`` (where
    the merge writes the crewmate file's limits and the dashboard's switches,
    ``mate_worker.merge_worker_spec``) and each whole ``@server``. Spelled as Claude
    Code names the tool (``claude_mcp_names``). A native or wildcard entry is not a
    rule: the session's ``tools`` list withholds a native tool
    (``claude_worker_tools.claude_worker_builtin_tools``), and a worker whose crewmate
    file sets any other is refused before this is read.
    """
    from kiro_crew.acp.claude_mcp_names import claude_mcp_server_rule, claude_mcp_tool_name
    from kiro_crew.mate_worker import uncarried_tool_limits_of

    rules = {claude_mcp_tool_name(server, tool) for server, tool in disabled_tools}
    excluded = spec.get("excludedTools") if isinstance(spec, dict) else None
    uncarried = set(uncarried_tool_limits_of(spec)) if isinstance(spec, dict) else set()
    for raw in excluded if isinstance(excluded, list) else []:
        # Trimmed exactly as ``uncarried_tool_limits_of`` trims it, so an entry it
        # counts as carried is always the rule written here.
        ref = raw.strip() if isinstance(raw, str) else ""
        if not ref.startswith("@") or ref in uncarried:
            continue
        server, _, tool = (part.strip() for part in ref[1:].partition("/"))
        rules.add(claude_mcp_tool_name(server, tool) if tool else claude_mcp_server_rule(server))
    return sorted(rules)


def worker_array_outruns_seed(in_force: Any, spec: dict[str, Any] | None) -> bool:
    """Whether a worker's array spec *spec* needs a deny rule its settings lack.

    The seed and the array each read the worker's spec, one after the other, so a
    crewmate file narrowed between the two reads, or a re-seed that was declined,
    would mount a server whose deny is not in what the session ships. *in_force* is
    those settings as Claude Code will read them (the inline settings, or the
    ``settings.local.json`` on disk); every rule *spec* asks for must be in its
    ``permissions.deny``. A wider spec needs nothing new and passes.
    """
    from kiro_crew.acp.session_mcp import session_mcp_disabled_tools

    if not isinstance(spec, dict):
        return False
    perms = in_force.get("permissions") if isinstance(in_force, dict) else None
    deny = perms.get("deny") if isinstance(perms, dict) else None
    shipped = {rule for rule in deny if isinstance(rule, str)} if isinstance(deny, list) else set()
    wanted = worker_deny_rules(session_mcp_disabled_tools(None, spec=spec), spec)
    return not set(wanted) <= shipped
