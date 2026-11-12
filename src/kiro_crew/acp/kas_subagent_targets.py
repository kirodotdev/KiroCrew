"""Register the agents a KAS session's sub-agent tool may name.

KAS's ``invoke_sub_agent`` (kiro-cli's ``use_subagent``) looks the name it is
given up in the session's agent registry: the ``customAgents`` batch plus the
on-disk profiles the engine keeps. The engine keeps no profile written for
kiro-cli (``allowedTools`` or ``toolsSettings`` and no ``permissions`` block), so
an agent the parent's ``toolsSettings.subagent.availableAgents`` names is found
only when the batch carries it. Each such agent is projected here through the
same translation as the session's own agent and appended to the batch, and the
session-start notice names every one that is not.

A target whose limits Kiro Crew enforces only at its own gate is not registered.
The gate applies the shell ``denyByDefault`` allowlist, the write
``fallbackAction: "deny"`` allowlist, the ``deniedCommands`` and write
``deniedPaths`` lists a deny record (``CREW_SPEC_DENIES_KEY``) carries, and the
spec's own ``preToolUse`` hooks only to the agent the session runs: a native
sub-agent's request reaches the gate with none of them, so the target would run
without its own limits.

Three more are not registered. A derived agent (``kirocrew-worker``,
``kirocrew-heartbeat``), because the check that its spec did not change while
the session started runs only on the snapshot of the agent the session runs. A
target that declares a Kiro Crew server the session's own agent does not,
because only the session's own agent's Crew servers are launched with this
session's identity (``kas_agents.hoist_servers_for_session``), so the target's
copy would refuse every call as ``identity_unattested``. And a target that
declares a pooled server (a name the session-level array carries as a broker
stub) other than as the session's own agent declares it: the stub launches the
session's own agent's declaration, and the projection drops the target's
declaration of that name, so the target's calls to its own server would reach
another one. A Crew server both declare, and a pooled server both declare
alike, are left out of the target's entry: the session-level array carries the
session's copy to every agent of the batch. That copy reaches the target whatever
its own declaration says, so a server the target's spec switches off
(``disabled``) is excluded whole from its entry (``@server`` in its
``excludedTools``) and is not counted as one it declares.

A registered target runs natively inside its session agent's turn, so the hooks
that turn fires meet its permission requests: the shared ones and the session
agent's own spec ``preToolUse`` hooks (``turn_spec_hooks._fire``). Its entry
withholds auto-approval for both, and the staleness check reads every entry of
the batch (:func:`registered_unprompted`), so a hook added later re-projects a
session whose target auto-approves what it covers. Its servers get the
session's ssh-agent verdict, as the session's own agent's do.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp.kas_permissions import (
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_SPEC_DENIES_KEY,
    CREW_WRITE_ALLOWLIST_KEY,
    _agent_glob,
    unprompted_capabilities,
    withhold_hook_gated_auto_approval,
)
from kiro_crew.acp.kas_prompt import prompt_unread_notice
from kiro_crew.acp.kas_tool_limits import with_switched_off_tools
from kiro_crew.agent_files import is_mate_service_agent, kas_wire_agent_id
from kiro_crew.platform.tool_names import KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME

logger = logging.getLogger(__name__)

#: The KAS id of the sub-agent tool, kiro-cli's ``use_subagent``.
KAS_SUBAGENT_TOOL = KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME["use_subagent"][0]

#: What a KAS ``tools`` or ``excludedTools`` entry selects the sub-agent tool by:
#: its id and the two tags KAS 0.66.33 declares it under (``tags: [BUILTIN,
#: SUBAGENT_META]``). The engine's matcher takes an entry equal to one of these, or
#: a ``*`` or ``?`` pattern that matches one, so ``@builtin`` mounts it.
_SUBAGENT_TOOL_NAMES = (KAS_SUBAGENT_TOOL, "@builtin", "@subagent")

#: How many targets one session registers. Each is a whole projection at session
#: start (a spec read, the governance checks, its servers' launch files), so a
#: ``*`` pattern over a large agents folder would slow every session start. The
#: rest are counted in the notice.
MAX_SUBAGENT_TARGETS = 8

_NOT_FOUND = "no agent of that name was found"
_NO_MATCH = "it matches no agent"
_UNUSABLE = "its agent file could not be used"
_RESERVED = "the backend keeps that name for one of its own modes"
_SERVICE = "it is one of Kiro Crew's own agents"
_GATE_ONLY = (
    "Kiro Crew applies its shell or file-write limits and its preToolUse hooks "
    "only to the agent a chat runs"
)
_UNATTESTED = "it uses {servers}, which this chat's own agent does not load"
_SHARED = (
    "it sets up its own {servers}, and every agent in this chat gets the copy this "
    "chat's own agent loads"
)

#: The :data:`CREW_SPEC_DENIES_KEY` fields the gate refuses a call by
#: (``spec_denied_gate.spec_denies_refusal``). Its ``web_fetch`` fields only grant.
_DENY_RECORD_LIMITS = ("deniedCommands", "deniedPaths")

#: Server-entry keys that never change which process a server launches or which
#: endpoint it reaches. ``disabledTools`` is carried in the target's own
#: ``excludedTools`` whichever copy it reaches, and ``disabled`` as the whole
#: ``@server`` there (:func:`_excluding_switched_off`), so the stub still serves
#: the session's own agent and never the target that switched it off.
#: ``autoApprove`` never crosses the wire, and ``timeout`` and its ``timeoutMs``
#: alias are a start budget (``kas_settings_mcp._with_timeout``). ``poolable`` is a
#: hint the pooling rewriter reads, not a launch input (``rewriter_launch.py``).
#: Every other key counts, so a key not named here refuses rather than shares.
_NOT_LAUNCH_KEYS = frozenset(
    {"autoApprove", "disabled", "disabledTools", "poolable", "timeout", "timeoutMs"}
)

#: The transport a command entry launches when it names none.
_DEFAULT_TRANSPORT = "stdio"


def _selects_subagent_tool(entry: Any) -> bool:
    """Whether one KAS tool-list *entry* selects the sub-agent tool, as the engine reads it.

    An entry equal to a :data:`_SUBAGENT_TOOL_NAMES` member, or a pattern that
    matches one, where ``*`` is any run, ``?`` any one character and every other
    character itself.
    """
    if not isinstance(entry, str):
        return False
    # ``[`` opens a character set to fnmatch and is itself to the engine.
    pattern = entry.replace("[", "[[]")
    return any(fnmatch.fnmatchcase(name, pattern) for name in _SUBAGENT_TOOL_NAMES)


def mounts_subagent_tool(entry: Any) -> bool:
    """Whether the projected *entry* mounts KAS's sub-agent tool and keeps it.

    Read as the engine filters an agent's tools: ``"*"`` mounts every tool, a list
    mounts what one of its entries selects, and ``excludedTools`` takes away what
    one of its entries selects (:func:`_selects_subagent_tool`).
    """
    if not isinstance(entry, dict):
        return False
    excluded = entry.get("excludedTools")
    if isinstance(excluded, list) and any(_selects_subagent_tool(t) for t in excluded):
        return False
    tools = entry.get("tools")
    if tools == "*":
        return True
    return isinstance(tools, list) and any(_selects_subagent_tool(t) for t in tools)


def project_subagent_targets(
    parent: str,
    parent_spec: dict[str, Any],
    custom_agents: list[dict[str, Any]],
    work_dir: str | Path | None,
    *,
    stub_server_names: frozenset[str],
    session_key: str,
    withheld_out: kas_agents_mod.WithheldOut,
    engine_features: frozenset[str],
    settings_dir: str | Path | None = None,
    forward_ssh_auth_sock: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """The entries to append to *parent*'s batch, and the notice for what is left out.

    Nothing when *parent*'s entry does not mount the sub-agent tool, or its spec
    declares no ``availableAgents`` (omitted, the engine's own roster answers, as
    before). Blocking file I/O: runs in the projection's worker thread. A target
    that fails to project costs that target and a notice line, never the session.
    *settings_dir* is the directory whose ``mcp.json`` the parent's entry was
    stamped from, *work_dir* when ``None``. *forward_ssh_auth_sock* is the
    verdict the session's own entry was built with
    (``kas_agents._project_mcp_servers``).
    """
    from kiro_crew.mcp_gateway.rewriter_spec import _named_in_tools
    from kiro_crew.subagent import spawn_allowlist

    entry = next((a for a in custom_agents if a.get("id") == parent), None)
    if not mounts_subagent_tool(entry):
        return [], ""
    globs = spawn_allowlist(parent_spec)
    if not globs:
        # ``None`` is "allow all" and ``()`` is "spawn nothing": no list to register.
        return [], ""
    names, missed = _target_names(parent, globs, work_dir)
    own_servers = entry.get("mcpServers") if isinstance(entry, dict) else None
    parent_servers = frozenset(own_servers) if isinstance(own_servers, dict) else frozenset()
    declared = parent_spec.get("mcpServers")
    # A pooled stub launches only a declaration the spec's ``tools`` names; one it
    # leaves out gets a stub that starts nothing, or none.
    parent_declared = (
        _named_in_tools(declared, parent_spec.get("tools")) if isinstance(declared, dict) else {}
    )
    overflow = max(0, len(names) - MAX_SUBAGENT_TARGETS)
    entries: list[dict[str, Any]] = []
    notices: list[str] = []
    session_hooks = _session_hook_matchers(parent, parent_spec) if names else ()
    for name in names[:MAX_SUBAGENT_TARGETS]:
        try:
            projected = _project_target(
                name,
                work_dir,
                stub_server_names=stub_server_names,
                session_key=session_key,
                withheld_out=withheld_out,
                engine_features=engine_features,
                notices=notices,
                settings_dir=settings_dir or work_dir,
                parent_servers=parent_servers,
                parent_declared=parent_declared,
                session_hooks=session_hooks,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
            )
        except Exception:  # noqa: BLE001 - a target never costs the session
            logger.warning("sub-agent %r could not be registered", name, exc_info=True)
            projected = _UNUSABLE
        if isinstance(projected, str):
            missed[name] = projected
        else:
            entries.append(projected)
    return entries, "\n\n".join(n for n in (*notices, _notice(parent, missed, overflow)) if n)


def _target_names(
    parent: str, globs: tuple[str, ...], work_dir: str | Path | None
) -> tuple[list[str], dict[str, str]]:
    """The agent ids *globs* name, in order, and each pattern that names none.

    A pattern KAS cannot read alike is skipped: the ``subagent`` rule
    (``kas_permissions._available_agents_rules``) already denies what it would
    name, and the translation notice names it. A ``*`` pattern is matched with
    kiro-cli's case-sensitive ``fnmatch`` against the agents the spawn gate offers.
    """
    names: list[str] = []
    missed: dict[str, str] = {}
    roster: list[str] | None = None
    # The parent's own wire id is the parent, never a second entry.
    own = {parent, kas_wire_agent_id(parent)}
    for raw in globs:
        glob = _agent_glob(raw.strip())
        if glob is None:
            continue
        if "*" not in glob:
            found = [glob]
        else:
            if roster is None:
                roster = _glob_roster(work_dir)
            found = [n for n in roster if fnmatch.fnmatchcase(n, glob)]
            if not found:
                missed[glob] = _NO_MATCH
        for name in found:
            if name not in own and name not in names:
                names.append(name)
    return names, missed


def _glob_roster(work_dir: str | Path | None) -> list[str]:
    """The agent names a ``*`` pattern is matched against, sorted.

    The spawn gate's set (``subagent._validate_agent``): the user-level agents and
    the checkout's, less Kiro Crew's generated specs and the agents reached by
    omitting a name. A crewmate's own folder is not listed, as no roster lists it.
    """
    from kiro_crew.agent_discovery import is_internal_agent_spec, list_agents
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.subagent import UNADVERTISED_AGENTS

    try:
        agents = list_agents(paths_mod.kiro_agents_dir(), project_dir=work_dir or None)
    except Exception:  # noqa: BLE001 - a pattern then matches nothing and is named
        logger.warning("agents folder unreadable for sub-agent patterns", exc_info=True)
        return []
    return sorted(
        {
            a.name
            for a in agents
            if a.name not in UNADVERTISED_AGENTS and not is_internal_agent_spec(a)
        }
    )


def _project_target(
    name: str,
    work_dir: str | Path | None,
    *,
    stub_server_names: frozenset[str],
    session_key: str,
    withheld_out: kas_agents_mod.WithheldOut,
    engine_features: frozenset[str],
    notices: list[str],
    settings_dir: str | Path | None,
    parent_servers: frozenset[str],
    parent_declared: dict[str, Any],
    session_hooks: tuple[str, ...],
    forward_ssh_auth_sock: bool,
) -> dict[str, Any] | str:
    """*name*'s projected entry, or the reason it is not registered.

    The spec comes through the gates the session's own agent passes (fork
    governance, nearest-first resolution with a checkout's spec trusted for
    nothing), and the entry from the same projection, stamped with the same
    ``mcp.json`` settings read (:func:`stamp_settings_mcp`). A derived agent is
    not registered, whatever its freshness gate answers. *parent_servers* names
    the servers the session's own entry declares: a Kiro Crew server outside them
    refuses the target, and one inside them is left out of its entry.
    *parent_declared* is the session's own spec's ``mcpServers`` its ``tools`` names,
    which the pooled stubs launch: a pooled server the target declares otherwise refuses it
    (:func:`_pooled_elsewhere`). *session_hooks* are the session agent's own spec
    PreToolUse matchers, whose coverage the entry withholds too. A server the spec
    switches off is excluded whole (:func:`_excluding_switched_off`).
    *withheld_out* and *notices* gain this target's lines only once it registers.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp.harness import kas as kas_harness
    from kiro_crew.acp.kas_mcp_json_legacy import legacy_mcp_json
    from kiro_crew.acp.kas_settings_mcp import include_mcp_json, stamp_settings_mcp
    from kiro_crew.acp.session_mcp import (
        session_mcp_disabled_servers,
        session_mcp_disabled_tools,
    )
    from kiro_crew.config import paths as paths_mod

    own_notices: list[str] = []
    if kas_wire_agent_id(name) != name:
        # Registered under another id, which the parent's ``subagent`` rule denies.
        return _RESERVED
    if is_mate_service_agent(name):
        return _SERVICE
    agents_dir = paths_mod.kiro_agents_dir()
    try:
        agent_mod.require_fork_governance(name, work_dir)
        if agent_mod.require_fresh_derived_spec(name, work_dir) is not None:
            # Derived from the default spec: its verified snapshot is re-checked once
            # the host has loaded the payload only for the agent the session runs
            # (``runtime_mode_activation._activate_mode_bracketed``), so a change to
            # the default spec landing while the session starts would leave this
            # copy registered.
            return _SERVICE
        spec, prompt_dir = kas_harness.resolve_projected_spec(
            agents_dir,
            name,
            work_dir,
            own_notices,
            project_hooks=kas_harness.session_projects_folder_hooks(session_key),
        )
    except kas_agents_mod.KasAgentSpecAbsent:
        return _NOT_FOUND
    except (
        agent_mod.ForkGovernanceUnresolved,
        agent_mod.DerivedSpecStale,
        kas_agents_mod.KasAgentTranslationError,
    ) as exc:
        logger.warning("sub-agent %r is not registered for this session: %s", name, exc)
        return _UNUSABLE
    if _spec_pre_tool_matchers(name, spec):
        return _GATE_ONLY
    # Decided before the projection, so a refused target writes no launch files.
    muted = session_mcp_disabled_servers(spec, None)
    elsewhere = _pooled_elsewhere(spec, parent_declared, stub_server_names, muted)
    if elsewhere:
        return _SHARED.format(servers=", ".join(elsewhere))
    scope_rules, skill_copies = kas_harness._skill_scope(name, spec, prompt_dir, work_dir)
    projected = spec
    if skill_copies:
        declared = spec.get("resources")
        kept = declared if isinstance(declared, list) else []
        projected = {**spec, "resources": [*kept, *skill_copies]}
    withheld: kas_agents_mod.WithheldOut = {}
    unread: list[Path] = []
    [entry] = kas_agents_mod.build_kas_custom_agents(
        prompt_dir,
        name,
        projected,
        stub_server_names=stub_server_names,
        session_key=session_key,
        skill_scope_rules=scope_rules,
        withheld_out=withheld,
        prompt_unread_out=unread,
        engine_features=engine_features,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
    )
    if _gate_only_limits(entry):
        # The launch files this projection wrote belong to no registered entry.
        kas_env_launch.discard_batch([entry])
        return _GATE_ONLY
    crew = _crew_servers(entry)
    unattested = [n for n in crew if n not in parent_servers]
    if unattested:
        kas_env_launch.discard_batch([entry])
        return _UNATTESTED.format(servers=", ".join(unattested))
    [entry] = with_switched_off_tools([entry], session_mcp_disabled_tools(name, spec=spec))
    entry = _excluding_switched_off(entry, muted)
    # The wire envelope finishes this entry from it as it does the session's own:
    # the mcp.json servers it does not declare excluded unless it opts in, the glob
    # ``disabledTools`` of the ones it reaches, and kiro-cli's start budget. The
    # session's own stamp already reported an unreadable file. A switched-off
    # server is not one it declares, so a pooled stub of the file's server that no
    # agent of the batch loads is not sent (``without_excluded_stubs``).
    declared = spec.get("mcpServers")
    [entry] = stamp_settings_mcp(
        [entry],
        name,
        settings_dir,
        declared=[n for n in declared if n not in muted] if isinstance(declared, dict) else (),
        legacy_mcp_json=legacy_mcp_json(name, agents_dir, spec) and prompt_dir == agents_dir,
        # From the spec: the projected entry never carries a ``true``.
        opted_in=include_mcp_json(spec) is True,
    )
    entry = _withhold_session_hooks(entry, name, session_hooks)
    withheld_out.update(withheld)
    notices.extend([*own_notices, prompt_unread_notice(name, unread)])
    return _without_servers(entry, crew)


