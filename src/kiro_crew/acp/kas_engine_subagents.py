"""The ``kas_subagents`` preview: whether a KAS chat's agent uses the engine's sub-agents.

On (``dashboard.feature_previews.kas_subagents`` is ``true``, set under Settings >
Developer > Feature previews): in the chats :func:`engine_subagents_on` names
(never a conductor's, :func:`hands_work_to_sessions`), the chat agent's projected
entry mounts KAS's own sub-agent tool (``invoke_sub_agent``) naming the helper, a copy
of the chat's own agent (``acp/kas_engine_subagent_target.py``), the person's own
agents the target projection registers join it (:func:`_held_children`), and
``spawn_run`` and ``spawn_sub_agents`` leave that entry's tools. The operating
contract's one ``spawn_sub_agents`` sentence
names the engine's tool to match (:func:`contract_engine_subagent_mode`, read once per
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
from collections.abc import Callable, Mapping
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
    deny_every_subagent,
)
from kiro_crew.acp.kas_tool_limits import with_switched_off_tools

logger = logging.getLogger(__name__)

#: The preview's name under ``dashboard.feature_previews`` (``FEATURE_PREVIEW_FLAGS``).
KAS_SUBAGENTS_PREVIEW = "kas_subagents"

#: The shipped default agent.
DEFAULT_AGENT = "kirocrew"
#: The crew member whose own chat on the default agent registers the person's own
#: agents with every server they declare (:func:`own_agents_keep_their_servers`).
DEFAULT_MEMBER = "default"
#: Kiro Crew's own sub-agent tools the engine's tool replaces in a mounted chat.
_SPAWN_TOOLS = frozenset({("kirocrew-core", "spawn_sub_agents"), ("kirocrew-core", "spawn_run")})

#: The contract sentence the engine's tool replaces, as ``config/prompt.md`` ships
#: it. Only this sentence changes; a prompt that does not hold it (a person's own
#: ``prompt.md``) is left as it is.
SPAWN_SUB_AGENTS_SENTENCE = (
    "`spawn_sub_agents` blocks for inline results; use only if you cannot end the "
    "turn without them."
)
#: The first words of the shipped contract's three orchestration steps, which name
#: ``spawn_run`` and its completion events; the chats that drop it read
#: :data:`ENGINE_STEPS` in their place.
_SPAWN_STEP_STARTS = (
    "1. Submit all independent tasks in ONE `spawn_run(tasks=[…])` batch.",
    "2. Report the dispatch and **END YOUR TURN**.",
    "3. Results arrive as `[Subagent completion event]` messages.",
)
#: The orchestration steps for a mounted chat, which drops ``spawn_run``.
ENGINE_STEPS = (
    f"1. Call `{KAS_SUBAGENT_TOOL}` once per independent task, all in one reply. Each "
    "task states its goal, ready inputs, verifiable output and stop condition. Never "
    "hand a task a result that is still being worked out.\n"
    f"2. Each call's result comes back in this turn as the tool's own result: read "
    "the actual outcome before you answer; a failed or refused call is not success.\n"
    f"3. When `{KAS_SUBAGENT_TOOL}` is not in your tool list, submit the tasks in ONE "
    "`spawn_run(tasks=[…])` batch instead, report the dispatch and END YOUR TURN, then "
    "wait for the whole batch's `[Subagent completion event]` messages before you "
    "answer."
)
#: What the shipped contract's sub-agent bullet says of ``spawn_run`` before that
#: sentence; a mounted chat loses it with the sentence.
SPAWN_RUN_LEAD = (
    "`spawn_run`: background subagents, `tasks` array for parallel work; follow the "
    "lifecycle below. "
)
#: The bullet that replaces that lead and sentence in a mounted chat. It names its
#: condition, so a chat whose mount was refused (a remote workspace, a helper name
#: taken, a restriction the helper cannot mirror) still reads true: that chat keeps
#: both spawn tools and has no engine tool. It also says what the engine's own tool
#: text cannot: the mount runs the helper and the registered own agents only
#: (:func:`_also_reaching`), refusing an agent it never tried under a deny whose
#: quoted ``match`` is ``*``; and what no sub-agent it runs has
#: (``kas_engine_subagent_target.CHILD_NOTE``).
ENGINE_ONLY_SUB_AGENT_SENTENCE = (
    f"`{KAS_SUBAGENT_TOOL}`, when it is in your tool list, runs short, independent "
    f"tasks whose results you need before you answer inside this turn, in this chat's "
    f"own process, with agent `{KAS_HELPER_AGENT}` (this chat's own agent) or one of "
    "the user's own agents: one call per task, all in one reply. Its list names agents "
    "it will not run (Kiro Crew's own, and some of the user's): a refusal that reads "
    '`deny subagent matching "*"` refuses that one agent. No sub-agent it runs has a '
    "Kiro Crew tool or can start sub-agents, and none takes a model, folder or turn "
    "limit: keep that work yourself. When it is not in your tool list, `spawn_run` "
    "runs background subagents (`tasks` array for parallel work; follow the lifecycle "
    "below), and " + SPAWN_SUB_AGENTS_SENTENCE
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
    """Whether this KAS session's agent mounts the engine's sub-agent tool.

    True for a chat of any agent but a conductor with the ``kas_subagents`` preview
    on: a person's dashboard chat (``dashboard:`` / ``dashboard_`` keys, a crew
    member's, a member dispatch and a Crew panel included), a cron run (``cron:`` /
    ``cron_``) and a messaging channel's chat. Never a conductor's chat, a Bot's own
    thread among them (:func:`hands_work_to_sessions`), never a Kiro Crew sub-agent
    run, a workflow, a task runner or a heartbeat session, and never while the
    chat-runtime-sharing preview is on: a chat whose agent can start native
    sub-agents keeps its own process
    (``chat_runtime_sharing.spec_grants_native_subagents``), and this mount is not
    in the spec that check reads, so the preview keeps it off instead.
    *member_dispatch* and *crew_panel* do not narrow it.
    """
    from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled

    del member_dispatch, crew_panel
    return (
        kas_subagents_preview_on(cfg)
        and bool(agent)
        and not hands_work_to_sessions(agent)
        and _is_chat_key(session_key)
        and not chat_runtime_sharing_enabled(cfg)
    )


def hands_work_to_sessions(agent: str) -> bool:
    """Whether *agent* is a conductor, which hands its work to sessions, never to a sub-agent.

    A Bot's own conductor (``mate_worker.MATE_CONDUCTOR_AGENT``) starts no sub-agent:
    its hand-off is a worker session, so its spec leaves out Kiro Crew's spawn tools
    (``mate_worker.MATE_CONDUCTOR_EXCLUDED_TOOLS``) and the spawn gate refuses it
    (``subagent_parts.agent_policy.conductor_spawn_refusal``). Every conductor
    ``crew_mode.conductor_agents`` names (the shipped conductor templates, the
    configured ``agent.crew_mode_agent`` and its aliases) dispatches each work item
    to a session it can verify and report on. The engine's tool would give each an
    in-turn sub-agent beside that dispatch, so none of them mounts it. Read from the
    agent alone, the one fact the batch and the contract renderer share.
    """
    from kiro_crew.crew_mode import conductor_agents
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    return agent == MATE_CONDUCTOR_AGENT or agent in conductor_agents()


def _is_chat_key(session_key: str) -> bool:
    """Whether *session_key* is a chat's: the dashboard's, a cron run's or a channel's."""
    from kiro_crew.constants import is_owner_minted_session_key
    from kiro_crew.messaging.link import is_channel_session_key

    key = session_key or ""
    return (
        is_owner_minted_session_key(key)
        or key.startswith(("cron:", "cron_"))
        or is_channel_session_key(key)
    )


def own_agents_keep_their_servers(agent: str, session_key: str) -> bool:
    """Whether the person's own agents this chat registers keep MCP servers its agent lacks.

    Only the two chats that registered them before the engine's tool replaced
    ``spawn_run`` everywhere: a person's own dashboard chat on the default agent, and
    the crew member named ``default``'s own chat on it. There a child's server
    beyond the chat agent's is held by an ask the person answers. In every other
    mounted chat (a cron run, a channel, a dispatched worker, a Bot's worker chat,
    another member's) that ask may be answered by no one (a cron's ``auto``, a
    trusted chat), so each child keeps only the servers it shares with the chat's
    agent (:func:`_only_shared_servers`). Read from the agent and the key alone.
    Blocking file IO (the member's DM binding).
    """
    from kiro_crew.constants import is_owner_minted_session_key
    from kiro_crew.members import is_member_session_key, member_identity_for_session

    if agent != DEFAULT_AGENT or not is_owner_minted_session_key(session_key or ""):
        return False
    if not is_member_session_key(session_key):
        return True
    identity = member_identity_for_session(session_key)
    return identity is not None and identity.get("name") == DEFAULT_MEMBER


def contract_names_engine_subagents(
    provider_label: str | None, agent: str | None, session_key: str | None
) -> bool:
    """Whether a session's operating contract names the engine's sub-agent tool."""
    return bool(contract_engine_subagent_mode(provider_label, agent, session_key))


def contract_engine_subagent_mode(
    provider_label: str | None, agent: str | None, session_key: str | None
) -> str:
    """How a session's operating contract names the engine's sub-agent tool.

    ``""`` when it does not, ``"replace"`` when it does: the engine's tool replaces
    Kiro Crew's own sub-agent tools in every chat it is mounted in. A live reading, taken once per session by the contract renderer: the session's provider
    label is KAS's (positive), and :func:`engine_subagents_on` holds for it with no
    member dispatch or Crew panel. Never raises: an unreadable config reads ``""``.
    """
    from kiro_crew.acp.provider_labels import PROVIDER_LABEL_KAS

    if provider_label != PROVIDER_LABEL_KAS or not agent or not session_key:
        return ""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        if not engine_subagents_on(
            cfg, agent, session_key, member_dispatch=False, crew_panel=False
        ):
            return ""
        if _is_own_agent_file(agent):
            # Never mounted (``kas_native_batch``): its file is read as kiro-cli reads it.
            return ""
    except Exception:  # noqa: BLE001 - an unreadable config keeps today's contract
        logger.debug("the kas_subagents preview unreadable for the contract", exc_info=True)
        return ""
    return "replace"


def _is_own_agent_file(agent: str) -> bool:
    """Whether *agent*'s spec is a person's own file, which the mount skips.

    The same test ``kas_native_batch`` applies to the projected spec. A spec that
    cannot be read is judged by its name alone, as an empty spec: one of Kiro Crew's
    own names is not a person's file, any other name is. Blocking file IO.
    """
    from kiro_crew.acp.default_crew_servers import is_custom_agent
    from kiro_crew.acp.kas_agents import load_agent_spec
    from kiro_crew.config import paths as paths_mod

    try:
        spec: Any = load_agent_spec(paths_mod.kiro_agents_dir(), agent)
    except Exception:  # noqa: BLE001 - judged by its name alone
        spec = {}
    return is_custom_agent(agent, spec if isinstance(spec, dict) else {})


def _with_engine_steps(prompt: str) -> str:
    """*prompt* with the shipped three ``spawn_run`` steps replaced by :data:`ENGINE_STEPS`.

    Only when all three are there, one after the other, each a whole line; a prompt
    of the person's own is left as it is.
    """
    lines = prompt.split("\n")
    for i in range(len(lines) - 2):
        if all(lines[i + n].startswith(_SPAWN_STEP_STARTS[n]) for n in range(3)):
            return "\n".join([*lines[:i], ENGINE_STEPS, *lines[i + 3 :]])
    return prompt


def with_engine_sentence(prompt: str) -> str:
    """*prompt* with its one ``spawn_sub_agents`` sentence naming the engine's tool first.

    The shipped three ``spawn_run`` steps become :data:`ENGINE_STEPS` too.
    """
    prompt = _with_engine_steps(prompt)
    lead = SPAWN_RUN_LEAD + SPAWN_SUB_AGENTS_SENTENCE
    if lead in prompt:
        return prompt.replace(lead, ENGINE_ONLY_SUB_AGENT_SENTENCE, 1)
    return prompt.replace(SPAWN_SUB_AGENTS_SENTENCE, ENGINE_ONLY_SUB_AGENT_SENTENCE, 1)


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
    """The session agent's *entry* with the engine's tool on, only the helper reachable.

    ``spawn_run`` and ``spawn_sub_agents`` leave it: the engine's tool replaces them.
    """
    tools = entry.get("tools")
    held = entry.get("permissions")
    block = dict(held) if isinstance(held, dict) else {}
    block["rules"] = [*(block.get("rules") or []), only_helper_rule()]
    [entry] = with_switched_off_tools([entry], _SPAWN_TOOLS)
    # ``"*"`` already mounts the tool; any other shape is a list the tool joins.
    mounted = (
        tools
        if tools == "*"
        else list(dict.fromkeys([*(tools if isinstance(tools, list) else []), KAS_SUBAGENT_TOOL]))
    )
    return {**entry, "tools": mounted, "permissions": block}


def _with_parent_restrictions(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """*child* also carrying every ``deny`` and ``ask`` rule of the session agent's entry.

    A person's own agent runs as the session agent's sub-agent, so it is held to that
    agent's engine rules as well as to its gate-only limits: a read ``deniedPaths``, a
    web ``blocked`` entry or an authored deny refuses the child's call as it refuses
    the parent's. Only restrictions are copied, never an allow; the ``subagent`` rules
    stay the parent's.
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
        return child
    own = child.get("permissions")
    block = dict(own) if isinstance(own, dict) else {}
    block["rules"] = [*(block.get("rules") or []), *restrictions]
    return {**child, "permissions": block}


def _rules_of(entry: dict[str, Any]) -> list[Any]:
    """The ``permissions.rules`` list of a projected *entry*, or ``[]``."""
    held = entry.get("permissions")
    rules = held.get("rules") if isinstance(held, dict) else None
    return rules if isinstance(rules, list) else []


def _only_shared_servers(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """*child* holding only the MCP servers it declares exactly as *parent* does.

    Every other server of its block is dropped and excluded whole (``@server`` in its
    ``excludedTools``), its spec's names leave the ``mcp.json`` stamp, and the stamp
    marks it not opted in, so a settings-file server reaches it only as it reaches the
    parent. What remains is what an approval of its call can never widen past the
    chat's own agent. Read after ``kas_engine_child_grants.holding_parents_servers``,
    which gives a server set up as the parent's the parent's declaration.
    """
    from kiro_crew.acp.kas_engine_child_grants import _withheld_servers, shared_servers_of
    from kiro_crew.acp.kas_settings_mcp import CREW_SETTINGS_MCP_KEY
    from kiro_crew.acp.kas_subagent_targets import _excluding_switched_off, _without_servers

    held = child.get("mcpServers")
    stamp = child.get(CREW_SETTINGS_MCP_KEY)
    named = {n for n in (held if isinstance(held, dict) else ()) if isinstance(n, str)}
    if isinstance(stamp, dict):
        named.update(n for n in stamp.get("declared") or () if isinstance(n, str))
    # A pooled server both declare reaches the child as the session's own stub: the
    # target projection leaves it out of the child's block only when the two
    # declarations are alike (``kas_subagent_targets._pooled_elsewhere``), so a name
    # both stamps declare and neither block carries is the parent's server too.
    theirs = parent.get(CREW_SETTINGS_MCP_KEY)
    parent_block = parent.get("mcpServers")
    pooled = {
        n
        for n in named
        if n not in (held if isinstance(held, dict) else ())
        and n not in (parent_block if isinstance(parent_block, dict) else ())
        and isinstance(theirs, dict)
        and n in (theirs.get("declared") or ())
    } - _withheld_servers(parent)
    keep = shared_servers_of(child, parent) | pooled
    unshared = sorted(n for n in named if n and n not in keep)
    out = _excluding_switched_off(_without_servers(child, unshared), frozenset(unshared))
    if isinstance(stamp, dict):
        declared = [n for n in stamp.get("declared") or () if n in keep]
        out[CREW_SETTINGS_MCP_KEY] = {
            **stamp,
            "declared": declared,
            "optedIn": False,
            "legacy": False,
        }
    return out


def _only_parents_builtins(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any] | None:
    """*child*'s tool selection cut to what *parent*'s projected ``tools`` mount, or ``None``.

    A child runs its own tool list under its parent's rules, and a tool the parent's
    ``tools`` leave out is no rule the child copies: an ask an unattended approver
    answers would be all that held it. So the child keeps, never more than the
    parent's:

    * a built-in only when the parent mounts it (every built-in when the parent
      does not narrow them, ``kas_subagent_targets._narrows_tools``; else one of the
      parent's entries selects it, read with the engine's ``*`` and ``?``);
    * for a server, its own ref when the parent mounts that server whole, the
      parent's own refs to it in place of a whole-server ref when the parent mounts
      only some of its tools, a per-tool ref only when a parent ref covers it, and
      the server excluded whole when the parent mounts none of it;
    * every ``excludedTools`` entry of the parent.

    ``_only_shared_servers`` then cuts the servers it does not share. A child whose
    ``tools`` select by a wildcard, a pattern or an engine tag cannot be cut entry by
    entry, so it is not registered (``None``).
    """
    import fnmatch

    from kiro_crew.acp.kas_engine_subagent_target import _ENGINE_TAGS, _refs
    from kiro_crew.acp.kas_subagent_targets import _narrows_tools

    theirs = parent.get("tools")
    every_builtin = not _narrows_tools(parent)
    # Read as the engine selects a tool: ``*`` is any run, ``?`` any one character,
    # every other character itself (``kas_subagent_targets._selects_subagent_tool``).
    patterns = [
        t.replace("[", "[[]")
        for t in (theirs if isinstance(theirs, list) else [])
        if isinstance(t, str) and not t.startswith("@") and t != KAS_SUBAGENT_TOOL
    ]

    def mounts(name: str) -> bool:
        if name == KAS_SUBAGENT_TOOL:
            return False
        return every_builtin or any(fnmatch.fnmatchcase(name, p) for p in patterns)

    def server_refs(ref: str) -> list[str]:
        """The parent's refs that cover child *ref*: never wider than the parent's."""
        name = ref[1:].split("/", 1)[0]
        parents = _refs(theirs, name)
        if f"@{name}" in parents:
            return [ref]
        if ref == f"@{name}":
            return parents
        globs = [p.replace("[", "[[]") for p in parents]
        return [ref] if any(fnmatch.fnmatchcase(ref, g) for g in globs) else []

    held = parent.get("excludedTools")
    withdrawn = [t for t in (held if isinstance(held, list) else []) if isinstance(t, str)]
    current = child.get("excludedTools")
    mine = current if isinstance(current, list) else []
    if theirs == "*" or (isinstance(theirs, list) and "*" in theirs):
        # The parent mounts every tool: the child's own list is within it.
        if not withdrawn:
            return child
        return {**child, "excludedTools": list(dict.fromkeys([*mine, *withdrawn]))}
    tools = child.get("tools")
    if not isinstance(tools, list):
        return None
    kept: list[Any] = []
    unmounted: list[str] = []
    for entry in tools:
        if not isinstance(entry, str) or any(c in entry for c in "*?["):
            return None
        if entry.startswith("@"):
            server = entry[1:].split("/", 1)[0]
            if server in _ENGINE_TAGS:
                return None
            covered = server_refs(entry)
            if not covered:
                unmounted.append(f"@{server}")
            kept.extend(covered)
        elif mounts(entry):
            kept.append(entry)
    kept = list(dict.fromkeys(kept))
    # A server the child named that the parent mounts none of is excluded whole,
    # so its stub in the session-level array does not reach the child either.
    still = {t[1:].split("/", 1)[0] for t in kept if isinstance(t, str) and t.startswith("@")}
    unmounted = [r for r in dict.fromkeys(unmounted) if r[1:] not in still]
    excluded = list(dict.fromkeys([*mine, *withdrawn, *unmounted]))
    out = {**child, "tools": kept}
    if excluded:
        out["excludedTools"] = excluded
    return out


def _held_children(
    own_agents: Callable[[], tuple[list[dict[str, Any]], str]] | None,
    parent: dict[str, Any],
    keep_their_servers: bool = False,
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
    deny or an ask, so they add nothing. Unless *keep_their_servers*
    (:func:`own_agents_keep_their_servers`), each also keeps only the servers it
    shares with the parent (:func:`_only_shared_servers`). The line naming the agents
    left out is logged, not shown: the person listed none of them. A projection that
    fails registers none.
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
    from kiro_crew.acp import kas_env_launch
    from kiro_crew.acp.kas_engine_child_grants import asking_outside, holding_parents_servers

    def held(entry: dict[str, Any]) -> dict[str, Any] | None:
        projected = entry
        entry = holding_parents_servers(entry, parent)
        if not keep_their_servers:
            cut = _only_parents_builtins(entry, parent)
            if cut is None:
                # Its own projection took launch holds the batch will never
                # release, as a refused target's does.
                kas_env_launch.discard_batch([projected])
                logger.info(
                    "KAS sub-agents: %s left out (its tools reach past its parent's)",
                    entry.get("id"),
                )
                return None
            entry = _only_shared_servers(cut, parent)
        # Its ask on every MCP call spares the servers it declares exactly as
        # *parent* does, before the parent's own asks join: those stay whole.
        entry = _with_parent_restrictions(asking_outside(entry, parent), parent)
        # An engine sub-agent starts none of its own, as the helper does not.
        return deny_every_subagent(
            entry, "kas_engine_subagent_child_leaf", "a registered own agent starts no sub-agent"
        )

    kept = [held(e) for e in entries if e.get("id") != KAS_HELPER_AGENT]
    return [e for e in kept if e is not None]


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


def unmirrored_restriction(parent_spec: Any) -> str:
    """The first restriction of the session agent's spec the helper cannot mirror, or ``""``.

    The helper's ceiling must equal its parent's or be tighter. Each restriction a
    spec can carry, and where the helper gets it:

    * ``hooks`` PreToolUse: withheld on the helper's entry (``_withhold_session_hooks``);
      PostToolUse: fired for its finished calls (``acp.kas_child_finished``).
    * built-in tools, ``excludedTools``, ``allowedTools`` and the engine ``deny`` /
      ``ask`` rules of the projected entry (skill scope included): the helper's entry
      is a copy of the parent's (``kas_engine_subagent_target.helper_entry``).
    * gate-only limits (shell / write allowlists, denied commands): stamped on its
      requests (``session_handle_permissions._denies_only``).
    * MCP servers: the helper holds the parent's, with each ``excludedTools`` entry
      the parent's entry holds for them, and Kiro Crew's own are excluded whole
      (``kas_engine_subagent_target.shared_servers``).
    * sandbox, approval mode, governance: the parent's own session, process and gate.

    What has no mirror refuses the mount, so the session stays on Kiro Crew's own
    sub-agents exactly as on main: a spec that is not readable, ``excludedTools``
    naming any built-in (the sub-agent tool among them), and ``toolsSettings`` of any
    tool but the shell (:func:`_only_held_tools_settings`).
    """
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
        # Only an exclusion of a known MCP server leaves the engine's sub-agent tool
        # to the chat; a built-in, a tag or anything unrecognised may withhold it.
        name = canonical_kiro_cli_tool_name(raw) if isinstance(raw, str) else ""
        if not name.startswith("@") or name[1:].split("/", 1)[0] not in servers:
            return "its excludedTools withdraw a built-in"
    if not _only_held_tools_settings(parent_spec.get("toolsSettings")):
        return "it sets toolsSettings"
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
    work_dir: str | Path | None,
    stub_server_names: frozenset[str],
    settings_dir: str | Path | None,
    notices: list[str],
    parent_spec: dict[str, Any] | None = None,
    mounted_out: list[str] | None = None,
    own_agents: Callable[[], tuple[list[dict[str, Any]], str]] | None = None,
    own_agents_keep_servers: bool = False,
) -> list[dict[str, Any]]:
    """*custom_agents* with the session agent's entry mounted and the helper appended.

    The helper is a copy of the session agent's projected entry
    (``kas_engine_subagent_target.helper_entry``), and the mounted entry drops
    ``spawn_run`` and ``spawn_sub_agents`` (:func:`_mounted`).

    *mounted_out* gains *agent* only when the helper is built and appended here: the
    host-memory provenance a helper-only child's grant stamp requires.

    The person's own agents *own_agents* registers join the batch too, held as
    :func:`_held_children` says, and the mount's deny spares their ids. It is
    called only once the mount is accepted. *own_agents_keep_servers* is
    :func:`own_agents_keep_their_servers`'s answer for the session: without it, each
    keeps only the MCP servers it shares with the session agent.

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
    children = _held_children(own_agents, parent, own_agents_keep_servers)
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
        parent,
        stub_server_names=stub_server_names,
        settings_dir=settings_dir,
        other_servers=others,
    )
    if not isinstance(parent_spec, dict):  # refused above; narrows the type
        return custom_agents
    matchers = _session_hook_matchers(agent, parent_spec)
    helper = _withhold_session_hooks(helper, KAS_HELPER_AGENT, matchers)
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
        f"sub-agent tool limited to {KAS_HELPER_AGENT}, a copy of {agent} without "
        "Kiro Crew's own servers or a sub-agent tool, holding "
        f"{len(shared_servers(parent, stub_server_names))} of its MCP server(s)",
    )
    logger.info("KAS sub-agents on for %s: %s mounted", agent, KAS_HELPER_AGENT)
    if mounted_out is not None:
        mounted_out.append(agent)
    return [*mounted, helper]


__all__ = [
    "DEFAULT_MEMBER",
    "ENGINE_ONLY_SUB_AGENT_SENTENCE",
    "ENGINE_STEPS",
    "SPAWN_RUN_LEAD",
    "SPAWN_SUB_AGENTS_SENTENCE",
    "KAS_SUBAGENTS_PREVIEW",
    "contract_engine_subagent_mode",
    "contract_names_engine_subagents",
    "own_agents_keep_their_servers",
    "engine_subagents_on",
    "hands_work_to_sessions",
    "kas_subagents_preview_on",
    "parent_refuses_helper",
    "unmirrored_restriction",
    "with_engine_sentence",
    "with_engine_subagents",
]
