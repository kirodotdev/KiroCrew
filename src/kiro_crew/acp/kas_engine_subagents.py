"""The ``kas_subagents`` preview: whether a KAS chat's default agent uses the engine's sub-agents.

On (``dashboard.feature_previews.kas_subagents`` is ``true``, set under Settings >
Developer > Feature previews): in the sessions :func:`engine_subagents_on` names,
the default agent's projected entry mounts KAS's own sub-agent tool
(``invoke_sub_agent``) naming the shipped helper
(``acp/kas_engine_subagent_target.py``) and the person's own agents the target
projection registers (:func:`_held_children`), ``spawn_sub_agents`` leaves that entry's
tools, and the operating contract's one ``spawn_sub_agents`` sentence names the
engine's tool in its place (:func:`contract_names_engine_subagents`, read once per
session by the contract renderer). Off or never set (the default): nothing is
mounted and the contract is what it was. An LTS build reads it off
(``edition_locks.feature_previews``).

Read by ``KasHarness.session_extras`` through ``kas_native_batch``, so a KAS
session reads it, once, at its start: a running session keeps what it started
with. The KAS ``toolsSettings`` translation reads it too, for three
``deniedCommands`` entries (``acp/kas_deny_cli_forms.py``). Nothing is written to an agent file, the default template or
``config/defaults.json``; on, every other session kind and backend keeps what it
had, and ``spawn_sub_agents`` stays served everywhere. Of the other agents, one
is read differently in the same chats: a person's own agent that excludes the
sub-agent tool the way a file run on kiro-cli did
(``acp/kas_excluded_tool_cli.py``).

The one other reader is ``kas_shell_grant_lift``, through
:func:`kas_subagents_preview_on`: whether a shell ``allowedTools`` grant lifts a
spec's ``denyByDefault``, read where a KAS agent is projected and where its
session-start notice is built. On, the allowlist holds beside the grant, as
kiro-cli's agent engine keeps it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew.acp.kas_engine_subagent_target import (
    KAS_HELPER_AGENT,
    helper_entry,
    only_helper_rule,
    shared_servers,
)
from kiro_crew.acp.kas_subagent_targets import (
    KAS_SUBAGENT_TOOL,
    _session_hook_matchers,
    _withhold_session_hooks,
    audit_subagent_policy,
)
from kiro_crew.acp.kas_tool_limits import with_switched_off_tools

logger = logging.getLogger(__name__)

#: The preview's name under ``dashboard.feature_previews`` (``FEATURE_PREVIEW_FLAGS``).
KAS_SUBAGENTS_PREVIEW = "kas_subagents"

#: The shipped default agent, the only one this mounts the tool on.
DEFAULT_AGENT = "kirocrew"

#: The contract sentence the engine's tool replaces, as ``config/prompt.md`` ships
#: it, and its replacement. Only this sentence changes; a prompt that does not hold
#: it (a person's own ``prompt.md``) is left as it is. The replacement names its
#: condition, so a chat whose mount was refused (a helper name taken) still reads true.
#: It also says what the engine's own tool text cannot: that text lists every agent
#: the engine finds, and the mount runs the helper and the registered own agents
#: only (:func:`_also_reaching`), refusing an own agent it never tried under a deny
#: whose quoted ``match`` is ``*``; and no agent it runs holds a Kiro Crew server
#: (``kas_engine_subagent_target.own_servers``).
SPAWN_SUB_AGENTS_SENTENCE = (
    "`spawn_sub_agents` blocks for inline results; use only if you cannot end the "
    "turn without them."
)
ENGINE_SUB_AGENT_SENTENCE = (
    f"`{KAS_SUBAGENT_TOOL}`, when it is in your tool list, runs short, independent "
    f"tasks whose results you need before you answer inside this turn, with agent "
    f"`{KAS_HELPER_AGENT}` or one of the user's own agents: one call per task, all "
    "in one reply. Its list names agents it will not run (Kiro Crew's own, and some "
    'of the user\'s): a refusal that reads `deny subagent matching "*"` refuses '
    "that one agent, so start it with `spawn_run` instead. No sub-agent it runs has "
    "a Kiro Crew tool: keep that work or send it to `spawn_run`."
)

#: The not-registered line when a helper of the same name already exists.
_HELPER_TAKEN = (
    "ℹ In this chat, Kiro Crew's own sub-agents stay on: an agent named "
    f"{KAS_HELPER_AGENT} already exists, and Kiro Crew does not run another agent "
    "under that name."
)


def kas_subagents_preview_on(cfg: Any) -> bool:
    """Whether the ``kas_subagents`` feature preview is on in *cfg*: stored ``true`` only.

    Read through ``edition_locks.feature_previews`` like every server reader of a
    preview switch, so an LTS build reads it off. Absent, ``false`` or any other
    value is off.
    """
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(getattr(getattr(cfg, "dashboard", None), "feature_previews", None))
    return isinstance(previews, Mapping) and previews.get(KAS_SUBAGENTS_PREVIEW) is True


def engine_subagents_on(
    cfg: Any,
    agent: str,
    session_key: str,
    *,
    member_dispatch: bool,
    crew_panel: bool,
) -> bool:
    """Whether this KAS session's default agent mounts the engine's sub-agent tool.

    True only for a person's dashboard chat (``dashboard:`` / ``dashboard_`` keys,
    not a crew member's) on the shipped default agent, outside a member dispatch and
    a Crew panel, with
    the ``kas_subagents`` preview on and the chat-runtime-sharing preview off: a
    chat whose agent can start native sub-agents keeps its own process
    (``chat_runtime_sharing.spec_grants_native_subagents``), and this mount is not
    in the spec that check reads, so the preview keeps it off instead.
    """
    from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled
    from kiro_crew.constants import is_owner_minted_session_key
    from kiro_crew.members import is_member_session_key

    return (
        kas_subagents_preview_on(cfg)
        and agent == DEFAULT_AGENT
        and is_owner_minted_session_key(session_key)
        and not is_member_session_key(session_key)
        and not member_dispatch
        and not crew_panel
        and not chat_runtime_sharing_enabled(cfg)
    )


def contract_names_engine_subagents(
    provider_label: str | None, agent: str | None, session_key: str | None
) -> bool:
    """Whether a session's operating contract names the engine's sub-agent tool.

    A live reading, taken once per session by the contract renderer: the session's
    provider label is KAS's (positive), and :func:`engine_subagents_on` holds for it
    with no member dispatch or Crew panel (both need a crew member's key, which it
    refuses). Never raises: an unreadable config reads False.
    """
    from kiro_crew.acp.provider_labels import PROVIDER_LABEL_KAS

    if provider_label != PROVIDER_LABEL_KAS or not agent or not session_key:
        return False
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return engine_subagents_on(cfg, agent, session_key, member_dispatch=False, crew_panel=False)
    except Exception:  # noqa: BLE001 - an unreadable config keeps today's contract
        logger.debug("the kas_subagents preview unreadable for the contract", exc_info=True)
        return False


def with_engine_sentence(prompt: str) -> str:
    """*prompt* with its one ``spawn_sub_agents`` sentence naming the engine's tool."""
    return prompt.replace(SPAWN_SUB_AGENTS_SENTENCE, ENGINE_SUB_AGENT_SENTENCE, 1)