def _gate_only_limits(entry: dict[str, Any]) -> bool:
    """Whether projected *entry* carries a limit only Crew's gate applies.

    The gate applies them only to the agent the session runs:
    ``session_handle_permissions._build_permission_event`` hands a native
    sub-agent's request none of the session's ``spec_*`` fields.
    """
    if CREW_SHELL_ALLOWLIST_KEY in entry or CREW_WRITE_ALLOWLIST_KEY in entry:
        return True
    record = entry.get(CREW_SPEC_DENIES_KEY)
    return isinstance(record, dict) and any(record.get(k) for k in _DENY_RECORD_LIMITS)


def _spec_pre_tool_matchers(agent_id: str, spec: dict[str, Any]) -> tuple[str, ...]:
    """The matchers of *spec*'s own PreToolUse hooks that run, read as
    ``kas_agents.pre_tool_hook_matchers`` reads that source."""
    from kiro_crew.agent_sdk.spec_hooks import spec_script_hooks
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

    return tuple(
        h.matcher
        for h in spec_script_hooks(agent_id, spec)
        if h.enabled and h.event == HOOK_EVENT_PRE_TOOL_USE and h.command.strip()
    )


def _session_hook_matchers(parent: str, parent_spec: dict[str, Any]) -> tuple[str, ...]:
    """*parent*'s own spec PreToolUse matchers, or ``("*",)`` when they cannot be read."""
    try:
        return _spec_pre_tool_matchers(parent, parent_spec)
    except Exception:  # noqa: BLE001 - fail toward gating, as pre_tool_hook_matchers does
        logger.warning(
            "spec hooks of %r could not be listed; its sub-agents auto-approve nothing",
            parent,
            exc_info=True,
        )
        return ("*",)


