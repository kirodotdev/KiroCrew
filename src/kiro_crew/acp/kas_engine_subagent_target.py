"""The one agent Kiro Crew ships for KAS's own sub-agent tool: ``kirocrew-helper``.

A KAS sub-agent (``invoke_sub_agent``) runs inside its parent's turn under the
entry the session's ``customAgents`` batch registers for its name. This module
builds that entry for a plain helper: built-in tools, its parent's MCP servers,
no hooks, no skills. It exists only in memory, in the batch of a session that
mounts it; Kiro Crew writes no agent file for it.

The helper holds the servers its parent holds (:func:`shared_servers`) and no
other. The engine hands a sub-agent the session-level array and the servers its
own entry declares, never its parent's block, so the helper lists each of the
parent's servers in ``tools`` as the parent's ``tools`` name it, repeats the
parent's projected block entry byte for byte (the engine keys a running server
by its name and declaration, so the helper is handed the parent's process and
starts none), and carries every ``excludedTools`` entry the parent holds for
them. The parent's ``permissions`` rules reach the helper's calls in the engine,
which intersects a sub-agent's policy with its parent's, so what the parent runs
unasked, is asked about or is denied, the helper is too.

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

#: The helper's built-in tools, kiro-cli names: the default agent's built-ins
#: without the sub-agent tool, the tool loader and the session tools.
_HELPER_TOOLS = (
    "fs_read",
    "fs_write",
    "execute_bash",
    "grep",
    "glob",
    "code",
    "web_fetch",
    "web_search",
)
#: What the helper's own spec runs without asking: nothing. Its parent's rules
#: decide each call in the engine, and what they keep at ask reaches Kiro Crew's
#: gate, which answers it as it answers the parent's.
_HELPER_ALLOWED: tuple[str, ...] = ()

_HELPER_PROMPT = (
    "You are a helper that Kiro Crew's chat agent hands one short, independent task. "
    "Do that task with your tools and reply with the result only. "
    "You cannot start other agents, and nothing you do outlives this reply. "
    "If the task needs a tool you do not have, say so in your reply instead of "
    "working around it."
)

#: Where the default agent's standing rules on what a shell or file tool must not do
#: start: the first words of each of those bullets in the ``## Rules`` section of the
#: shipped ``config/prompt.md``, the one source the parent's prompt is built from. The
#: helper carries each whole bullet as that file has it (:func:`helper_tool_rules`), so
#: no second copy of the text exists; ``test_kas_engine_subagent_target.py`` fails when
#: the source loses one. Guidance only: what the helper may do is bounded by the same
#: gate and sandbox as its parent.
HELPER_RULE_ANCHORS: tuple[str, ...] = (
    "- Do NOT run `git push` to protected branches",
    "- Do NOT run destructive commands",
    "- A blocked call is a policy decision, not a puzzle.",
    "- Do NOT read credential files directly",
    "- You CAN run AWS CLI commands",
    "- If you need to serve files over HTTP",
)
_RULES_HEADING = "## Rules"


def helper_tool_rules() -> tuple[str, ...]:
    """The ``## Rules`` bullets of the shipped prompt that :data:`HELPER_RULE_ANCHORS` names.

    Read from the file at build time, in anchor order, each whole bullet as written.
    An anchor the section does not hold is left out and logged; an unreadable file
    gives none, and the helper keeps only its own three sentences.
    """
    from kiro_crew.agent import _BUNDLED_CFG_DIR

    try:
        lines = (_BUNDLED_CFG_DIR / "prompt.md").read_text(encoding="utf-8").splitlines()
        start = lines.index(_RULES_HEADING) + 1
    except (OSError, ValueError):
        logger.warning("the shipped prompt's Rules could not be read for the helper", exc_info=True)
        return ()
    section: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        section.append(line)
    found: list[str] = []
    for anchor in HELPER_RULE_ANCHORS:
        match = next((line for line in section if line.startswith(anchor)), None)
        if match is None:
            logger.warning("the shipped prompt's Rules hold no bullet starting %r", anchor)
        else:
            found.append(match)
    return tuple(found)


def helper_spec() -> dict[str, Any]:
    """The helper's agent spec, in kiro-cli's shape, built fresh on every call."""
    return {
        "name": KAS_HELPER_AGENT,
        # The engine lists this line under its sub-agent tool, where the parent
        # picks an agent, so it says what the helper holds (:func:`shared_servers`)
        # and what it does not (:func:`own_servers`).
        "description": (
            "Runs one short, independent task with built-in tools and the chat "
            "agent's MCP servers (no Kiro Crew tool) and reports the result back."
        ),
        "prompt": "\n\n".join((_HELPER_PROMPT, "\n".join(helper_tool_rules()))),
        "tools": list(_HELPER_TOOLS),
        "allowedTools": list(_HELPER_ALLOWED),
        "mcpServers": {},
        "includeMcpJson": False,
        "resources": [],
    }


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


def _with_parents_servers(
    entry: dict[str, Any], parent: dict[str, Any], shared: dict[str, Any]
) -> dict[str, Any]:
    """The helper's *entry* holding the *shared* servers exactly as *parent* does.

    Its ``tools`` gain the parent's own entries for each server, its
    ``excludedTools`` every entry the parent holds for one of them (a switched-off
    tool, a ``disabledTools`` name or glob), and its block the parent's entries.
    """
    if not shared:
        return entry
    tools = entry.get("tools")
    refs = [ref for name in shared for ref in _refs(parent.get("tools"), name)]
    held = parent.get("excludedTools")
    carried = [
        t
        for t in (held if isinstance(held, list) else [])
        if isinstance(t, str) and t.startswith("@") and t[1:].split("/", 1)[0] in shared
    ]
    current = entry.get("excludedTools")
    out = {
        **entry,
        "tools": list(dict.fromkeys([*(tools if isinstance(tools, list) else []), *refs])),
        "excludedTools": list(
            dict.fromkeys([*(current if isinstance(current, list) else []), *carried])
        ),
    }
    block = {name: declaration for name, declaration in shared.items() if declaration is not None}
    if block:
        out["mcpServers"] = block
    return out


def helper_entry(
    agents_dir: Path,
    parent_entry: dict[str, Any],
    *,
    stub_server_names: frozenset[str],
    session_key: str,
    settings_dir: str | Path | None,
    other_servers: Iterable[str] = (),
) -> dict[str, Any]:
    """The helper's projected ``customAgents`` entry for this session.

    Projected through the same translation as every other agent
    (``kas_agents.build_kas_custom_agents``), then given its parent's servers
    (:func:`shared_servers`). Every other server that could reach it is excluded
    whole: Kiro Crew's own, the pooled stubs and the parent's block servers it
    does not share, and *other_servers* (a sub-agent target's). The ``mcp.json``
    settings stamp marks it not opted in, with the shared servers as its own.
    """
    from kiro_crew.acp import kas_agents as kas_agents_mod
    from kiro_crew.acp import kas_env_launch
    from kiro_crew.acp.kas_settings_mcp import stamp_settings_mcp
    from kiro_crew.acp.kas_subagent_targets import _excluding_switched_off

    spec = helper_spec()
    [entry] = kas_agents_mod.build_kas_custom_agents(
        agents_dir,
        KAS_HELPER_AGENT,
        spec,
        stub_server_names=stub_server_names,
        session_key=session_key,
    )
    shared = shared_servers(parent_entry, stub_server_names)
    entry = _with_parents_servers(entry, parent_entry, shared)
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
    return entry


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
    "DIRECT_CHILD_DEPTH",
    "HELPER_RULE_ANCHORS",
    "KAS_HELPER_AGENT",
    "engine_names_the_helper",
    "helper_entry",
    "helper_is_the_only_child",
    "helper_spec",
    "helper_tool_rules",
    "mount_deny_last",
    "only_helper_rule",
    "own_servers",
    "shared_servers",
]
