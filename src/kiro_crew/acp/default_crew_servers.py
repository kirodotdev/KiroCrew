"""Crew's own servers a user's custom agent gets by default.

A custom agent is a user-level spec Kiro Crew did not write: one the user authored
or a tool such as AIM installed. Its spec grants what it declares, and the user has
no practical way to declare Crew's servers in it: their launch command, data-home
pin and session identity are Crew-internal. So a Crew session on such an agent gets
:data:`DEFAULT_CREW_SERVERS` added to the spec it runs on, with the managed launch
(:func:`kiro_crew.agent.managed_mcp_spec_entry`) and an ``@<server>`` grant. One
that grants the ``tool_search`` loader, and is not a Crew Mode conductor, also gets
every enabled installed app's servers keyed ``<app>:<server>``, the entries Crew's
default agent mounts, without their ``autoApprove``, except one a wildcard of its
own ``allowedTools`` would approve unasked.

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


def crew_added_servers(agent: str | None, spec: object) -> tuple[str, ...]:
    """The :data:`DEFAULT_CREW_SERVERS` Crew added to *spec*, the spec a session runs.

    A server counts only when *agent* is a custom agent and the spec's entry for it
    is exactly the managed launch :func:`with_default_crew_servers` adds. An entry
    the author wrote (a mute, a ``disabledTools`` list, any other shape) is theirs
    and is not counted. Blocking file I/O, as :func:`is_custom_agent`.
    """
    if not isinstance(spec, dict) or not is_custom_agent(agent, spec):
        return ()
    declared = spec.get("mcpServers")
    if not isinstance(declared, dict):
        return ()
    from kiro_crew.agent import managed_mcp_spec_entry

    return tuple(
        server
        for server in DEFAULT_CREW_SERVERS
        if server in declared and declared[server] == managed_mcp_spec_entry(server)
    )


def opted_out(spec: dict[str, Any], server: str) -> bool:
    """Whether *spec* withholds *server* whole.

    Each of these says so: ``managedToolPolicy.excludeServers`` naming it;
    ``excludedTools`` naming ``@<server>``, ``@<server>/*``, or the wildcards ``@*``
    and ``@mcp`` (every MCP server) and ``*`` (every tool); or a ``permissions`` rule that denies
    the ``mcp`` (or ``all``) capability with no ``exclude`` carve-out, for every
    resource or for ``<server>/*``. A per-tool exclusion or deny
    (``@kirocrew-core/spawn_run``) is not an opt-out: the server mounts and the
    spec's own rule keeps that tool off.

    The ``permissions`` block is read as written, before KAS's projection parses
    it: a block that projection refuses whole still says the author withholds the
    server, so the server is not added.
    """
    policy = spec.get("managedToolPolicy")
    if isinstance(policy, dict):
        servers = policy.get("excludeServers")
        if isinstance(servers, list) and server in servers:
            return True
    excluded = spec.get("excludedTools")
    if isinstance(excluded, list):
        # ``@mcp`` is KAS's own name for every MCP tool, which a v3 agent file may use.
        if any(e in excluded for e in ("@*", "@mcp", "*", f"@{server}", f"@{server}/*")):
            return True
    return _permissions_deny_server(spec.get("permissions"), server)


def _permissions_deny_server(permissions: object, server: str) -> bool:
    """Whether an authored ``permissions`` block denies every tool of *server*.

    A rule counts when its effect is ``deny``, its capability is ``mcp`` or ``all``,
    it has no non-empty ``exclude``, and its ``match`` is absent or empty (KAS reads
    both as every resource) or holds a pattern whose server part matches *server* and
    whose tool part is absent or only ``*``.
    """
    rules = permissions.get("rules") if isinstance(permissions, dict) else None
    if not isinstance(rules, list):
        return False
    from fnmatch import fnmatchcase

    def covers(pattern: object) -> bool:
        # Whole server: the server part matches it and the tool part is only `*`s
        # (absent counts too), so a one-tool pattern such as `<server>/?` does not.
        if not isinstance(pattern, str):
            return False
        server_part, _, tool_part = pattern.partition("/")
        return fnmatchcase(server, server_part) and not tool_part.strip("*")

    for rule in rules:
        if not isinstance(rule, dict) or rule.get("effect") != "deny" or rule.get("exclude"):
            continue
        if rule.get("capability") not in ("mcp", "all"):
            continue
        match = rule.get("match")
        if not match:
            return True
        if isinstance(match, list) and any(covers(p) for p in match):
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


def _wildcard_approves(spec: dict[str, Any], server: str) -> bool:
    """Whether a wildcard in *spec*'s own ``allowedTools`` would run *server* unasked.

    An entry that reaches ``@<server>`` without naming it (``"*"``, ``@*``,
    ``@<app>:*``). An app's server is third-party code, so it is not added while
    one is there, even beside an entry naming it: the wildcard could still run
    the server's other tools unasked.
    """
    allowed = spec.get("allowedTools")
    if not isinstance(allowed, list):
        return False
    from kiro_crew.agent_sdk.mcp_refs import entry_reaches

    return any(
        isinstance(ref, str)
        and (ref == "*" or ref.startswith("@"))
        and not _references([ref], server)
        and entry_reaches(ref.partition("/")[0], f"@{server}")
        for ref in allowed
    )


def _app_servers() -> dict[str, dict[str, Any]]:
    """Each enabled installed app's server as ``kirocrew.json`` mounts it, minus ``autoApprove``.

    Only an unaliased ``<app>:<server>`` key: a server whose declared name has a
    ``/`` is keyed by an alias, and an alias can be another server's.
    """
    from kiro_crew import agent as agent_mod

    mounted = (agent_mod._installed_default_spec() or {}).get("mcpServers")
    if not isinstance(mounted, dict):
        return {}
    owned = agent_mod._app_owned_mcp_keys()
    return {
        key: {field: value for field, value in entry.items() if field != "autoApprove"}
        for key, entry in mounted.items()
        if ":" in key and owned.get(key) and isinstance(entry, dict)
    }


def with_default_crew_servers(
    agent: str | None, spec: dict[str, Any], *, audit: bool = True
) -> dict[str, Any]:
    """*spec* with :data:`DEFAULT_CREW_SERVERS` added when *agent* is a custom agent.

    A spec that grants the ``tool_search`` loader also gets :func:`_app_servers`,
    under the same rules, unless *agent* is a Crew Mode conductor: a conductor
    mounts only Crew's own servers (``crew_mode.conductor_agents``). An app server
    a wildcard of the spec's own ``allowedTools`` would approve is not added
    (:func:`_wildcard_approves`).

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

    *audit* false skips the security-event record of a withheld approval, for a
    read-only caller that must judge the spec as a spawn would.
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
    from kiro_crew.agent_sdk.tool_search import spec_grants_tool_search
    from kiro_crew.crew_mode import conductor_agents

    def wanted(server: str) -> bool:
        return not opted_out(spec, server) and not (declared and server in declared)

    added: dict[str, dict[str, Any]] = {}
    for server in DEFAULT_CREW_SERVERS:
        entry = managed_mcp_spec_entry(server) if wanted(server) else None
        if isinstance(entry, dict):
            added[server] = entry
    if spec_grants_tool_search(spec) and agent not in conductor_agents():
        added.update(
            (key, entry)
            for key, entry in _app_servers().items()
            if wanted(key) and not _wildcard_approves(spec, key)
        )
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
        if withheld and audit:
            from kiro_crew.acp.kas_agents import _audit_permission_decision

            _audit_permission_decision(
                ", ".join(sorted(withheld)), "withheld", "governance ceiling", agent or ""
            )
    logger.debug("agent %r: adding Crew's default servers %s", agent, ", ".join(added))
    return out