def _withhold_session_hooks(
    entry: dict[str, Any], name: str, matchers: tuple[str, ...]
) -> dict[str, Any]:
    """Target *entry* with an ``ask`` for what the session agent's own hooks cover.

    The turn fires those hooks on every permission request it raises, a native
    sub-agent's included (``turn_spec_hooks._fire``), but the target's own
    projection met only the shared hooks and its own spec's. An auto-approved call
    raises no request, so without the ask the hook would never see it.
    """
    if not matchers:
        return entry
    held = entry.get("permissions")
    permissions = withhold_hook_gated_auto_approval(
        held,
        matchers,
        audit_decision=lambda refs, outcome, reason: kas_agents_mod._audit_permission_decision(
            refs, outcome, reason, name
        ),
        agent_id=name,
    )
    if permissions is held or not permissions:
        return entry
    return {**entry, "permissions": {**(held if isinstance(held, dict) else {}), **permissions}}


def registered_unprompted(active: Any, custom_agents: Any) -> list[Any]:
    """What a live session lets KAS decide without Crew, one answer per agent.

    *active* is the answer recorded for the agent the session runs
    (``kas_auto_approved``), then each entry's of the registered batch
    (``unprompted_capabilities``), each once and none empty. Never their union:
    an ``mcp-held:`` pattern, or the servers an entry's ``mcp`` grant is confined
    to, says nothing about another entry's grant. KAS keeps the whole batch for
    the session's life, and a registered target's calls run under its own entry
    inside the session agent's turn, so the staleness check
    (``spec_hooks.hook_projection_stale``) meets each answer with that turn's hooks.
    """
    entries = custom_agents if isinstance(custom_agents, list) else []
    parts: list[Any] = []
    for part in (
        active,
        *(unprompted_capabilities(a.get("permissions")) for a in entries if isinstance(a, dict)),
    ):
        if part and part not in parts:
            parts.append(part)
    return parts


