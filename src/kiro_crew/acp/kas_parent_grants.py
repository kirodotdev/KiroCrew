"""With the KAS sub-agents preview on, a KAS sub-agent run is approved as its founding chat is.

kiro-cli runs a sub-agent under the grants of the session that started the chain and
gives the sub-agent's own grants no effect; every ``ask`` and ``deny`` of every agent
on the chain, the sub-agent's own included, still holds. Here the same holds for a KAS
run started with ``spawn_run`` / ``spawn_sub_agents`` while the preview is on
(:func:`preview_on`), in the two steps ``KasHarness.session_extras`` calls:

* :func:`founding_grants_spec` gives the run's spec the founding agent's permission
  half (``allowedTools``, ``toolsSettings``, ``permissions`` and each server's
  ``autoApprove``) in place of its own grants, with the chain's ``ask`` rules and the
  run's own ``ask`` and ``deny`` rules appended. A tool the run's own file restricts
  keeps the run's own ``toolsSettings`` entry. The session's one projection then
  applies every rule it has to that spec, so the run's own limits refuse and
  withhold exactly as they do today. A founder's deny lists are not copied: the
  record ``kas_parent_limits.hold_to_parents`` carries refuses by them, under the
  founder's name, with the preview on as with it off.
* :func:`limit_to_founders` then keeps, of what the run's entry runs unasked, only
  what each founding agent's own session runs unasked, so a grant passes the run's
  governance ceiling and the founding chat's.

A Bot's worker keeps its own spec and is limited the same way, so it runs unasked what
both its own file and the founding agent grant. Three holds, all decided in
:func:`_half`, keep a run from running unasked what its founding chat is asked for or
refused: a grant that names an MCP server reaches the run only under
:func:`server_grant_reaches` (by name, as on kiro-cli: the run's own server of that
name answers), a grant of commands or paths only in the founding
chat's own directory, and a founding agent with a PreToolUse hook of its own hands
down a grant only when the run's own spec carries that same hook, so it runs on the
run's calls too. Tools, servers, hooks, model and prompt stay the run's own. Preview
off, a run with no parent session and every session that is not a sub-agent run are
returned unchanged. A run whose founders all run its own agent keeps its own
permission half: it gains only the ``ask`` rules of the sessions above that its own
file lacks, and is unchanged when there are none. Whatever cannot be read, a session
between included (its agent has no file, or its record is gone), leaves the run with
no grant at all.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp.kas_parent_limits import (
    _MAX_LEVELS,
    CREW_PARENT_LIMITS_KEY,
    ParentLimitsUnreadable,
    _gate_read_denies,
    _parent_keys,
    _unresolved_dirs,
    parent_spec,
)
from kiro_crew.acp.kas_permissions import (
    _MAX_RULES,
    CREW_ANSWERED_BUILTINS_KEY,
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_ANSWERED_HOOK_HELD_KEY,
    CREW_ANSWERED_WRITES_KEY,
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_SPEC_DENIES_KEY,
    CREW_UNJUDGED_DENIES_KEY,
    CREW_WRITE_ALLOWLIST_KEY,
    allowed_tools_grant_shell,
    shell_denies_by_default,
)
from kiro_crew.acp.kas_spec_grants import (
    CREW_ANSWERED_PATHS_KEY,
    CREW_ANSWERED_READ_PATHS_KEY,
    CREW_SHELL_READONLY_KEY,
)

logger = logging.getLogger(__name__)

#: The spec keys that decide what runs unasked: the half a run takes from its founder.
_HALF_KEYS = ("allowedTools", "toolsSettings", "permissions")
#: Entry keys that list what Crew's gate answers by exact name.
_NAMED_GRANT_KEYS = (
    CREW_ANSWERED_BUILTINS_KEY,
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_ANSWERED_PATHS_KEY,
    CREW_ANSWERED_READ_PATHS_KEY,
)
#: Entry keys that list the ``@server/tool`` refs Crew's gate answers.
_REF_GRANT_KEYS = (CREW_ANSWERED_WRITES_KEY, CREW_ANSWERED_HOOK_HELD_KEY)
#: Entry keys that only limit, with the tool family each reads. On a run that took
#: its founder's half they are the founder's unless the run's own file restricts
#: that family, and the ``hold_to_parents`` record already holds the run to them.
_LIMIT_ONLY_KEYS = {
    CREW_SHELL_ALLOWLIST_KEY: "shell",
    CREW_SHELL_READONLY_KEY: "shell",
    CREW_WRITE_ALLOWLIST_KEY: "write",
    CREW_UNJUDGED_DENIES_KEY: "shell",
}
#: ``toolsSettings`` keys whose reach depends on the directory a session runs in: the
#: command and path grants, and the allowlist switches that read the same lists.
_DIRECTORY_KEYS = frozenset(
    {"allowedCommands", "autoAllowReadonly", "denyByDefault", "allowedPaths", "fallbackAction"}
)
#: Capabilities whose ``allow`` patterns name no command and no local path.
_ANYWHERE = frozenset({"mcp", "web_fetch", "web_search"})
#: ``toolsSettings`` keys that list what a spec refuses. A founder's are not copied
#: into the run's spec: the parent record the run carries refuses by them.
_DENY_LIST_KEYS = ("deniedCommands", "deniedPaths", "blocked")
#: The whole-tool grant that covers each kind of command or path grant.
_WHOLE_TOOL = {
    CREW_ANSWERED_COMMANDS_KEY: "execute_bash",
    CREW_ANSWERED_PATHS_KEY: "fs_write",
    CREW_ANSWERED_READ_PATHS_KEY: "fs_read",
}
#: ``ScriptHook`` fields that name a hook or record its last runs. Every other
#: field decides whether the hook runs or what it stops.
_HOOK_NAMING_FIELDS = frozenset(
    {"id", "name", "last_run", "last_status", "last_error", "run_count"}
)
#: What decides which process or endpoint an MCP server entry is.
_LAUNCH_KEYS = ("type", "command", "args", "env", "url", "headers")


@dataclasses.dataclass(frozen=True)
class Founding:
    """What :func:`founding_grants_spec` found, for the two calls after the projection.

    *applies* is False for a run the preview leaves alone. *founders* holds
    ``(agent, session key, spec, same)`` for each founding chat whose agent file was
    read, one per session and per spec that session may run, never one per agent
    name; *same* says that spec is the run's own. Empty with *applies* means no grant
    may be kept. *asks* are the ``ask`` rules of every session above the run. *swapped* says the run's spec took a founder's half.
    *cwd* is the run's directory. *own* names the tool families the run's own file
    restricts: their limits in its entry are its own. *kept* says the run keeps its
    own half, its founders all running its own spec, and its spec gained *asks*.
    """

    applies: bool = False
    founders: tuple[tuple[str, str, dict[str, Any], bool], ...] = ()
    asks: tuple[Any, ...] = ()
    swapped: bool = False
    cwd: str = ""
    own: frozenset[str] = frozenset()
    kept: bool = False


_UNCHANGED = Founding()


def preview_on() -> bool:
    """Whether the "KAS sub-agents" feature preview is on. Unreadable reads off.

    The preview's one reader lives beside the engine sub-agent switch
    (``kas_engine_subagents.kas_subagents_preview_on``); a build without it has no
    preview, which is off. It is handed the config the projection of the same
    session start hands it (``kas_shell_grant_lift``): the one the gateway last
    applied, else ``config.json``. So whether a run takes its founding chat's
    grants and whether its shell grant lifts ``denyByDefault`` are one reading.
    """
    try:
        from kiro_crew.acp import kas_engine_subagents as switch
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        reader = getattr(switch, "kas_subagents_preview_on", None)
        return reader is not None and reader(live.snapshot() or KiroCrewConfig.load()) is True
    except Exception:  # noqa: BLE001 - an unreadable config keeps today's approvals
        logger.warning("config unreadable; sub-agent runs keep their own grants", exc_info=True)
        return False


def server_grant_reaches(server: str, run: dict[str, Any]) -> bool:
    """Whether a founding agent's grant that names MCP *server* reaches the run.

    By name, as on kiro-cli: it reaches the server the run's own spec declares under
    that name, whichever program that is and whether or not the founding agent holds
    a server of that name, so the run's own server answers unasked. A name the run's
    spec declares no server for is not reached. The one place this rule lives.
    """
    return _server(run, server) is not None


def _held_alike(server: str, founder: dict[str, Any], run: dict[str, Any]) -> bool:
    """Whether both specs declare MCP *server* the same way.

    For what a founding agent grants without naming a server: ``*`` in
    ``allowedTools``, an ``mcp`` allow rule with no ``match``, and a server entry's
    ``autoApprove`` verbs, which approve tools of the founder's own program.
    """
    theirs, ours = _server(founder, server), _server(run, server)
    if theirs is None or ours is None:
        return False
    return all(theirs.get(key) == ours.get(key) for key in _LAUNCH_KEYS)


def _server(spec: dict[str, Any], name: str) -> dict[str, Any] | None:
    servers = spec.get("mcpServers")
    entry = servers.get(name) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _sessions_above(run_key: str) -> tuple[list[str] | None, list[str]]:
    """The chats that founded the chain above *run_key*, and every session above it.

    The founders are ``[]`` with no parent session and ``None`` when a link of the
    chain is missing. Each session is listed once under its own key: two sessions of
    one agent are two sessions here.
    """
    found: list[str] = []
    every: list[str] = []
    seen, level = {run_key}, [run_key]
    for _ in range(_MAX_LEVELS):
        above: list[str] = []
        for key in level:
            parents = _parent_keys(key)
            if not parents:
                return ([] if key == run_key else None), every
            for parent in (p for p in parents if p not in seen):
                seen.add(parent)
                every.append(parent)
                (above if parent.startswith("subagent:") else found).append(parent)
        if not above:
            return (found or None), every
        level = above
    return None, every


def _specs_of(agent: str, key: str, run_cwd: str = "") -> list[dict[str, Any]]:
    """Each spec the session on *key* may run *agent* from, as ``kas_parent_limits``
    reads a parent's: one per directory it may run in, the same one once. Empty when
    no agent file defines it.

    A session whose directory the resolver does not know (a sub-agent run started
    with no ``cwd``) is read where its session was noted as bound, when that is
    another directory than *run_cwd*, the run's, and then with no directory
    (``kas_parent_limits._unresolved_dirs``), so a rule either read finds applies.
    """
    from kiro_crew.subagent import parent_project_dirs

    specs: list[dict[str, Any]] = []
    for cwd in parent_project_dirs(key) or _unresolved_dirs(key, run_cwd):
        spec = parent_spec(agent, key, cwd)
        if spec is not None and spec not in specs:
            specs.append(spec)
    return specs


def _rules(spec: dict[str, Any], *effects: str) -> list[Any]:
    """The rules of *spec*'s own ``permissions`` block with one of *effects*; none
    when KAS would refuse the block whole."""
    from kiro_crew.agent_sdk.kas_tool_view import permissions_block_readable

    block = spec.get("permissions")
    if not isinstance(block, dict) or not permissions_block_readable(block):
        return []
    return [
        r for r in block.get("rules") or [] if isinstance(r, dict) and r.get("effect") in effects
    ]


def _reaching_rule(rule: Any, founder: dict[str, Any], run: dict[str, Any]) -> Any:
    """*rule* with an ``mcp`` allow narrowed to the servers whose grant reaches the run.

    A ``match`` entry names its server (:func:`server_grant_reaches`). A rule with no
    ``match`` names none: it covers the founder's own servers the run holds alike.
    """
    if rule.get("capability") != "mcp" or rule.get("effect") != "allow":
        return rule
    match, servers = rule.get("match"), founder.get("mcpServers")
    if isinstance(match, list):
        kept = [
            m for m in match if isinstance(m, str) and server_grant_reaches(m.split("/", 1)[0], run)
        ]
    else:
        kept = [
            f"{server}/*"
            for server in (servers if isinstance(servers, dict) else ())
            if _held_alike(server, founder, run)
        ]
    return {**rule, "match": kept} if kept else None


def _half(
    founding: tuple[str, str, dict[str, Any], bool],
    run: dict[str, Any],
    agent: str,
    asks: tuple[Any, ...],
    cwd: str,
    *,
    with_run: bool,
) -> dict[str, Any]:
    """The permission half one *founding* chat hands the run of *agent*, as spec keys.

    The founder's ``allowedTools`` with each server's ``autoApprove`` verbs, its
    ``toolsSettings`` and its ``permissions`` rules; then *asks* and the run's own
    ``ask`` and ``deny`` rules. Every hold on what a founder hands down is decided
    here:

    * an MCP grant that names its server reaches the run only where
      :func:`server_grant_reaches`. A server's ``autoApprove`` verbs, which name
      none, reach it only for a server both specs declare the same way. ``*`` and
      an ``mcp`` allow rule with no ``match`` are narrowed here to such servers
      too, and then hand the run no MCP grant at all: the founding agent's own
      session holds none for them, so :func:`limit_to_founders` removes it;
    * a grant of commands or paths reaches it only when the run works in the
      founding chat's one known directory (*cwd*): anywhere else it would name
      other files than it does for the founder;
    * a founding agent with a PreToolUse hook of its own hands down a grant only
      when that hook runs on the run's calls too (:func:`_hooks_run_on_the_run`)
      and the run works in the founding chat's one known directory (*cwd*): a hook
      runs where its session runs, and elsewhere the same entry may not find what
      it runs, which blocks nothing; otherwise a call it would stop must still ask
      here. The Hooks page's hooks run on both;
    * a founder hands down nothing when its own permission half, or the run's,
      cannot be read whole (:func:`_unreadable`): what that file asks for or
      refuses is then unknown.

    *with_run* makes it the half the run's spec takes: the run's own sub-agent
    settings stay, without ``trustedAgents``, and a tool the run's own file
    restricts keeps the run's own entry in place of the founder's. The session's
    projection then holds the founder's grant of that tool to the run's
    restrictions exactly as it holds the run's own grant today. The founder's
    ``deniedCommands``, ``deniedPaths`` and ``blocked`` lists stay out of that
    half: the parent record the run carries (``kas_parent_limits.hold_to_parents``)
    refuses by them and names the founder, and where it cannot decide a call it
    takes the run's grants off that call, so the run asks as the founding chat
    does. A ``denyByDefault`` of the run's own refuses as its own file
    makes it refuse: kiro-cli lifts it for an agent whose ``allowedTools`` names the
    shell, so whether the list names the shell stays the run's own too. Without
    *with_run* the half is the founder's alone, the one :func:`limit_to_founders`
    projects as the founder's own session.
    """
    from kiro_crew.acp.kas_auto_approve import auto_approve_refs
    from kiro_crew.acp.kas_grant_only_settings import without_grant_only_settings
    from kiro_crew.mate_held_rules import _capability_of_tool, _without_agent_grants
    from kiro_crew.platform.tool_names import expand_kiro_cli_allowed_tools
    from kiro_crew.platform.tools_settings_aliases import (
        _TOOLS_SETTINGS_CANONICAL,
        canonical_tools_settings,
    )

    name, key, founder, same = founding
    same_dir = _same_directory(key, cwd)
    if not same and (
        _unreadable(founder)
        or _unreadable(run)
        or not _hooks_run_on_the_run(name, founder, agent, run)
        # A hook runs in its session's directory: the same entry run elsewhere may
        # not find what it runs, and a hook that fails to start blocks nothing.
        or (not same_dir and _has_pre_tool_hooks(name, founder))
    ):
        founder = {}
    half: dict[str, Any] = {}
    listed = founder.get("allowedTools")
    refs = (
        expand_kiro_cli_allowed_tools(listed, founder.get("mcpServers"))
        if isinstance(listed, list)
        else None
    )
    auto = auto_approve_refs(founder, audit=False)
    if refs is not None or auto:
        # The entries as written: ``*`` and the ``autoApprove`` verbs name no server.
        named = expand_kiro_cli_allowed_tools(listed, None) if isinstance(listed, list) else []
        half["allowedTools"] = [
            ref
            for ref in dict.fromkeys([*(refs or []), *auto])
            if not ref.startswith("@")
            or (
                server_grant_reaches(ref[1:].split("/", 1)[0], run)
                if ref in named
                else _held_alike(ref[1:].split("/", 1)[0], founder, run)
            )
        ]
    theirs, _ = canonical_tools_settings(founder.get("toolsSettings"))
    settings: dict[str, Any] = {
        tool: (
            {
                k: v
                for k, v in entry.items()
                if (same_dir or k not in _DIRECTORY_KEYS)
                and not (with_run and k in _DENY_LIST_KEYS)
            }
            if isinstance(entry, dict)
            else entry
        )
        for tool, entry in (theirs.items() if isinstance(theirs, dict) else ())
        if _capability_of_tool(str(tool)) != "subagent"
    }
    ours, _ = canonical_tools_settings(_without_agent_grants(run).get("toolsSettings"))
    restricts = without_grant_only_settings(ours)
    # One entry per tool: the founder's may be filed under another spelling.
    filed = {_TOOLS_SETTINGS_CANONICAL.get(tool, tool): tool for tool in settings}
    for tool, entry in ours.items() if with_run and isinstance(ours, dict) else ():
        if _capability_of_tool(str(tool)) == "subagent":
            settings[tool] = entry
        elif isinstance(restricts, dict) and tool in restricts:
            settings.pop(filed.get(_TOOLS_SETTINGS_CANONICAL.get(tool, tool), tool), None)
            settings[tool] = entry
    if with_run and shell_denies_by_default(ours):
        listed = [r for r in half.get("allowedTools", ()) if not allowed_tools_grant_shell([r])]
        if allowed_tools_grant_shell(run.get("allowedTools")):
            listed.append("execute_bash")
        if listed or "allowedTools" in half:
            half["allowedTools"] = listed
    settings = {tool: entry for tool, entry in settings.items() if entry != {}}
    if settings:
        half["toolsSettings"] = settings
    rules = [
        _reaching_rule(rule, founder, run)
        for rule in _rules(founder, "allow", "ask", "deny")
        if same_dir
        or rule.get("effect") != "allow"
        or "match" not in rule
        or rule.get("capability") in _ANYWHERE
    ]
    rules = [r for r in (*rules, *asks, *_rules(run, "ask", "deny")) if r is not None]
    if rules:
        half["permissions"] = {"rules": [r for i, r in enumerate(rules) if r not in rules[:i]]}
    return half


def _unreadable(spec: dict[str, Any]) -> bool:
    """Whether *spec*'s ``toolsSettings`` or ``permissions`` cannot be read whole."""
    from kiro_crew.agent_sdk.kas_tool_view import permissions_block_readable

    settings, block = spec.get("toolsSettings"), spec.get("permissions")
    if settings is not None and not isinstance(settings, dict):
        return True
    return block is not None and not (isinstance(block, dict) and permissions_block_readable(block))