def _helper_file_exists(work_dir: str | Path | None) -> bool:
    """Whether an agent named like the helper sits where the engine looks.

    Resolved as every spec read resolves a name (``kas_agents.agent_spec_absent``):
    ``<name>.json``, ``<name>.md`` or any spec declaring the name. A folder that
    cannot be scanned counts as holding one, so the mount stays off.
    """
    from kiro_crew.acp.kas_agents import agent_spec_absent
    from kiro_crew.config import paths as paths_mod

    dirs = [paths_mod.kiro_agents_dir()]
    if work_dir:
        dirs.append(paths_mod.project_agents_dir(work_dir))
    for d in dirs:
        if not d.exists():
            continue
        try:
            if not agent_spec_absent(d, KAS_HELPER_AGENT):
                return True
        except Exception:  # noqa: BLE001 - absence not established keeps the mount off
            logger.warning("agents folder %s unreadable; KAS sub-agents stay off", d, exc_info=True)
            return True
    return False


def _mounted(entry: dict[str, Any]) -> dict[str, Any]:
    """The default agent's *entry* with the engine's tool on, only the helper reachable."""
    tools = entry.get("tools")
    held = entry.get("permissions")
    block = dict(held) if isinstance(held, dict) else {}
    block["rules"] = [*(block.get("rules") or []), only_helper_rule()]
    [entry] = with_switched_off_tools([entry], frozenset({("kirocrew-core", "spawn_sub_agents")}))
    # ``"*"`` already mounts the tool; any other shape is a list the tool joins.
    mounted = (
        tools
        if tools == "*"
        else list(dict.fromkeys([*(tools if isinstance(tools, list) else []), KAS_SUBAGENT_TOOL]))
    )
    return {**entry, "tools": mounted, "permissions": block}