def _crew_servers(entry: dict[str, Any]) -> list[str]:
    """The Kiro Crew servers projected *entry* declares, sorted.

    Crew's managed servers and the builtin app servers that run Crew's own code,
    whose every call needs this session's identity. The runtime launches them with
    it only from the session's own entry, hoisted into the session-level array
    (``kas_agents.hoist_servers_for_session``); a copy left in a target's entry is
    launched without it.
    """
    declared = entry.get("mcpServers")
    if not isinstance(declared, dict):
        return []
    return _crew_names(n for n in declared if isinstance(n, str))


def _crew_names(names: Iterable[str]) -> list[str]:
    """The Kiro Crew servers among *names*, sorted (:func:`_crew_servers`)."""
    crew: list[str] = []
    rest: list[str] = []
    for n in names:
        (crew if n in kas_agents_mod.MANAGED_MCP_SERVER_NAMES else rest).append(n)
    if rest:
        from kiro_crew.acp.session_mcp import builtin_app_identity_launches

        apps = builtin_app_identity_launches()
        crew.extend(n for n in rest if n in apps)
    return sorted(crew)


def _pooled_elsewhere(
    spec: dict[str, Any],
    parent_declared: dict[str, Any],
    stub_server_names: frozenset[str],
    muted: frozenset[str] = frozenset(),
) -> list[str]:
    """The pooled servers target *spec* declares other than the session's agent does, sorted.

    A name in *stub_server_names* reaches every agent of the batch as one
    session-level broker stub, which launches the session's own agent's
    declaration of it (``mcp_gateway.session_servers.pooled_session_servers``), and
    the projection drops a target's declaration of that name by name
    (``kas_agents._project_mcp_servers``). A target whose declaration launches
    something else (another command, argument, environment, working directory or
    endpoint) would reach the stub under its own server's name. So would one the
    session's own spec does not declare at all, a stub the global ``mcp.json``
    supplies: nothing here can show that stub launches the target's declaration.
    A Kiro Crew server keeps the treatment :func:`_crew_servers` gives it. A name in
    *muted* (the target's spec switches that server off whole) is not compared: it
    never launches, and :func:`_excluding_switched_off` keeps the session's copy
    out of the target's reach.
    """
    declared = spec.get("mcpServers")
    if not stub_server_names or not isinstance(declared, dict):
        return []
    pooled = [
        n
        for n, e in declared.items()
        if isinstance(n, str) and n in stub_server_names and n not in muted and isinstance(e, dict)
    ]
    crew = set(_crew_names(pooled))
    return sorted(
        n
        for n in pooled
        if n not in crew and _launch(declared[n]) != _launch(parent_declared.get(n))
    )


