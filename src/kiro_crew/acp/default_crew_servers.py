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

ONE helper, called by every path that adds them, so they cannot drift:

* kiro-cli runs the agent's skill view, so the view preparation applies it to the
  authored spec before projecting
  (:func:`kiro_crew.acp.skill_projection_parts.preparation.prepare_native_skill_projection`);
  the per-session control-plane element then reads the view and carries identity
  as it does for ``kirocrew.json``.
* KAS projects the spec it resolved for the session, so
  :func:`kiro_crew.acp.harness.kas.resolve_projected_spec` applies it to a
  user-level spec before :func:`kiro_crew.acp.kas_agents.build_kas_custom_agents`.
* A mirrored harness in ``ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY`` (claude,
  codex) builds its ``session/new`` array from the spec the shared translation
  resolves, so that one read applies it to a user-level spec
  (:func:`with_default_crew_servers_in_array`) where KAS does: before a project
  checkout's switch-offs are layered on. Those mirrors get Crew's own two servers
  only, never an app's. The mirror carries identity as it does for
  ``kirocrew.json``.

Nothing is written to the user's file. The servers add no new identity or memory
path: their tools resolve the caller from the session's own key, the same way they
do on Crew's default agent, so memory tools reach the store that session is already
bound to.

A custom agent that is not a Crew Mode conductor also gets each edition server the
edition names for custom agents (``McpToolingProvider.custom_agent_servers``), the
entry every spec Crew writes carries, minus its ``autoApprove`` and under the app
servers' wildcard rule, loader or not. The spec a session on a given backend runs,
with whatever that backend's spawn adds, is :func:`session_spec_on`: the one answer
a read-only check judges a spec by, so the check and the spawns cannot disagree.

``kirocrew-work`` is deliberately not in the set: it serves only a session the
conductor bound to a ledger item and answers ``not_bound`` everywhere else.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, TypeVar

from kiro_crew.agent_spec_format import is_app_agent_launch_name, is_native_skill_alias_name
from kiro_crew.dashboard.side_readonly_spec import is_readonly_spec_description
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: The servers a custom agent gets by default, in mount order.
DEFAULT_CREW_SERVERS: tuple[str, ...] = ("kirocrew-core", "kirocrew-cron")

_T = TypeVar("_T")

#: What one pass over many agents has read of the host state they share
#: (:func:`one_pass`); ``None`` outside a pass, where every call reads it afresh.
_PASS: ContextVar[dict[str, Any] | None] = ContextVar("default_crew_servers_pass", default=None)


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


@contextmanager
def one_pass() -> Iterator[None]:
    """Within the block, read the host state every agent shares once, not once per agent.

    The edition's servers (``extra_mcp_servers()`` and ``mcp.json``) and kiro-cli's
    skill-view admission are the same for every agent a skill-view preparation or a
    doctor check walks. Outside a block each call reads them afresh, so nothing a
    pass read outlives it. A nested block shares the outer one's reads.
    """
    if _PASS.get() is not None:
        yield
        return
    token = _PASS.set({})
    try:
        yield
    finally:
        _PASS.reset(token)


def _once(key: str, read: Callable[[], _T]) -> _T:
    """*read*'s answer, read once per :func:`one_pass` and on every call outside one."""
    memo = _PASS.get()
    if memo is None:
        return read()
    if key not in memo:
        memo[key] = read()
    value: _T = memo[key]
    return value


def _skill_view_projects() -> bool:
    from kiro_crew.acp.skill_projection import next_spawn_projects_skill_view

    return next_spawn_projects_skill_view()