def _with_parent_restrictions(helper: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """*helper* also carrying every ``deny`` and ``ask`` rule of the session agent's entry.

    The helper runs as the session agent's sub-agent, so it is held to that agent's
    engine rules as well as to its gate-only limits: a read ``deniedPaths``, a web
    ``blocked`` entry or an authored deny refuses the helper's call as it refuses the
    parent's. Only restrictions are copied, never an allow; the ``subagent`` rules
    stay the parent's, since the helper has no sub-agent tool.
    """
    held = parent.get("permissions")
    rules = held.get("rules") if isinstance(held, dict) else None
    restrictions = [
        r
        for r in (rules if isinstance(rules, list) else [])
        if isinstance(r, dict)
        and r.get("effect") in ("deny", "ask")
        and r.get("capability") != "subagent"
    ]
    if not restrictions:
        return helper
    own = helper.get("permissions")
    block = dict(own) if isinstance(own, dict) else {}
    block["rules"] = [*(block.get("rules") or []), *restrictions]
    return {**helper, "permissions": block}


def _rules_of(entry: dict[str, Any]) -> list[Any]:
    """The ``permissions.rules`` list of a projected *entry*, or ``[]``."""
    held = entry.get("permissions")
    rules = held.get("rules") if isinstance(held, dict) else None
    return rules if isinstance(rules, list) else []


def _held_children(
    own_agents: Callable[[], tuple[list[dict[str, Any]], str]] | None,
    parent: dict[str, Any],
) -> list[dict[str, Any]]:
    """The person's own agents this mount registers, each held as the helper is.

    *own_agents* is the target projection a custom parent's ``availableAgents``
    list goes through (``kas_subagent_targets.project_subagent_targets``, with
    ``every_agent``), so each entry already passed its refusals: none of Kiro
    Crew's own agents, no agent whose limits only the gate applies, none with its
    own PreToolUse hooks, what the parent's hooks cover withheld, every Kiro Crew
    server excluded whole, and an ask on every MCP call, which here spares the
    servers the entry declares exactly as the parent does
    (``kas_engine_child_grants.asking_outside``), a server its file sets up as the
    parent's file does being declared as the parent's entry declares it
    (``kas_engine_child_grants.holding_parents_servers``). One more holds here: each
    entry carries every ``deny`` and ``ask`` rule of the parent's entry
    (:func:`_with_parent_restrictions`). An entry's own allow rules stay: the
    engine answers a child by its parent's decision unless the child's own is a
    deny or an ask, so they add nothing. The line naming the agents left out is
    logged, not shown: the person listed none of them. A projection that fails
    registers none.
    """
    if own_agents is None:
        return []
    try:
        entries, notice = own_agents()
    except Exception:  # noqa: BLE001 - the person's agents never cost the session
        logger.warning("the person's own agents could not be registered", exc_info=True)
        return []
    if notice:
        logger.info("KAS sub-agents: %s", notice)
    # The helper's id names the helper alone: a file of that name written after
    # the mount's own check would otherwise register under it.
    from kiro_crew.acp.kas_engine_child_grants import asking_outside, holding_parents_servers

    # Its ask on every MCP call spares the servers it declares exactly as *parent*
    # does, before the parent's own asks join: those stay whole.
    return [
        _with_parent_restrictions(
            asking_outside(holding_parents_servers(e, parent), parent), parent
        )
        for e in entries
        if e.get("id") != KAS_HELPER_AGENT
    ]


def _also_reaching(mounted: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """*mounted* with the mount's deny sparing *names* beside the helper.

    The deny :func:`_mounted` appended is the entry's last rule. Any other shape
    there leaves the entry as it is, so the names stay denied.
    """
    rules = list(_rules_of(mounted))
    last = rules[-1] if rules else None
    if not (
        isinstance(last, dict)
        and last.get("capability") == "subagent"
        and last.get("effect") == "deny"
        and last.get("exclude") == [KAS_HELPER_AGENT]
    ):
        return mounted
    rules[-1] = {**last, "exclude": [KAS_HELPER_AGENT, *names]}
    return {**mounted, "permissions": {**mounted["permissions"], "rules": rules}}


#: Entries of a spec's ``tools`` that mount every built-in (the projection passes
#: ``@builtin/`` and ``@builtin/*`` through unexpanded there, so they mount none).
_ALL_BUILTINS_TOOLS = frozenset({"*", "@builtin"})
#: Entries of a spec's ``allowedTools`` that grant every built-in.
_ALL_BUILTINS_ALLOWED = frozenset({"*", "@builtin", "@builtin/", "@builtin/*"})


def _reaches_all(raw: Any, names: Iterable[str], wide: frozenset[str]) -> bool:
    """Whether a spec ``tools`` / ``allowedTools`` value names every one of *names*."""
    if raw == "*":
        return True
    listed = {t for t in raw if isinstance(t, str)} if isinstance(raw, list) else set()
    return bool(listed & wide) or set(names) <= listed


def unmirrored_restriction(parent_spec: Any) -> str:
    """The first restriction of the session agent's spec the helper cannot mirror, or ``""``.

    The helper's ceiling must equal its parent's or be tighter. Each restriction a
    spec can carry, and where the helper gets it:

    * ``hooks`` PreToolUse: withheld on the helper's entry (``_withhold_session_hooks``);
      PostToolUse: fired for its finished calls (``acp.kas_child_finished``).
    * engine ``deny`` / ``ask`` rules of the projected entry (skill scope included):
      copied (:func:`_with_parent_restrictions`).
    * gate-only limits (shell / write allowlists, denied commands): stamped on its
      requests (``session_handle_permissions._denies_only``).
    * MCP servers: the helper holds the parent's, with each ``excludedTools`` entry
      the parent's entry holds for them, and Kiro Crew's own are excluded whole
      (``kas_engine_subagent_target.shared_servers``).
    * sandbox, approval mode, governance: the parent's own session, process and gate.

    What has no mirror refuses the mount, so the session stays on Kiro Crew's own
    sub-agents exactly as on main: a spec that is not readable, ``excludedTools``
    naming any built-in, ``toolsSettings`` of any tool but the shell
    (:func:`_only_held_tools_settings`), a ``tools`` that does not reach every
    helper built-in, or an ``allowedTools`` that does not run unasked everything
    the helper runs unasked.
    """
    from kiro_crew.acp.kas_engine_subagent_target import _HELPER_ALLOWED, _HELPER_TOOLS
    from kiro_crew.platform.tool_names import canonical_kiro_cli_tool_name

    if not isinstance(parent_spec, dict):
        return "the agent spec could not be read"
    from kiro_crew.acp.kas_agents_mcp_servers import MANAGED_MCP_SERVER_NAMES

    declared = parent_spec.get("mcpServers")
    servers = set(MANAGED_MCP_SERVER_NAMES) | (
        set(declared) if isinstance(declared, dict) else set()
    )
    excluded = parent_spec.get("excludedTools")
    for raw in excluded if isinstance(excluded, list) else ([excluded] if excluded else []):
        # Only an exclusion of a known MCP server (the helper reaches none) has its
        # mirror; a built-in, a tag or anything unrecognised does not.
        name = canonical_kiro_cli_tool_name(raw) if isinstance(raw, str) else ""
        if not name.startswith("@") or name[1:].split("/", 1)[0] not in servers:
            return "its excludedTools withdraw a built-in"
    if not _only_held_tools_settings(parent_spec.get("toolsSettings")):
        return "it sets toolsSettings"
    if not _reaches_all(parent_spec.get("tools"), _HELPER_TOOLS, _ALL_BUILTINS_TOOLS):
        return "its tools do not include every built-in the helper holds"
    if not _reaches_all(parent_spec.get("allowedTools"), _HELPER_ALLOWED, _ALL_BUILTINS_ALLOWED):
        return "its allowedTools do not run unasked what the helper runs unasked"
    return ""


#: The shell settings every native sub-agent is held to as its parent is. A
#: ``deniedCommands`` entry is an engine deny rule the sub-agent's entry copies
#: (:func:`_with_parent_restrictions`) or a deny the gate refuses on its request;
#: ``denyByDefault`` with ``allowedCommands`` and ``autoAllowReadonly`` are the
#: allowlist and flag the gate stamps on it
#: (``session_handle_permissions._denies_only``); ``allowedCommands`` alone
#: is a grant, which reaches a sub-agent only with its parent's other grants.
_HELD_SHELL_SETTINGS = frozenset(
    {"allowedCommands", "autoAllowReadonly", "deniedCommands", "denyByDefault"}
)


def _only_held_tools_settings(raw: Any) -> bool:
    """Whether a spec's ``toolsSettings`` *raw* holds nothing a sub-agent escapes.

    True for none at all, and for settings of the shell alone whose every key is
    one of :data:`_HELD_SHELL_SETTINGS`. Read with one key per kiro-cli alias
    group, as the projection reads them; a block that is not an object, a key
    kiro-cli discards, another tool's settings and a shell key not listed all
    answer False: no test shows a sub-agent held to them.
    """
    if not raw:
        return True
    from kiro_crew.acp.kas_spec_grants import SHELL_SETTINGS_KEYS
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    tools_settings, dropped = canonical_tools_settings(raw)
    if dropped or not isinstance(tools_settings, dict):
        return False
    return all(
        tool in SHELL_SETTINGS_KEYS
        and isinstance(settings, dict)
        and set(settings) <= _HELD_SHELL_SETTINGS
        for tool, settings in tools_settings.items()
    )


def parent_refuses_helper(parent: dict[str, Any]) -> bool:
    """Whether the session agent's projected entry already denies starting the helper.

    A ``deny`` whose capability KAS expands to ``subagent``
    (``kas_permissions.META_CAPABILITY_EXPANSION``: ``subagent``, ``builtin``,
    ``all``) and whose ``match`` covers ``kirocrew-helper`` while its ``exclude``
    does not, both read as KAS compiles them (``kas_skill_scope._rule_matches`` and
    ``_cedar_like``: ``*`` is any run, every other character is itself, an absent or
    empty ``match`` is every name). Mounted, such a chat would keep that deny and
    lose ``spawn_sub_agents`` too, with no in-turn sub-agent tool left, so it stays
    on Kiro Crew's own sub-agents instead. Read before the mount, so Kiro Crew's own
    deny on its agents (``kas_subagent_targets.deny_own_agents``) is not among them.
    """
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION
    from kiro_crew.acp.kas_skill_scope import _cedar_like, _rule_matches

    held = parent.get("permissions")
    rules = held.get("rules") if isinstance(held, dict) else None
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, dict) or rule.get("effect") != "deny":
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str):
            continue
        if "subagent" not in META_CAPABILITY_EXPANSION.get(capability, (capability,)):
            continue
        match = rule.get("match")
        listed = [p for p in match if isinstance(p, str)] if isinstance(match, list) else []
        exclude = rule.get("exclude")
        spared = [p for p in exclude if isinstance(p, str)] if isinstance(exclude, list) else []
        if any(_rule_matches(KAS_HELPER_AGENT, p) for p in listed or ["*"]) and not any(
            _cedar_like(KAS_HELPER_AGENT, p) for p in spared
        ):
            return True
    return False


