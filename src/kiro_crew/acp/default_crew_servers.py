"""Crew's own servers a user's custom agent gets by default.

A custom agent is a user-level spec Kiro Crew did not write: one the user authored
or a tool such as AIM installed. Its spec grants what it declares, and the user has
no practical way to declare Crew's servers in it: their launch command, data-home
pin and session identity are Crew-internal. So a Crew session on such an agent gets
:data:`DEFAULT_CREW_SERVERS` added to the spec it runs on, with the managed launch
(:func:`kiro_crew.agent.managed_mcp_spec_entry`) and an ``@<server>`` grant.

ONE helper, called by both kiro paths, so the two cannot drift:

* kiro-cli runs the agent's skill view, so the view preparation applies it to the
  authored spec before projecting
  (:func:`kiro_crew.acp.skill_projection_parts.preparation.prepare_native_skill_projection`);
  the per-session control-plane element then reads the view and carries identity
  as it does for ``kirocrew.json``.
* KAS projects the spec it resolved for the session, so
  :func:`kiro_crew.acp.harness.kas.resolve_projected_spec` applies it to a
  user-level spec before :func:`kiro_crew.acp.kas_agents.build_kas_custom_agents`.

Nothing is written to the user's file. The servers add no new identity or memory
path: their tools resolve the caller from the session's own key, the same way they
do on Crew's default agent, so memory tools reach the store that session is already
bound to.

``kirocrew-work`` is deliberately not in the set: it serves only a session the
conductor bound to a ledger item and answers ``not_bound`` everywhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.agent_spec_format import is_app_agent_launch_name, is_native_skill_alias_name
from kiro_crew.dashboard.side_readonly_spec import is_readonly_spec_description
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: The servers a custom agent gets by default, in mount order.
DEFAULT_CREW_SERVERS: tuple[str, ...] = ("kirocrew-core", "kirocrew-cron")


def is_custom_agent(agent: str | None, spec: object) -> bool:
    """Whether *agent* is a user-level spec Kiro Crew did not write.

    Not custom: every spec Crew writes (``agent_files.OWNED_KIRO_AGENT_FILES``), the
    crewmate service agents, an app's registered agent (its containment is the app
    framework's), and Crew's derived copies (skill views, app launch copies and the
    side chat's read-only spec). Saved capability choices are authoritative;
    unreadable capability state adds nothing. Blocking file I/O for these lookups.
    """
    if not agent or not isinstance(spec, dict):
        return False
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES, is_mate_service_agent

    if f"{agent}.json" in OWNED_KIRO_AGENT_FILES or is_mate_service_agent(agent):
        return False
    if is_native_skill_alias_name(agent) or is_app_agent_launch_name(agent):
        return False
    if is_readonly_spec_description(spec.get("description")):
        return False
    from kiro_crew import agent_state

    try:
        if agent_state.get_capabilities(agent) is not None:
            return False
    except (OSError, ValueError):
        return False
    from kiro_crew.apps.manager import owning_app_for_agent

    return owning_app_for_agent(agent) is None


def opted_out(spec: dict[str, Any], server: str) -> bool:
    """Whether *spec* withholds *server* whole.

    Two existing spellings say so: ``managedToolPolicy.excludeServers`` naming it, or
    ``excludedTools`` naming ``@<server>`` or ``@<server>/*``. A per-tool exclusion
    (``@kirocrew-core/spawn_run``) is not an opt-out: the server mounts and the
    spec's own ``excludedTools`` keeps that tool off, natively on both backends.
    """
    policy = spec.get("managedToolPolicy")
    if isinstance(policy, dict):
        servers = policy.get("excludeServers")
        if isinstance(servers, list) and server in servers:
            return True
    excluded = spec.get("excludedTools")
    if isinstance(excluded, list):
        if f"@{server}" in excluded or f"@{server}/*" in excluded:
            return True
    return False


def _registry_mode() -> bool:
    """Whether the install is registry-governed, as the session mount reads it.

    Under an admin's MCP catalog the host decides which servers run, and KAS drops
    Crew's own (``kas_agents._project_mcp_servers`` says why), so a default added
    there would only hold the session at its readiness wait. The same answer
    :func:`kiro_crew.acp.session_mcp.kiro_control_plane_servers` mounts nothing on.
    """
    from kiro_crew.acp.session_mcp import _registry_mode as registry_mode

    return registry_mode()


def _grants_all(tools: list[Any]) -> bool:
    return "*" in tools


def _references(tools: list[Any], server: str) -> bool:
    """Whether *tools* already names *server*, whole or per tool.

    A per-tool grant (``@kirocrew-core/skill_search``) is the author narrowing the
    server, so the whole-server ref is not added beside it.
    """
    whole, prefix = f"@{server}", f"@{server}/"
    return any(isinstance(t, str) and (t == whole or t.startswith(prefix)) for t in tools)


def with_default_crew_servers(agent: str | None, spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* with :data:`DEFAULT_CREW_SERVERS` added when *agent* is a custom agent.

    Returns *spec* itself when nothing is added, else a new dict. Nothing is added
    on a registry-governed install (:func:`_registry_mode`). For each server:

    * skipped when the spec opts out (:func:`opted_out`);
    * skipped when the spec already declares it in ``mcpServers``, in any shape: the
      author's entry (a mute, a ``disabledTools`` list) is theirs and stays as it is;
    * skipped when its managed entry cannot be resolved (a closed gate);
    * skipped when ``tools`` is absent, malformed or an empty list: an agent that
      grants no tools at all stays tool-less.

    Otherwise the managed entry joins ``mcpServers`` and ``@<server>`` joins
    ``tools``, unless ``tools`` already grants everything or already names the
    server (a per-tool grant keeps its narrowing).
    """
    if not is_custom_agent(agent, spec) or _registry_mode():
        return spec
    tools = spec.get("tools")
    if tools == "*":
        tool_list: list[Any] | None = None
    elif isinstance(tools, list) and tools:
        tool_list = None if _grants_all(tools) else list(tools)
    else:
        return spec
    declared = spec.get("mcpServers")
    if declared is not None and not isinstance(declared, dict):
        return spec
    from kiro_crew.agent import managed_mcp_spec_entry

    added: dict[str, dict[str, Any]] = {}
    for server in DEFAULT_CREW_SERVERS:
        if opted_out(spec, server) or (declared and server in declared):
            continue
        entry = managed_mcp_spec_entry(server)
        if isinstance(entry, dict):
            added[server] = entry
    if not added:
        return spec
    out = copy_json_document(spec)
    out["mcpServers"] = {**(declared or {}), **added}
    if tool_list is not None:
        refs = [f"@{name}" for name in added if not _references(tool_list, name)]
        out["tools"] = [*tool_list, *refs]
    allowed = out.get("allowedTools")
    if isinstance(allowed, list):
        from kiro_crew.agent_sdk.mcp_refs import entry_reaches
        from kiro_crew.platform.governance import may_skip_gate_now

        gated = [f"@{server}" for server in added if not may_skip_gate_now(f"@{server}")]

        def reaches_gated(ref: object) -> bool:
            # Only entries that reach a server added here are judged: a per-tool
            # ceiling withholds the server grant too, so a covering wildcard must
            # not auto-approve the newly mounted tools. Every other entry is the
            # author's and stays as written; the backends' own filters judge it.
            if not isinstance(ref, str):
                return False
            return any(
                ref == "*" or (ref.startswith("@") and entry_reaches(ref.partition("/")[0], server))
                for server in gated
            )

        withheld = [ref for ref in allowed if reaches_gated(ref)]
        out["allowedTools"] = [ref for ref in allowed if not reaches_gated(ref)]
        if withheld:
            from kiro_crew.acp.kas_agents import _audit_permission_decision

            _audit_permission_decision(
                ", ".join(sorted(withheld)), "withheld", "governance ceiling", agent or ""
            )
    logger.debug("agent %r: adding Crew's default servers %s", agent, ", ".join(added))
    return out