def session_spec_on(backend: str, agent: str, spec: dict[str, Any]) -> dict[str, Any]:
    """*agent*'s user-level *spec* as a spawn on *backend* hands it to the session.

    For a read-only check that judges a spec by what its session reaches, so a server
    a spawn adds is counted exactly where it is added. The one place that says where:

    * KAS, on every spawn: the template's approved additions
      (``template_mcp_additions.merge_at_spawn``), then :func:`with_default_crew_servers`,
      in the order :func:`kiro_crew.acp.harness.kas.resolve_projected_spec` applies;
    * kiro-cli: :func:`with_default_crew_servers`, which the skill view applies, but
      only while a spawn would project one
      (:func:`kiro_crew.acp.skill_projection.next_spawn_projects_skill_view`);
      otherwise the spawn falls back to the authored agent;
    * a mirror in ``ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY`` (claude, codex):
      :func:`with_default_crew_servers_in_array`, the copy its ``session/new`` array
      is built from: the template's approved additions, then only Crew's own two
      servers;
    * any other backend in ``ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN``: the
      template's approved additions alone;
    * any other backend: *spec* as written.

    Records no withheld approval (``audit=False``): that is a spawn's decision. Never
    raises: state that cannot be read judges *spec* as written, so a check never
    counts a server the session may lack. Blocking; the host state every agent shares
    is read once per :func:`one_pass`.
    """
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY,
        ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN,
    )

    try:
        if backend in ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY:
            return with_default_crew_servers_in_array(agent, spec) or spec
        if backend in ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN:
            from kiro_crew.template_mcp_additions import merge_at_spawn

            merged = merge_at_spawn(agent, spec)
            if backend == ACP_BACKEND_KAS:
                return with_default_crew_servers(agent, merged, audit=False)
            return merged
        if backend == ACP_BACKEND_KIRO and _once("skill_view", _skill_view_projects):
            return with_default_crew_servers(agent, spec, audit=False)
    except Exception:
        logger.debug(
            "the %r session spec for %r could not be resolved", backend, agent, exc_info=True
        )
    return spec


