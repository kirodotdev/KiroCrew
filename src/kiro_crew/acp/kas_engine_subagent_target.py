"""The one agent Kiro Crew ships for KAS's own sub-agent tool: ``kirocrew-helper``.

A KAS sub-agent (``invoke_sub_agent``) runs inside its parent's turn under the
entry the session's ``customAgents`` batch registers for its name. This module
builds that entry: the chat's own agent, the one an unnamed ``spawn_run`` runs.
It is a copy of the parent's projected entry (built-in tools, ``allowedTools``,
``permissions``, prompt, resources) under the helper's id, less what an in-turn
sub-agent cannot have: Kiro Crew's own servers, the sub-agent tool itself, and
the per-run options of a ``spawn_run`` (:data:`CHILD_NOTE` tells it so). It
exists only in memory, in the batch of a session that mounts it; Kiro Crew
writes no agent file for it.

The helper holds the servers its parent holds (:func:`shared_servers`) and no
other. The engine hands a sub-agent the session-level array and the servers its
own entry declares, never its parent's block, so the helper keeps the parent's
``tools`` entries for each of those servers, repeats the parent's projected
block entry byte for byte (the engine keys a running server by its name and
declaration, so the helper is handed the parent's process and starts none), and
carries every ``excludedTools`` entry the parent holds. The engine intersects a
sub-agent's policy with its parent's, and the copy's policy is the parent's, so
what the parent runs unasked, is asked about or is denied, the helper is too.

Kiro Crew's own servers stay out, whole (:func:`own_servers`): each is launched
with the parent session's identity, so inside it a helper's call is the
parent's, and nothing can refuse a session-bound tool or a spawn for the helper
alone. So do a sub-agent target's servers. A server from the OS user's
``mcp.json`` is in the default agent's block, so the helper shares it as the
parent holds it; a project's ``mcp.json`` server reaches neither (the settings
stamp, ``kas_settings_mcp.stamp_settings_mcp``, marks the helper not opted in).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The helper's agent id. No agent file carries it; a user's agent file of the
#: same name never takes its place (:func:`helper_entry` builds its own).
KAS_HELPER_AGENT = "kirocrew-helper"

#: What the helper's prompt says after its parent's: what this in-turn run cannot
#: do that a ``spawn_run`` child can. Its tool list holds none of Kiro Crew's own
#: tools (:func:`own_servers`) and no sub-agent tool, and the engine starts it with
#: none of ``spawn_run``'s per-run options.
CHILD_NOTE = (
    "You are running as a sub-agent inside another agent's turn, not in a chat of "
    "your own. Do the one task you were given and reply with the result only. You "
    "have no Kiro Crew tool (no memory, cron, messaging or spawn tool), you cannot "
    "start sub-agents, and no model, folder or turn limit was set for this run. If "
    "the task needs something you do not have, say so in your reply instead of "
    "working around it."
)

#: The line the engine lists under its sub-agent tool, where the parent picks an agent.
HELPER_DESCRIPTION = (
    "Runs one short, independent task as this chat's own agent, without Kiro Crew's "
    "own tools or sub-agents, and reports the result back."
)


def _uncopied_keys() -> frozenset[str]:
    """Keys of the parent's projected entry the helper's copy leaves out.

    Kiro Crew's own records of what it answers for the session agent: the gate
    stamps the session agent's on the helper's requests from the session, never from
    the helper's entry (``session_handle_permissions._stamp_parent_grants``). The
    mcp.json settings stamp among them: the helper gets its own.
    """
    from kiro_crew.acp.kas_parent_limits import CREW_PARENT_LIMITS_KEY
    from kiro_crew.acp.kas_permissions import CREW_ONLY_ENTRY_KEYS

    return CREW_ONLY_ENTRY_KEYS | {CREW_PARENT_LIMITS_KEY}


#: What the engine reads after ``@`` in a ``tools`` entry as a tag of its own,
#: never as a server (KAS 0.66.119's tool tags). A server of one of these names
#: is not listed for the helper: its ``@name`` would select something else, the
#: sub-agent tool among them.
_ENGINE_TAGS = frozenset(
    {
        "builtin",
        "mcp",
        "mcp-disclosure",
        "powers",
        "sandbox",
        "subagent",
        "subagent-explicit",
        "tool-server",
    }
)


def own_servers() -> frozenset[str]:
    """Kiro Crew's own servers, by rule: its managed servers and the builtin app servers.

    Both are launched with the session's identity
    (``kas_agents_session_hoist.hoist_servers_for_session``), so a call the
    helper made there would be its parent's. Read from the two sources on every
    call: a managed server added later is one of these with no edit here.
    """
    from kiro_crew.acp import kas_agents_mcp_servers
    from kiro_crew.acp.session_mcp import builtin_app_identity_launches

    return frozenset(kas_agents_mcp_servers.MANAGED_MCP_SERVER_NAMES) | frozenset(
        builtin_app_identity_launches()
    )


def _refs(tools: Any, name: str) -> list[str]:
    """The entries of a projected ``tools`` value that mount server *name*.

    ``"*"`` mounts every tool, so it names the whole server. Otherwise only an
    entry that names the server itself (``@name``, ``@name/...``) counts, as
    written: a pattern over server names is not followed, which leaves the helper
    without that server.
    """
    if tools == "*":
        return [f"@{name}"]
    listed = [t for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
    if "*" in listed:
        return [f"@{name}"]
    return [t for t in listed if t == f"@{name}" or t.startswith(f"@{name}/")]


def shared_servers(parent: dict[str, Any], stub_server_names: Iterable[str]) -> dict[str, Any]:
    """The parent's servers the helper holds: each name with the parent's block entry.

    ``None`` stands for a server the session-level array carries: a pooled stub of
    a server the parent's spec declares (the settings stamp's ``declared``). A
    server counts only when the parent's ``tools`` mount it and its
    ``excludedTools`` do not withdraw it whole. Never one of Kiro Crew's own
    (:func:`own_servers`), and never a name the engine reads as a tag or a pattern
    in a ``tools`` entry. The block entry is a copy of the PROJECTED one, so the
    helper's declaration is the parent's byte for byte.
    """
    import copy

    from kiro_crew.acp.kas_settings_mcp import CREW_SETTINGS_MCP_KEY

    held = parent.get("mcpServers")
    block = held if isinstance(held, dict) else {}
    stamp = parent.get(CREW_SETTINGS_MCP_KEY)
    declared = stamp.get("declared") if isinstance(stamp, dict) else None
    pooled = {n for n in stub_server_names if n in (declared or ())}
    excluded = parent.get("excludedTools")
    withdrawn = set(excluded) if isinstance(excluded, list) else set()
    own = own_servers()
    out: dict[str, Any] = {}
    for name in sorted(n for n in {*block, *pooled} if isinstance(n, str) and n):
        if name in own or name in _ENGINE_TAGS or any(c in name for c in "/*?"):
            continue
        if f"@{name}" in withdrawn or not _refs(parent.get("tools"), name):
            continue
        out[name] = copy.deepcopy(block[name]) if name in block else None
    return out


def helper_entry(
    parent_entry: dict[str, Any],
    *,
    stub_server_names: frozenset[str],
    settings_dir: str | Path | None,
    other_servers: Iterable[str] = (),
) -> dict[str, Any]:
    """The helper's ``customAgents`` entry for this session: a copy of its parent's.

    *parent_entry* is the session agent's projected entry before the mount, the
    agent an unnamed ``spawn_run`` runs. The copy keeps its built-in tools,
    ``allowedTools``, ``permissions``, ``excludedTools``, prompt and resources,
    under the helper's id and description, with :data:`CHILD_NOTE` after the
    prompt. It leaves out Kiro Crew's own records of the parent
    (:func:`_uncopied_keys`) and the sub-agent tool: ``invoke_sub_agent`` leaves its
    tools and joins its ``excludedTools``, and every engine sub-agent is denied
    (``kas_subagent_targets.deny_every_subagent``). Of the parent's block it keeps
    the servers :func:`shared_servers` names; every other server that could reach
    it is excluded whole: Kiro Crew's own, the pooled stubs and the parent's block
    servers it does not share, and *other_servers* (a sub-agent target's). The
    ``mcp.json`` settings stamp marks it not opted in, with the shared servers as
    its own.
    """
    import copy

    from kiro_crew.acp import kas_env_launch
    from kiro_crew.acp.kas_settings_mcp import stamp_settings_mcp
    from kiro_crew.acp.kas_subagent_targets import (
        KAS_SUBAGENT_TOOL,
        _excluding_switched_off,
        deny_every_subagent,
    )

    uncopied = _uncopied_keys()
    entry = {k: copy.deepcopy(v) for k, v in parent_entry.items() if k not in uncopied}
    prompt = entry.get("prompt")
    entry.update(
        id=KAS_HELPER_AGENT,
        description=HELPER_DESCRIPTION,
        prompt=f"{prompt}\n\n{CHILD_NOTE}" if isinstance(prompt, str) and prompt else CHILD_NOTE,
    )
    shared = shared_servers(parent_entry, stub_server_names)
    tools = entry.get("tools")
    if isinstance(tools, list):
        # A server it does not share leaves its list too (it is excluded below):
        # what remains names built-ins, engine tags and the shared servers.
        entry["tools"] = [
            t
            for t in tools
            if t != KAS_SUBAGENT_TOOL
            and not (
                isinstance(t, str)
                and t.startswith("@")
                and (server := t[1:].split("/", 1)[0]) not in shared
                and server not in _ENGINE_TAGS
            )
        ]
    excluded = entry.get("excludedTools")
    entry["excludedTools"] = list(
        dict.fromkeys([*(excluded if isinstance(excluded, list) else []), KAS_SUBAGENT_TOOL])
    )
    entry["mcpServers"] = {
        name: declaration for name, declaration in shared.items() if declaration is not None
    }
    # The repeated declarations name the parent's launch snapshots, and the batch
    # is released once per entry that names one: hold each once more.
    kas_env_launch.retain_batch([{"mcpServers": entry.get("mcpServers")}])
    held = parent_entry.get("mcpServers")
    names = {n for n in (held if isinstance(held, dict) else ()) if isinstance(n, str)}
    names.update(n for n in (*stub_server_names, *other_servers) if isinstance(n, str))
    names.update(own_servers())
    entry = _excluding_switched_off(entry, frozenset(n for n in names if n and n not in shared))
    [entry] = stamp_settings_mcp(
        [entry], KAS_HELPER_AGENT, settings_dir, declared=sorted(shared), opted_in=False
    )
    return deny_every_subagent(
        entry, "kas_engine_subagent_helper_leaf", "the helper starts no sub-agent of its own"
    )


def only_helper_rule() -> dict[str, Any]:
    """The ``subagent`` deny a mounted session agent carries: every agent but the helper."""
    return {
        "capability": "subagent",
        "match": ["*"],
        "exclude": [KAS_HELPER_AGENT],
        "effect": "deny",
    }


def helper_is_the_only_child(registered: Any, agent: str) -> bool:
    """Whether every sub-agent *agent* can start in this batch is the shipped helper.

    True when the registered batch holds the helper's entry and *agent*'s own entry
    carries :func:`only_helper_rule`, the mount's mark. A native child of such a
    session runs the helper's entry, which is built from its parent's. With the
    person's own agents registered the mount's deny spares them too, the rule is
    another one, and this is False: which child asked is then the engine's to say
    (:func:`engine_names_the_helper`).
    """
    batch = [a for a in registered if isinstance(a, dict)] if isinstance(registered, list) else []
    if not agent or not any(a.get("id") == KAS_HELPER_AGENT for a in batch):
        return False
    entry = next((a for a in batch if a.get("id") == agent), None)
    held = entry.get("permissions") if entry else None
    rules = held.get("rules") if isinstance(held, dict) else None
    return isinstance(rules, list) and only_helper_rule() in rules


#: The depth the engine reports for a sub-agent the session's own agent started.
DIRECT_CHILD_DEPTH = 1


def is_mount_deny(rule: Any) -> bool:
    """Whether *rule* is the deny a mount adds to the session agent's entry.

    :func:`only_helper_rule`, or that rule with the person's registered agents
    spared after the helper: a ``subagent`` deny on every name whose ``exclude``
    opens with the helper's id.
    """
    return (
        isinstance(rule, dict)
        and rule.get("capability") == "subagent"
        and rule.get("effect") == "deny"
        and rule.get("match") == ["*"]
        and isinstance(rule.get("exclude"), list)
        and rule["exclude"][:1] == [KAS_HELPER_AGENT]
    )


def _entry_rules(entry: Any) -> list[Any]:
    """The ``permissions.rules`` list of a batch *entry*, or ``[]``."""
    held = entry.get("permissions") if isinstance(entry, dict) else None
    rules = held.get("rules") if isinstance(held, dict) else None
    return rules if isinstance(rules, list) else []


def engine_names_the_helper(requester: Any) -> bool:
    """Whether the engine says the helper, started by the session's own agent, raised a request.

    *requester* is the ``(name, depth)`` the engine put on that one permission
    request (``kas_wire.requesting_subagent``): the name it looked the sub-agent's
    definition up by, and how deep under the session's own agent it runs. True
    only when the name is the helper's id, exactly, and the depth is the integer
    :data:`DIRECT_CHILD_DEPTH`. False for everything else: no requester (the
    engine sent none), a name that is not a string or not that id, a depth that is
    missing or not an integer, a helper another sub-agent started.

    The name says which definition runs only where the shipped helper is the entry
    under that id, which this function does not know: the caller asks that first.
    """
    if not isinstance(requester, tuple) or len(requester) != 2:
        return False
    name, depth = requester
    if not isinstance(name, str) or name != KAS_HELPER_AGENT:
        return False
    # ``True`` is an ``int`` to ``isinstance``; the engine sends a number.
    return type(depth) is int and depth == DIRECT_CHILD_DEPTH


def mount_deny_last(custom_agents: list[dict[str, Any]], agent: str) -> list[dict[str, Any]]:
    """*custom_agents* with the mount's deny after every other rule of *agent*'s entry.

    When the engine refuses a call it quotes the first deny that matches, and of
    that rule only its ``match``. With the mount's deny first, naming one of Kiro
    Crew's own agents reads ``deny subagent matching "*"``, which says every
    sub-agent is blocked. Behind the denies that name agents, the refusal names the
    agent. Which calls are refused does not change: the most restrictive matching
    rule decides, whatever the order. A batch without the helper, and an entry
    without that deny, come back as they are.
    """
    if not any(isinstance(a, dict) and a.get("id") == KAS_HELPER_AGENT for a in custom_agents):
        return custom_agents
    out: list[dict[str, Any]] = []
    for entry in custom_agents:
        rules = _entry_rules(entry)
        if isinstance(entry, dict) and entry.get("id") == agent and any(map(is_mount_deny, rules)):
            ordered = [r for r in rules if not is_mount_deny(r)]
            ordered += [r for r in rules if is_mount_deny(r)]
            entry = {**entry, "permissions": {**entry["permissions"], "rules": ordered}}
        out.append(entry)
    return out


__all__ = [
    "CHILD_NOTE",
    "DIRECT_CHILD_DEPTH",
    "HELPER_DESCRIPTION",
    "KAS_HELPER_AGENT",
    "engine_names_the_helper",
    "helper_entry",
    "helper_is_the_only_child",
    "mount_deny_last",
    "only_helper_rule",
    "own_servers",
    "shared_servers",
]