def _session_half(founder: dict[str, Any]) -> dict[str, Any]:
    """*founder*'s permission half as its own session reads it: nothing held back
    and nothing left out, with each server's ``autoApprove`` verbs as grants."""
    from kiro_crew.acp.kas_auto_approve import auto_approve_refs

    half = {key: founder[key] for key in _HALF_KEYS if key in founder}
    listed, auto = founder.get("allowedTools"), auto_approve_refs(founder, audit=False)
    if auto and (listed is None or isinstance(listed, list)):
        half["allowedTools"] = [*(listed or ()), *auto]
    return half


def _pre_tool_hooks(agent: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """*spec*'s own enabled PreToolUse hooks, each as the whole record the hook
    runner reads without :data:`_HOOK_NAMING_FIELDS`. Raises when they cannot be read."""
    from kiro_crew.agent_sdk.spec_hooks import spec_script_hooks
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

    declared = spec.get("hooks")
    if declared and not isinstance(declared, (dict, list)):
        raise ValueError(f"the hooks of agent {agent!r} are neither an object nor an array")
    return [
        {k: v for k, v in dataclasses.asdict(hook).items() if k not in _HOOK_NAMING_FIELDS}
        for hook in spec_script_hooks(agent, spec)
        if hook.enabled and hook.event == HOOK_EVENT_PRE_TOOL_USE and hook.command.strip()
    ]


def _hooks_run_on_the_run(
    founder_agent: str, founder: dict[str, Any], agent: str, run: dict[str, Any]
) -> bool:
    """Whether every PreToolUse hook of the founding agent's own runs on the run's
    calls too: the run's own spec carries the same entry, enabled.

    A spec's own hooks run on its own session's calls only, so a founder's hook the
    run lacks would stop a call in the founding chat and not here. The same entry
    is the same whole record: event, matcher and its mode, command, skills, timeout
    and output cap. The run may carry more hooks. A spec whose hooks cannot be read,
    on either side, answers no.
    """
    try:
        theirs = _pre_tool_hooks(founder_agent, founder)
        ours = _pre_tool_hooks(agent, run) if theirs else []
    except Exception:  # noqa: BLE001 - a hook that cannot be listed may refuse a call
        return False
    return all(hook in ours for hook in theirs)


def _has_pre_tool_hooks(founder_agent: str, founder: dict[str, Any]) -> bool:
    """Whether the founding agent's own spec has a PreToolUse hook. A spec whose
    hooks cannot be read answers yes."""
    try:
        return bool(_pre_tool_hooks(founder_agent, founder))
    except Exception:  # noqa: BLE001 - a hook that cannot be listed may refuse a call
        return True


def _same_directory(key: str, cwd: str) -> bool:
    """Whether the session on *key* is known to run in *cwd* and nowhere else."""
    from kiro_crew.subagent import parent_project_dirs

    dirs = parent_project_dirs(key)
    return bool(cwd and dirs) and all(os.path.realpath(d) == os.path.realpath(cwd) for d in dirs)


def _founding(agent: str, run_key: str, notices: list[str], cwd: str) -> Founding:
    """The founders of the run on *run_key* and the asks of every session above it;
    unchanged for a run with no parent session, and for one whose founders all run
    its own spec while no session above carries an ask its own spec lacks.

    A founder runs the run's own spec when it names *agent* and reads an equal spec
    (:func:`~kiro_crew.acp.kas_parent_limits.parent_spec`, the one reader for both).
    The name alone does not say so: a folder or an app binding can resolve one name
    to another spec, and that founder is then another agent to this run.
    """
    from kiro_crew.subagent import _parent_template_for_spawn

    keys, above = _sessions_above(run_key)
    if keys == []:
        return _UNCHANGED
    own = parent_spec(agent, run_key, cwd)
    founders: list[tuple[str, str, dict[str, Any], bool]] = []
    known = keys is not None
    for key in keys or ():
        name = _parent_template_for_spawn(key)
        specs = _specs_of(name, key, cwd) if name else []
        if not specs:
            known = False
            notices.append(
                "This sub-agent runs nothing without asking: no agent file defines "
                f"{name or 'the agent'!r} of the chat that started it."
            )
        founders.extend((name, key, spec, name == agent and spec == own) for spec in specs)
    asks: list[Any] = []
    for key in above:
        name = _parent_template_for_spawn(key)
        holders = _specs_of(name, key, cwd) if name else []
        if not holders and known:
            # Its asks cannot be read: no file defines its agent, or its record
            # is gone and with it the name of the agent it ran.
            known = False
            notices.append(
                "This sub-agent runs nothing without asking: "
                + (
                    f"no agent file defines {name!r} of a session that started it."
                    if name
                    else "a session that started it has no record of the agent it ran."
                )
            )
        asks.extend(rule for holder in holders for rule in _rules(holder, "ask"))
    if known and all(same for _name, _key, _spec, same in founders):
        held = _rules(own or {}, "ask")
        if all(rule in held for rule in asks):
            return _UNCHANGED
    return Founding(True, tuple(founders) if known else (), tuple(asks), cwd=cwd)


def founding_grants_spec(
    spec: dict[str, Any], agent: str, session_key: str, notices: list[str], cwd: Any = None
) -> tuple[dict[str, Any], Founding]:
    """*spec* for the run on *session_key*, with its founding agent's permission half.

    *spec* itself and an unchanged :class:`Founding` unless the preview is on and the
    session is a sub-agent run founded by a chat on another agent, or one below an
    ``ask`` its own spec lacks (:func:`_with_asks`). A Bot's worker
    keeps its spec (its merge is not done yet); :func:`limit_to_founders` limits it.
    So does a run whose own ``toolsSettings`` or ``permissions`` cannot be read whole,
    which then keeps no grant. A chain that cannot be read gives the run an empty
    half: no grant at all.
    """
    from kiro_crew.acp.harness.kas import MATE_WORKER_AGENT
    from kiro_crew.acp.kas_grant_only_settings import without_grant_only_settings
    from kiro_crew.agent_sdk.kas_tool_view import tools_settings_family
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    if not session_key.startswith("subagent:") or not preview_on():
        return spec, _UNCHANGED
    try:
        found = _founding(agent, session_key, notices, str(cwd or ""))
    except Exception:  # noqa: BLE001 - an unreadable chain grants nothing
        logger.warning("founding agent of %s could not be read", session_key, exc_info=True)
        found = Founding(True)
    if not found.applies or agent == MATE_WORKER_AGENT:
        return spec, found
    if _unreadable(spec):
        # What its own file asks for or refuses is unknown: its spec stays, with no grant.
        return spec, Founding(True, (), found.asks, cwd=found.cwd)
    if found.founders and all(same for _name, _key, _spec, same in found.founders):
        return _with_asks(spec, found)
    base = next((f for f in found.founders if not f[3]), ("", "", {}, False))
    half = _half(base, spec, agent, found.asks, found.cwd, with_run=True)
    out = {key: value for key, value in spec.items() if key not in _HALF_KEYS} | half
    servers = out.get("mcpServers")
    if isinstance(servers, dict):
        out["mcpServers"] = {
            name: (
                {k: v for k, v in entry.items() if k != "autoApprove"}
                if isinstance(entry, dict)
                else entry
            )
            for name, entry in servers.items()
        }
    if _unreadable(out):
        # Two readable halves can compose past what KAS reads, and the projection
        # would then drop the whole block, the run's own denies with it: as in
        # _with_asks, the run keeps its own spec and no grant.
        return spec, Founding(True, (), found.asks, cwd=found.cwd)
    limits = without_grant_only_settings(canonical_tools_settings(spec.get("toolsSettings"))[0])
    own = frozenset(tools_settings_family(str(tool)) for tool in limits or ())
    return out, Founding(True, found.founders, found.asks, True, found.cwd, own)


def _with_asks(spec: dict[str, Any], found: Founding) -> tuple[dict[str, Any], Founding]:
    """*spec* keeping its own permission half, with the asks of the sessions above.

    For a run whose founders all run its own spec: its grants are its founding
    chat's already, so only the ``ask`` rules it lacks are appended and its entry
    is limited by no founder (*kept*). With more rules than KAS reads the block
    would travel as no rule at all, so the spec stays and the run keeps no grant.
    """
    block = spec.get("permissions") or {}
    rules = list(block.get("rules") or ())
    rules += [r for i, r in enumerate(found.asks) if r not in rules and r not in found.asks[:i]]
    out = {**spec, "permissions": {**block, "rules": rules}}
    if _unreadable(out):
        return spec, Founding(True, (), found.asks, cwd=found.cwd)
    return out, dataclasses.replace(found, kept=True)


def _holding_its_denies(entry: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """*entry* recording every ``deniedCommands`` and write ``deniedPaths`` entry of
    *spec* the gate reads, as a parent's record does.

    The projection records them only beside a grant they held back, and an ask
    from above, or a chain that cannot be read, may have taken that grant: the
    gate refuses by them all the same.
    """
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    read = _gate_read_denies(canonical_tools_settings(spec.get("toolsSettings"))[0])
    if not any(read.values()):
        return entry
    record = entry.get(CREW_SPEC_DENIES_KEY) or {"builtins": [], "commands": []}
    merged = {k: [*dict.fromkeys([*(record.get(k) or []), *v])] for k, v in read.items()}
    return {**entry, CREW_SPEC_DENIES_KEY: {**record, **merged}}


def _entry_rules(entry: dict[str, Any]) -> list[Any]:
    block = entry.get("permissions")
    rules = block.get("rules") if isinstance(block, dict) else None
    return rules if isinstance(rules, list) else []


@dataclasses.dataclass(frozen=True)
class _Held:
    """What one founder's own entry runs unasked, for :func:`_limited`."""

    allows: frozenset[tuple[str, str]]
    refs: frozenset[str]
    named: dict[str, frozenset[str]]
    trusted: list[Any]


def _allow_matches(rule: dict[str, Any]) -> list[Any]:
    """The patterns an allow *rule* grants, ``[""]`` for the whole capability.

    Read as the projection reads it (``kas_permissions``: ``not rule.get("match")``):
    an empty ``match`` is no match, so the rule grants its whole capability.
    """
    match = rule.get("match")
    return match if isinstance(match, list) and match else [""]


def _held(entry: dict[str, Any], record_grants: bool) -> _Held:
    """*entry*'s unasked calls: ``(capability, match)`` of its plain allow rules
    (``""`` for a rule with no match), the ``server/tool`` refs Crew's gate answers,
    and each named grant list. *record_grants* counts the grants its deny record
    holds too, for a run that the same denies hold through its parent record."""
    record = entry.get(CREW_SPEC_DENIES_KEY) if record_grants else None
    record = record if isinstance(record, dict) else {}
    named = {key: frozenset(entry.get(key) or ()) for key in _NAMED_GRANT_KEYS}
    named[CREW_ANSWERED_BUILTINS_KEY] |= frozenset(record.get("builtins") or ())
    named[CREW_ANSWERED_COMMANDS_KEY] |= frozenset(record.get("commands") or ())
    return _Held(
        allows=frozenset(
            (str(rule.get("capability")), match)
            for rule in _entry_rules(entry)
            if isinstance(rule, dict) and rule.get("effect") == "allow" and not rule.get("exclude")
            for match in _allow_matches(rule)
        ),
        refs=frozenset(
            ref[1:]
            for key in _REF_GRANT_KEYS
            for ref in entry.get(key) or ()
            if isinstance(ref, str)
        ),
        named=named,
        trusted=list(record.get("webFetchTrusted") or ()),
    )


def _allowed(held: _Held, capability: str, match: str) -> bool:
    """Whether *held* allows *match*: the same pattern, the whole capability, or for
    an MCP tool its whole server."""
    wide = {(capability, match), (capability, "")}
    if capability == "mcp" and "/" in match:
        wide.add((capability, match.split("/", 1)[0] + "/*"))
    return bool(wide & held.allows)


def _named(held: _Held, key: str, value: str) -> bool:
    """Whether *held* holds the named grant *value* of *key*: itself, the whole
    tool a command or path grant is a part of, or for a built-in a plain allow of
    the capability that carries its name."""
    whole = _WHOLE_TOOL.get(key)
    return (
        value in held.named[key]
        or (whole is not None and whole in held.named[CREW_ANSWERED_BUILTINS_KEY])
        or (key == CREW_ANSWERED_BUILTINS_KEY and (value, "") in held.allows)
    )


def _limited(entry: dict[str, Any], others: list[_Held]) -> dict[str, Any]:
    """*entry* with every grant removed that one of *others* does not hold."""
    out = dict(entry)
    block = entry.get("permissions")
    if isinstance(block, dict) and isinstance(block.get("rules"), list):
        rules: list[Any] = []
        for rule in block["rules"]:
            if not isinstance(rule, dict) or rule.get("effect") != "allow":
                rules.append(rule)
                continue
            capability = str(rule.get("capability"))
            wanted = _allow_matches(rule)
            kept = [
                match
                for match in wanted
                if not rule.get("exclude") and all(_allowed(h, capability, match) for h in others)
            ]
            if kept:
                rules.append({**rule, "match": kept} if kept != [""] else rule)
        out["permissions"] = {**block, "rules": rules}
    for key in _NAMED_GRANT_KEYS:
        if key in out:
            out[key] = [v for v in out[key] if all(_named(h, key, v) for h in others)]
    for key in _REF_GRANT_KEYS:
        if key in out:
            out[key] = [
                ref
                for ref in out[key]
                if all(ref[1:] in h.refs or _allowed(h, "mcp", ref[1:]) for h in others)
            ]
    for key in (*_NAMED_GRANT_KEYS, *_REF_GRANT_KEYS):
        if key in out and not out[key]:
            del out[key]
    record = out.get(CREW_SPEC_DENIES_KEY)
    if isinstance(record, dict):
        # Its grants stand only with its own denies, so they are limited in place.
        trusted = record.get("webFetchTrusted") or []
        out[CREW_SPEC_DENIES_KEY] = {
            **record,
            "builtins": [
                b
                for b in record.get("builtins") or ()
                if all(_named(h, CREW_ANSWERED_BUILTINS_KEY, b) for h in others)
            ],
            "commands": [
                c
                for c in record.get("commands") or ()
                if all(_named(h, CREW_ANSWERED_COMMANDS_KEY, c) for h in others)
            ],
            **(
                {"webFetchTrusted": trusted if all(h.trusted == trusted for h in others) else []}
                if "webFetchTrusted" in record
                else {}
            ),
        }
    return out


def limit_to_founders(
    custom_agents: list[dict[str, Any]], agent: str, spec: dict[str, Any], found: Founding
) -> list[dict[str, Any]]:
    """*custom_agents* with the run's entry limited to what its founders run unasked.

    Each founder is projected twice under its own session key: the half it hands
    down (:func:`_half`) and its whole permission half as its own session reads it
    (:func:`_session_half`). A grant the run's entry holds and either entry does not
    is removed, so no hold on the first can leave the run a grant its founding chat
    is asked for. A run that kept its own half (``found.kept``) is not limited: its
    entry gains the record of its own denies (:func:`_holding_its_denies`), as does
    the entry of a run that is left no grant at all.
    A Bot's worker gains the chain's ``ask`` rules, and a founder's grant
    that stands only with the founder's own denies is not counted for it: its entry
    does not check them. Raises
    :class:`~kiro_crew.acp.kas_parent_limits.ParentLimitsUnreadable` when the entry
    would hold more rules than KAS reads, so the run is not started on it.
    """
    if not found.applies:
        return custom_agents
    if found.kept:
        return [
            (
                _holding_its_denies(entry, spec)
                if isinstance(entry, dict) and entry.get("id") == agent
                else entry
            )
            for entry in custom_agents
        ]
    try:
        entries = [
            kas_agents_mod.to_client_custom_agent(
                name, {**half, "tools": founder.get("tools") or []}, "", session_key=key
            )
            for name, key, founder, same in found.founders
            for half in (
                _half(
                    (name, key, founder, same), spec, agent, found.asks, found.cwd, with_run=False
                ),
                _session_half(founder),
            )
        ] or [{}]
    except Exception:  # noqa: BLE001 - a founder that cannot be projected grants nothing
        logger.warning("founding agent of %r could not be projected", agent, exc_info=True)
        entries = [{}]
    others = [_held(entry, found.swapped) for entry in entries]
    out: list[dict[str, Any]] = []
    for entry in custom_agents:
        if isinstance(entry, dict) and entry.get("id") == agent:
            entry = _limited(entry, others)
            if not found.founders:
                # It keeps no grant, and still refuses what its own file refuses.
                entry = _holding_its_denies(entry, spec)
            carried = entry.get(CREW_PARENT_LIMITS_KEY) or ()
            for key, family in _LIMIT_ONLY_KEYS.items() if found.swapped else ():
                theirs = family not in found.own and key in entry
                if theirs and any(c["limits"].get(key) == entry[key] for c in carried):
                    del entry[key]
            # A worker's own entry never saw the chain's asks: each founder's
            # projection carries them in the engine's own form.
            asks = [
                rule
                for other in ([] if found.swapped else entries)
                for rule in _entry_rules(other)
                if isinstance(rule, dict) and rule.get("effect") == "ask"
            ]
            rules = [*_entry_rules(entry), *asks]
            rules = [r for i, r in enumerate(rules) if r not in rules[:i]]
            if len(rules) > _MAX_RULES:
                raise ParentLimitsUnreadable(
                    f"agent {agent!r} would carry more than {_MAX_RULES} permission rules "
                    "with the asks of the chat that started it, so the sub-agent was not started"
                )
            if rules:
                entry["permissions"] = {**(entry.get("permissions") or {}), "rules": rules}
        out.append(entry)
    return out


def runs_on_founding_grants(run_key: str, agent: str, run_dir: str = "") -> bool:
    """Whether :func:`founding_grants_spec` changes the run on *run_key*. Blocking.

    For the warm pool, like ``kas_parent_limits.run_is_held``: a pooled session's
    batch was built before any run named it, and *run_dir* is the directory that
    session is bound to. An unreadable chain answers True. A session that names no
    agent registers no entry, so there is nothing to change.
    """
    if not agent or not run_key.startswith("subagent:") or not preview_on():
        return False
    try:
        return _founding(agent, run_key, [], run_dir).applies
    except Exception:  # noqa: BLE001 - the cold start decides
        return True