def opted_out(spec: dict[str, Any], server: str) -> bool:
    """Whether *spec* withholds *server* whole.

    Each of these says so: ``managedToolPolicy.excludeServers`` naming it;
    ``excludedTools`` naming ``@<server>``, ``@<server>/*``, or the wildcards ``@*``
    and ``@mcp`` (every MCP server) and ``*`` (every tool); or a ``permissions`` rule that denies
    the ``mcp`` (or ``all``) capability with no ``exclude`` carve-out, for every
    resource or for ``<server>/*``. A per-tool exclusion or deny
    (``@kirocrew-core/spawn_run``) is not an opt-out: the server mounts and the
    spec's own rule keeps that tool off, natively on kiro-cli and KAS. A host that
    applies neither ``excludedTools`` nor ``permissions`` asks
    :func:`narrowed_per_tool` instead.

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


def narrowed_per_tool(spec: dict[str, Any], server: str) -> bool:
    """Whether an ``excludedTools`` entry of *spec* may reach a tool of *server*.

    An MCP tool's id is ``@<server>/<tool>``, so an entry can match one only when
    its first character can match that ``@``: an ``@`` entry whose server part
    reaches *server* (``@<server>/<tool>``, ``@kirocrew-*/spawn_*``, ``@*``, the
    whole-server opt-outs too), or one that starts with a glob (``*``, ``?``,
    ``[``), which may cross the ``/``. A built-in name such as ``fs_write`` reaches
    none. A ``permissions`` deny rule for the ``mcp`` (or ``all``) capability
    counts the same way when it may reach a tool of *server*: no ``match`` (every
    resource), or a pattern whose server part matches it, with any tool part or
    ``exclude`` carve-out. A host that applies neither would run those tools, so
    it must not add the server.
    """
    if _permissions_reach_server(spec.get("permissions"), server):
        return True
    excluded = spec.get("excludedTools")
    if not isinstance(excluded, list):
        return False
    from kiro_crew.agent_sdk.mcp_refs import entry_reaches

    return any(
        isinstance(ref, str)
        and (
            ref[:1] in ("*", "?", "[")
            or (ref.startswith("@") and entry_reaches(ref.partition("/")[0], f"@{server}"))
        )
        for ref in excluded
    )


def _permissions_reach_server(permissions: object, server: str) -> bool:
    """Whether an authored ``permissions`` deny rule may reach any tool of *server*."""
    rules = permissions.get("rules") if isinstance(permissions, dict) else None
    if not isinstance(rules, list):
        return False
    from fnmatch import fnmatchcase

    for rule in rules:
        if not isinstance(rule, dict) or rule.get("effect") != "deny":
            continue
        if rule.get("capability") not in ("mcp", "all"):
            continue
        match = rule.get("match")
        if not match:
            return True
        if isinstance(match, list) and any(
            isinstance(p, str) and fnmatchcase(server, p.partition("/")[0]) for p in match
        ):
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
    ``@<app>:*``). An app's server is third-party code, and an edition server is one
    the author never chose, so neither is added while one is there, even beside an
    entry naming it: the wildcard could still run the server's other tools unasked.
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


def _edition_server_names() -> tuple[str, ...]:
    """The names ``McpToolingProvider.custom_agent_servers`` returns.

    Read through ``getattr``, so an edition provider that predates the method names
    none. No I/O of its own: the edition answers from its own state.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    def _read() -> tuple[str, ...]:
        reader = getattr(current_context().mcp_tooling, "custom_agent_servers", None)
        names = reader() if callable(reader) else ()
        return tuple(name for name in names if isinstance(name, str))

    names: tuple[str, ...] = safe_context_call(
        _read,
        fallback_factory=tuple,
        log_message="custom_agent_servers lookup failed; adding no edition server",
    )
    return names


def _seeded_edition_servers() -> dict[str, Any]:
    from kiro_crew.agent import seed_edition_servers

    seeded: dict[str, Any] = {}
    seed_edition_servers(seeded)
    return seeded


def _edition_servers(names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """The edition servers *names* a custom agent gets, minus ``autoApprove``.

    Each kept only when ``extra_mcp_servers()`` supplies it on this host, as
    :func:`kiro_crew.agent.seed_edition_servers` builds
    it for every spec Crew writes, so the env the user set on it applies here too.
    The caller filters :func:`_edition_server_names` first, so a spec that wants
    none costs no ``extra_mcp_servers()`` call and no ``mcp.json`` read, and within
    a :func:`one_pass` they are read once for every agent. Each entry is a copy, so
    an edit to one agent's spec never reaches another's.
    """
    if not names:
        return {}
    seeded = _once("edition", _seeded_edition_servers)
    return {
        name: copy_json_document(
            {field: value for field, value in seeded[name].items() if field != "autoApprove"}
        )
        for name in names
        if isinstance(seeded.get(name), dict)
    }


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
    agent: str | None,
    spec: dict[str, Any],
    *,
    audit: bool = True,
    per_tool_limits_applied: bool = True,
) -> dict[str, Any]:
    """*spec* with :data:`DEFAULT_CREW_SERVERS` added when *agent* is a custom agent.

    A spec that grants the ``tool_search`` loader also gets :func:`_app_servers`,
    and any spec gets :func:`_edition_servers`, under the same rules, unless *agent*
    is a Crew Mode conductor: a conductor mounts only Crew's own servers
    (``crew_mode.conductor_agents``). An app or edition server a wildcard of the
    spec's own ``allowedTools`` would approve is not added (:func:`_wildcard_approves`).

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

    *per_tool_limits_applied* false is a host that mounts a server Crew adds with
    every tool, whatever the spec narrows: a default server :func:`narrowed_per_tool`
    names is not added, and no app or edition server is. An app's server is
    third-party code and an edition server one the author never chose, so no limit
    the spec sets on either may be lost, and codex runs a tool it marks
    ``readOnlyHint`` without asking.
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
    if not per_tool_limits_applied:
        added = {k: e for k, e in added.items() if not narrowed_per_tool(spec, k)}
    elif spec_grants_tool_search(spec) and agent not in conductor_agents():
        added.update(
            (key, entry)
            for key, entry in _app_servers().items()
            if wanted(key) and not _wildcard_approves(spec, key)
        )
    if per_tool_limits_applied and agent not in conductor_agents():
        # Filtered before any entry is built, so a spec that wants none reads nothing.
        names = (
            n for n in _edition_server_names() if wanted(n) and not _wildcard_approves(spec, n)
        )
        added.update(_edition_servers(tuple(names)))
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


def with_default_crew_servers_in_array(
    agent: str, spec: dict[str, Any] | None
) -> dict[str, Any] | None:
    """User-level *spec* as a mirror in ``ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY`` reads it.

    Called by the read that resolves a mirrored session's spec
    (``session_mcp._agent_spec_and_snapshot_for``), on the user-level spec only, so
    a spec only a project checkout declares runs as written, as on KAS. The
    template's approved additions merge first, as KAS merges them; the array read's
    own merge after this one then finds each declared and adds nothing. These
    mirrors mount a server whole and apply no ``excludedTools``, so only Crew's own
    two servers are added, each only while no ``excludedTools`` entry may reach it.
    They grant no ``allowedTools`` approval, so a withheld approval is not
    recorded: the record would name an approval this host never grants. Blocking.
    """
    if not isinstance(spec, dict):
        return spec
    from kiro_crew.template_mcp_additions import merge_at_spawn

    spec = merge_at_spawn(agent, spec)
    return with_default_crew_servers(agent, spec, audit=False, per_tool_limits_applied=False)