def with_engine_subagents(
    custom_agents: list[dict[str, Any]],
    agent: str,
    *,
    agents_dir: Path,
    work_dir: str | Path | None,
    stub_server_names: frozenset[str],
    session_key: str,
    settings_dir: str | Path | None,
    notices: list[str],
    parent_spec: dict[str, Any] | None = None,
    mounted_out: list[str] | None = None,
    own_agents: Callable[[], tuple[list[dict[str, Any]], str]] | None = None,
) -> list[dict[str, Any]]:
    """*custom_agents* with the default agent's entry mounted and the helper appended.

    *mounted_out* gains *agent* only when the helper is built and appended here: the
    host-memory provenance a helper-only child's grant stamp requires.

    The person's own agents *own_agents* registers join the batch too, held as
    :func:`_held_children` says, and the mount's deny spares their ids. It is
    called only once the mount is accepted.

    The helper runs inside the session agent's turn, so that agent's own spec
    PreToolUse hooks meet its requests: its entry withholds auto-approval for what
    they cover, as a registered target's does. A session agent whose spec carries a
    restriction the helper cannot mirror (:func:`unmirrored_restriction`) gets no
    mount.

    Called only when :func:`engine_subagents_on` holds. Refused, with a notice and
    the batch unchanged, when the batch already holds an entry named like the
    helper or an agent file of that name sits in the user's or the project's agents
    folder: the shipped helper never shares its name with another agent.
    """
    if any(isinstance(a, dict) and a.get("id") == KAS_HELPER_AGENT for a in custom_agents) or (
        _helper_file_exists(work_dir)
    ):
        notices.append(_HELPER_TAKEN)
        return custom_agents
    parent = next((a for a in custom_agents if isinstance(a, dict) and a.get("id") == agent), None)
    if parent is None:
        return custom_agents
    unmirrored = unmirrored_restriction(parent_spec) or (
        "its permissions deny the helper" if parent_refuses_helper(parent) else ""
    )
    if unmirrored:
        logger.info("KAS sub-agents stay off for %s: %s", agent, unmirrored)
        return custom_agents
    # Before the helper is built, so it excludes their servers as a target's.
    children = _held_children(own_agents, parent)
    custom_agents = [*custom_agents, *children]
    # The helper holds its parent's servers. Every other entry's own servers can
    # reach the session-level array too, so the helper excludes those.
    others = frozenset(
        name
        for a in custom_agents
        if isinstance(a, dict) and a is not parent and isinstance(a.get("mcpServers"), dict)
        for name in a["mcpServers"]
    )
    helper = helper_entry(
        agents_dir,
        parent,
        stub_server_names=stub_server_names,
        session_key=session_key,
        settings_dir=settings_dir,
        other_servers=others,
    )
    if not isinstance(parent_spec, dict):  # refused above; narrows the type
        return custom_agents
    matchers = _session_hook_matchers(agent, parent_spec)
    helper = _withhold_session_hooks(helper, KAS_HELPER_AGENT, matchers)
    own_rules = len(_rules_of(helper))
    helper = _with_parent_restrictions(helper, parent)
    mounted = [_mounted(a) if a is parent else a for a in custom_agents]
    if children:
        reached = [str(c.get("id")) for c in children]
        mounted = [_also_reaching(a, reached) if a.get("id") == agent else a for a in mounted]
        audit_subagent_policy(
            "kas_engine_subagent_children",
            agent,
            f"sub-agent tool also reaches {', '.join(reached)}, each carrying the "
            "deny and ask rules of its parent and asking for every MCP call",
        )
    audit_subagent_policy(
        "kas_engine_subagent_mounted",
        agent,
        f"sub-agent tool limited to {KAS_HELPER_AGENT}, which carries "
        f"{len(_rules_of(helper)) - own_rules} deny or ask rule(s) of its parent "
        f"and holds {len(shared_servers(parent, stub_server_names))} of its MCP server(s)",
    )
    logger.info("KAS sub-agents on for %s: %s mounted", agent, KAS_HELPER_AGENT)
    if mounted_out is not None:
        mounted_out.append(agent)
    return [*mounted, helper]


__all__ = [
    "ENGINE_SUB_AGENT_SENTENCE",
    "SPAWN_SUB_AGENTS_SENTENCE",
    "KAS_SUBAGENTS_PREVIEW",
    "contract_names_engine_subagents",
    "engine_subagents_on",
    "kas_subagents_preview_on",
    "parent_refuses_helper",
    "unmirrored_restriction",
    "with_engine_sentence",
    "with_engine_subagents",
]