def _launch(entry: Any) -> dict[str, Any] | None:
    """*entry*'s keys that decide what its server launches, an empty value as unset.

    An explicit ``type: "stdio"`` is the transport an entry gets when it names none,
    so it reads as unset; any other transport stays a difference.
    """
    if not isinstance(entry, dict):
        return None
    return {
        k: v
        for k, v in entry.items()
        if k not in _NOT_LAUNCH_KEYS
        and v not in (None, "", [], {})
        and not (k == "type" and v == _DEFAULT_TRANSPORT)
    }


def _excluding_switched_off(entry: dict[str, Any], muted: frozenset[str]) -> dict[str, Any]:
    """Target *entry* with ``@server`` in its ``excludedTools`` for each *muted* server.

    *muted* is what the target's spec switches off whole
    (``session_mcp.session_mcp_disabled_servers``, the shared launch predicate, so
    a non-boolean ``disabled`` mutes too). The projection drops each such
    declaration (``kas_agents._project_mcp_servers``), which keeps the target's own
    copy from starting but not a server of the name that reaches it anyway: a
    pooled stub or a Crew server the session-level array carries for the session's
    own agent, or an ``mcp.json`` server. Crew's servers and KAS's always-started
    ones are not exempt, as they are in ``kas_settings_mcp.excluded_whole``: that
    spares servers an agent never named, and the target named these and switched
    them off. Its existing exclusions keep their order.
    """
    if not muted:
        return entry
    current = entry.get("excludedTools")
    kept = current if isinstance(current, list) else []
    refs = [f"@{n}" for n in sorted(muted)]
    return {**entry, "excludedTools": list(dict.fromkeys([*kept, *refs]))}


def _without_servers(entry: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """*entry* without the *names* servers, whose launch files are removed.

    Each is a Kiro Crew server the session's own entry declares too. The session's
    copy reaches every agent of the batch from the session-level array, as the
    stub of a pooled server both declare alike does, and a second declaration of
    the name would be an unattested launch of it. ``tools`` and ``excludedTools``
    refs stay: one resolves wherever its server is declared.
    """
    declared = entry.get("mcpServers")
    if not names or not isinstance(declared, dict):
        return entry
    dropped = {n: e for n, e in declared.items() if n in names}
    kas_env_launch.discard_batch([{"mcpServers": dropped}])
    out = {k: v for k, v in entry.items() if k != "mcpServers"}
    kept = {n: e for n, e in declared.items() if n not in names}
    if kept:
        out["mcpServers"] = kept
    return out


def _notice(parent: str, missed: dict[str, str], overflow: int) -> str:
    """The session-start line naming the targets the sub-agent tool cannot reach."""
    parts = [f"{name} ({reason})" for name, reason in missed.items()]
    if overflow:
        parts.append(
            f"{overflow} more agents (only the first {MAX_SUBAGENT_TARGETS} it names are tried)"
        )
    if not parts:
        return ""
    return (
        f"ℹ In this chat, agent {parent} cannot hand work to {'; '.join(parts)}. "
        "Choose that agent for a chat of its own to use it."
    )
