"""A moved Bot's restrictions its worker cannot carry exactly, and the ask guard for them.

When the upgrade moves a Bot onto its own file (``mate_config_migration``), or a
start repairs one an earlier release moved without its old agent's limits
(``mate_policy_repair``), each ``hooks`` entry, ``permissions`` rule and
``toolsSettings`` entry of the old agent is carried into the Bot's file when the
worker keeps it exactly (:func:`split_policy`). One that cannot be carried is
never written in a form that would stop the worker from starting. It is
recorded instead, under ``held_rules`` in ``migrated.json``, with the capability
it speaks about, and:

- on KAS, an ``ask`` rule for that capability is carried in its place, so KAS
  sends every such call to Crew instead of answering it from ``allowedTools``;
- Crew's own approval gates never approve such a call without a person
  (:func:`held_for`): an ``auto`` cron, Trust, YOLO, a trusted pattern or a hook
  grant sends it to the approval card instead, and a surface with no card refuses it;
- under a held ``subagent`` limit, Crew's own spawn admission starts none of the
  Bot's sub-agents on its own either (:func:`held_spawn_limits`).

Nothing clears a hold: picking the Bot's approval mode does not, since an ``auto``
cron's approval does not depend on that mode. Every other call behaves as before.
Blocking file I/O; never raises from the gate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: The ``migrated.json`` key: Bot name -> its held restrictions.
HELD_KEY = "held_rules"

#: A held restriction whose capability cannot be told: every call of that Bot asks.
ANY = "*"

#: A held hook on an event that sees no tool call: recorded and named in the
#: notice, but no call falls under it and no ``ask`` rule is written for it.
NO_CALL = "none"

#: ``toolsSettings`` family -> the KAS capability its entries limit.
_FAMILY_CAPABILITY = {
    "shell": "shell",
    "read": "fs_read",
    "write": "fs_write",
    "web_fetch": "web_fetch",
    "subagent": "subagent",
}

#: Built-in tool ids (kiro-cli and KAS spellings) -> capability.
_TOOL_CAPABILITY = {
    **dict.fromkeys(
        ("execute_bash", "execute_pwsh", "shell", "run_command", "control_bash_process"), "shell"
    ),
    **dict.fromkeys(("fs_read", "read", "read_file", "grep", "glob", "list_directory"), "fs_read"),
    **dict.fromkeys(("fs_write", "write", "fs_append", "str_replace", "write_file"), "fs_write"),
    **dict.fromkeys(("web_fetch", "fetch"), "web_fetch"),
    **dict.fromkeys(("web_search", "remote_web_search"), "web_search"),
    **dict.fromkeys(("subagent", "use_subagent"), "subagent"),
}

#: ACP semantic kind -> capability.
_KIND_CAPABILITY = {
    "execute": "shell",
    "read": "fs_read",
    "search": "fs_read",
    "edit": "fs_write",
    "delete": "fs_write",
    "move": "fs_write",
    "fetch": "web_fetch",
}

#: Capabilities a carried ``ask`` rule is written for: every one a held limit can
#: name, so KAS sends each such call to Crew instead of answering it itself.
_ASKABLE = frozenset({*_FAMILY_CAPABILITY.values(), "mcp", "web_search"})

_KNOWN = _ASKABLE


def _capability_of_tool(name: str) -> str:
    from kiro_crew.agent_sdk.kas_tool_view import tools_settings_family

    return _TOOL_CAPABILITY.get(name) or _FAMILY_CAPABILITY.get(tools_settings_family(name), "")


def _hook_capability(event: Any, entry: Any) -> str:
    from kiro_crew.agent import _KAS_TRIGGER_CANONICAL

    trigger = _KAS_TRIGGER_CANONICAL.get(event.lower()) if isinstance(event, str) else None
    if trigger is not None and trigger != "PreToolUse":
        # Only a preToolUse hook sees a call before it runs; a known other
        # event's hook (agentSpawn, userPromptSubmit, postToolUse, stop) limits
        # no call. An event that cannot be told is read as a gate.
        return NO_CALL
    matcher = entry.get("matcher") if isinstance(entry, dict) else None
    if not isinstance(matcher, str) or matcher in ("", "*"):
        return ANY
    if matcher.startswith("@builtin"):
        # ``@builtin`` covers every built-in tool, ``@builtin/<tool>`` that one.
        tool = matcher.partition("/")[2]
        return _tool_capability(tool) if tool else ANY
    if matcher.startswith("@") or "/" in matcher:
        return "mcp"
    return _tool_capability(matcher)


def _tool_capability(name: str) -> str:
    """The capability of tool *name*; ``tool:<name>`` for a known tool no capability names.

    That name is kiro-cli's own (``useAws`` gives ``tool:use_aws``). Any other name
    (a glob, a tag, a case variant, one Crew does not know) covers every call.
    """
    from kiro_crew.mate_held_scope import named_tool

    return _capability_of_tool(name) or named_tool(name) or ANY


def _bundle_beside_rules(perms: Any) -> bool:
    """Whether *perms* is ``rules`` plus a ``policies`` list of bundle ids, and nothing else."""
    return (
        isinstance(perms, dict)
        and set(perms) == {"rules", "policies"}
        and isinstance(perms.get("rules"), list)
        and isinstance(perms.get("policies"), list)
        and all(isinstance(p, str) for p in perms["policies"])
    )


def _pieces(policy: dict[str, Any]) -> list[tuple[str, dict[str, Any], list[str] | None]]:
    """Each restriction in *policy*, as ``(capability, the policy holding it alone, paths)``.

    *paths* are the paths a read or write limit names alone
    (``mate_held_scope.rule_paths``), ``None`` for one that covers every such call.
    """
    from kiro_crew.mate_held_scope import rule_paths, settings_paths
    from kiro_crew.mate_restriction_carry import _GRANT_ONLY_SETTINGS

    out: list[tuple[str, dict[str, Any], list[str] | None]] = []
    hooks = policy.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            for entry in entries if isinstance(entries, list) else [entries]:
                out.append((_hook_capability(event, entry), {"hooks": {event: [entry]}}, None))
    elif isinstance(hooks, list):
        # The list form names each entry's event inside it (``trigger``, or ``event``).
        for entry in hooks:
            event = (
                (entry.get("trigger") or entry.get("event")) if isinstance(entry, dict) else None
            )
            out.append((_hook_capability(event, entry), {"hooks": [entry]}, None))
    elif "hooks" in policy:
        out.append((ANY, {"hooks": hooks}, None))
    perms = policy.get("permissions")
    if isinstance(perms, dict) and _bundle_beside_rules(perms):
        # A named bundle (``policies``) only expands into ``allow`` rules on KAS's
        # side: a grant, not a limit. Leaving it behind narrows, and each rule
        # beside it is carried or held under its own capability, as in a block
        # of rules alone. Read whole, one deny there made every call ask.
        perms = {"rules": perms["rules"]}
    if isinstance(perms, dict) and set(perms) <= {"rules"} and isinstance(perms.get("rules"), list):
        for rule in perms["rules"]:
            cap = rule.get("capability") if isinstance(rule, dict) else None
            cap = cap if isinstance(cap, str) and cap in _KNOWN else ANY
            out.append((cap, {"permissions": {"rules": [rule]}}, rule_paths(cap, rule)))
    elif "permissions" in policy:
        out.append((ANY, {"permissions": perms}, None))
    settings = policy.get("toolsSettings")
    if isinstance(settings, dict):
        for tool, entry in settings.items():
            # Not narrowed to ``tool:<name>`` as a hook is: a setting limits what the
            # tool reaches, and a harness with no such tool (KAS, Claude) reaches it
            # through the shell, so ``use_aws`` settings keep every call held.
            cap = _capability_of_tool(str(tool)) or ANY
            paths = settings_paths(cap, entry, _GRANT_ONLY_SETTINGS)
            out.append((cap, {"toolsSettings": {tool: entry}}, paths))
    elif "toolsSettings" in policy:
        out.append((ANY, {"toolsSettings": settings}, None))
    return out


def _lands(base: dict[str, Any], mate_file: dict[str, Any], piece: dict[str, Any]) -> bool:
    """Whether the worker merged from *mate_file* runs and keeps every restriction of *piece*."""
    from kiro_crew.mate_config_migration import dropped_restrictions
    from kiro_crew.mate_spec_policy import SpecPolicyUncarried
    from kiro_crew.mate_worker import merge_worker_spec, worker_additions

    try:
        merged = merge_worker_spec(base, worker_additions(base, mate_file))
    except SpecPolicyUncarried:
        return False
    return not dropped_restrictions(base, piece, merged)


def _phrases(base: dict[str, Any], piece: dict[str, Any]) -> list[str]:
    from kiro_crew.mate_config_migration import dropped_restrictions

    return dropped_restrictions(base, piece, base) or ["one of its limits"]


def _without_allows(doc: dict[str, Any], caps: set[str]) -> dict[str, Any]:
    """*doc* without its ``permissions`` allow rules for *caps* (every one under :data:`ANY`).

    A held capability keeps no allow of any kind: one would run its calls with no
    permission request, so the gate that asks a person would never see them.
    """
    perms = doc.get("permissions")
    if not caps or not isinstance(perms, dict) or not isinstance(perms.get("rules"), list):
        return doc
    rules = perms["rules"]
    kept = [
        r
        for r in rules
        if not (
            isinstance(r, dict)
            and r.get("effect") == "allow"
            and (ANY in caps or r.get("capability") in caps)
        )
    ]
    if len(kept) != len(rules):
        doc["permissions"] = {**perms, "rules": kept}
    return doc


def split_policy(
    applies: bool, base: dict[str, Any], policy: dict[str, Any], mate_file: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """*mate_file* with every restriction of *policy* it can carry exactly, and the rest.

    *applies* says the Bot's worker backend runs a file's restrictions (KAS);
    without it nothing is written, since such a file would stop the worker, and
    every restriction is held. Each held one is ``{"text", "capability"}``. On KAS
    an ``ask`` rule is carried for each held capability that has one. The result
    always merges: the worker starts. *mate_file* is not changed.
    """
    from kiro_crew.mate_spec_policy import SpecPolicyUncarried, layer_policy

    pieces = _pieces(policy)
    asks: set[str] = set()
    while True:
        # The asks the held restrictions need are written first, so a carried
        # restriction can never use up the room one of them needs; a restriction
        # that does not fit is held, and its capability's ask joins the next pass.
        # A held capability keeps no allow rule either, which also makes room.
        out = _without_allows(copy_json_document(mate_file), asks)
        landed: set[str] = set()
        for capability in sorted(asks):
            trial = copy_json_document(out)
            try:
                layer_policy(trial, {"permissions": {"rules": [_ask(capability)]}})
            except SpecPolicyUncarried:
                trial = None
            if trial is not None and _lands(base, trial, {}):
                out = trial
                landed.add(capability)
            else:
                # No ask, and no allow: the call still asks at Crew's gate.
                logger.warning("held Bot rules: an ask on %s could not be written", capability)
        held: list[dict[str, Any]] = []
        for capability, piece, paths in pieces:
            trial = copy_json_document(out)
            try:
                layer_policy(trial, piece)
            except SpecPolicyUncarried:
                trial = None
            if applies and trial is not None and _lands(base, trial, piece):
                out = trial
                continue
            held.extend(_held_rule(t, capability, paths) for t in _phrases(base, piece))
        if not applies:
            return out, held
        held_caps = {h["capability"] for h in held}
        # A limit whose capability cannot be told asks on every capability.
        needed = set(_ASKABLE) if ANY in held_caps else held_caps & _ASKABLE
        if needed <= asks:
            # Every needed ask was tried in this pass. One that did not land (a
            # full policy) leaves its capability with no ask and no allow, so KAS
            # sends the call to Crew, whose gate asks a person (``held_for``).
            if needed - landed:
                logger.warning(
                    "held Bot rules: no ask written for %s; Crew's gate holds those calls",
                    ", ".join(sorted(needed - landed)),
                )
            return out, held
        asks |= needed


def _ask(capability: str) -> dict[str, str]:
    return {"capability": capability, "effect": "ask"}


def held_scopes(base: dict[str, Any], policy: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Each restriction text of *policy* -> the held rule :func:`split_policy` records for it.

    A text two restrictions of different reach share maps to ``None``: which one a
    recorded rule came from cannot be told.
    """
    out: dict[str, dict[str, Any] | None] = {}
    for capability, piece, paths in _pieces(policy):
        for text in _phrases(base, piece):
            rule = _held_rule(text, capability, paths)
            out[text] = rule if out.get(text, rule) == rule else None
    return out


def _held_rule(text: str, capability: str, paths: list[str] | None) -> dict[str, Any]:
    """One recorded held limit; ``paths`` only when it covers those paths alone."""
    rule: dict[str, Any] = {"text": text, "capability": capability}
    if paths:
        rule["paths"] = list(paths)
    return rule


def record_held(name: str, member_id: str, held: list[dict[str, str]]) -> None:
    """Record *held* as Bot *name*'s restrictions that need a person; none clears it."""
    from kiro_crew.mate_ledger_lock import ledger_lock

    with ledger_lock():
        _record_held(name, member_id, held)


def drop_held(name: str, member_id: str) -> None:
    """Clear Bot *name*'s held restrictions if they are the ones recorded for *member_id*."""
    from kiro_crew.mate_config_migration import _read_ledger
    from kiro_crew.mate_ledger_lock import ledger_lock

    with ledger_lock():
        entries = _read_ledger().get(HELD_KEY)
        entry = entries.get(name) if isinstance(entries, dict) else None
        if isinstance(entry, dict) and str(entry.get("member_id") or "") == member_id:
            _record_held(name, member_id, [])


def _record_held(name: str, member_id: str, held: list[dict[str, str]]) -> None:
    from kiro_crew.mate_config_migration import _read_ledger, _write_ledger

    data = _read_ledger()
    entries = data.get(HELD_KEY)
    entries = entries if isinstance(entries, dict) else {}
    if held:
        entries[name] = {"member_id": member_id, "rules": held}
    else:
        entries.pop(name, None)
    data[HELD_KEY] = dict(sorted(entries.items()))
    _write_ledger(data)


#: The phrase a Bot's call is held under while ``migrated.json`` cannot be read.
UNREADABLE = "the record of its limits could not be read"


def _held_entries() -> dict[str, Any] | None:
    """``held_rules`` from ``migrated.json``: ``{}`` when absent, ``None`` when unreadable."""
    from kiro_crew.config import paths as paths_mod

    path = paths_mod.mate_agents_dir().parent / "migrated.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        # RecursionError: a document nested too deep for the parser reads as
        # unreadable, the same as a malformed one.
        logger.warning("held Bot rules: %s unreadable; every Bot call asks a person", path)
        return None
    if not isinstance(data, dict):
        return None
    entries = data.get(HELD_KEY)
    return entries if isinstance(entries, dict) else {}


def _member_of(agent: str) -> str:
    """The ``member_id`` of the Bot *agent* names now, ``""`` when it names none."""
    if not agent:
        return ""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        binding = KiroCrewConfig.load().agents.get(agent)
    except Exception:  # noqa: BLE001 - an unreadable config names no Bot
        return ""
    return str(getattr(binding, "member_id", "") or "")


def event_tool_names(event: Any) -> set[str]:
    """The built-in tool names the harness itself stamped on *event*, in every spelling.

    kiro-cli's ``toolName``, KAS's ``toolId``, claude's built-in; never the model's
    title. A qualified id (``claude#WebFetch``) is read under the kiro-cli names it
    maps to. Empty for an MCP call (it is named by its server) or one with no name.
    """
    from kiro_crew.agent_sdk.spec_hooks import spec_hook_tool_names
    from kiro_crew.hooks import ToolCall
    from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES

    call = ToolCall.from_event(event)
    if call.mcp_server:
        return set()
    harness_id = str(getattr(event, "harness_tool_id", "") or "")
    stamped = (
        call.mcp_tool,
        *(spec_hook_tool_names(harness_id) or (harness_id,)),
        str(getattr(event, "harness_builtin_tool", "") or ""),
    )
    names: set[str] = set()
    for name in filter(None, stamped):
        names.update((name, KIRO_TOOL_ALIASES.get(name, name)))
    return names


def event_capabilities(event: Any) -> set[str]:
    """The capabilities a permission request *event* speaks about; empty when unknown."""
    from kiro_crew.hooks import ToolCall

    call = ToolCall.from_event(event)
    caps: set[str] = set()
    if call.is_shell or call.command:
        caps.add("shell")
    told = False
    if call.mcp_server:
        caps.add("mcp")
        told = True
    else:
        for spelling in event_tool_names(event):
            if _capability_of_tool(spelling):
                caps.add(_capability_of_tool(spelling))
                told = True
    if call.kind in _KIND_CAPABILITY:
        caps.add(_KIND_CAPABILITY[call.kind])
    if call.kind == "fetch" and not told:
        # A web call whose tool was not told may be a search as well as a fetch.
        caps.add("web_search")
    return caps


def _ledger_names(member_ids: set[str], agent: str = "") -> bool:
    """Whether the unparseable ``migrated.json`` still names one of *member_ids*, or *agent*.

    A damaged file cannot say which limits a Bot has, but its text still
    shows which Bots it recorded, so only those are held. An entry recorded
    with no ``member_id`` is matched by its Bot's name, as :func:`_bot_rules`
    matches it. A file whose bytes cannot be read at all names everyone.
    """
    names = {n for n in (*member_ids, agent) if n}
    if not names:
        return False
    from kiro_crew.config import paths as paths_mod

    try:
        text = (paths_mod.mate_agents_dir().parent / "migrated.json").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return True
    return any(json.dumps(name) in text for name in names)


def _bot_rules(session_key: str, agent: str) -> list[Any]:
    """The held rules recorded for the Bot behind *session_key* (or *agent*).

    While ``migrated.json`` exists but cannot be read, a Bot's one rule is
    :data:`UNREADABLE` under :data:`ANY`. Raises on an unexpected error.
    """
    entries = _held_entries()
    if entries == {}:
        return []
    # Every identity the session is known by: the transcript's record is written
    # by the session it describes, so it may add one but never hide the one this
    # process vouched for or the one the agent name resolves to.
    member_ids: set[str] = set()
    if session_key:
        from kiro_crew.execution_context import (
            read_session_execution,
            read_vouched_session_execution,
        )

        for read in (read_session_execution, read_vouched_session_execution):
            try:
                execution = read(session_key)
            except Exception:  # noqa: BLE001 - an unreadable record names no Bot
                execution = None
            member_ids.add(str(getattr(execution, "member_id", "") or ""))
    # A name is resolved to the Bot it names now, so a Bot made again under a
    # deleted one's name is not held under the deleted one's limits.
    member_ids.add(_member_of(agent))
    member_ids.discard("")
    if entries is None:
        return [{"text": UNREADABLE, "capability": ANY}] if _ledger_names(member_ids, agent) else []
    rules: list[Any] = []
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        recorded = str(entry.get("member_id") or "")
        if (recorded and recorded in member_ids) or (not recorded and agent and name == agent):
            rules.extend(entry.get("rules") or [])
    return rules


#: Capability -> the kiro-cli built-ins whose claude names a claude ``ask`` rule is written for.
_CLAUDE_KIRO_TOOLS = {
    "shell": ("execute_bash",),
    "fs_read": ("fs_read", "grep", "glob"),
    "fs_write": ("fs_write",),
    "web_fetch": ("web_fetch",),
    "web_search": ("web_search",),
    "subagent": ("use_subagent",),
}


def claude_ask_rules(session_key: str, mcp_servers: list[str]) -> list[str]:
    """The claude ``permissions.ask`` rules for the held limits of the Bot behind *session_key*.

    On claude a Bot's limits are never written to its file (:func:`split_policy`
    holds them all), and ``default`` mode runs Read, Grep and Glob inside the
    session's cwd, or a call an allow rule pre-approves, with no
    ``session/request_permission``, so Crew's gate never sees it. An ``ask``
    rule per built-in a held capability covers sends each such call to the
    gate, which asks a person (:func:`held_for`). ``mcp`` asks on each server in
    *mcp_servers*. Empty when nothing is held; never raises.
    """
    from kiro_crew.agent_sdk.claude_worker_tools import CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL

    try:
        rules = _bot_rules(session_key, "")
    except Exception:  # noqa: BLE001 - a session start never breaks on this record
        logger.warning("held Bot rules: could not be read for a claude session", exc_info=True)
        return []
    caps = {str(r.get("capability") or ANY) for r in rules if isinstance(r, dict)} - {NO_CALL}
    if ANY in caps:
        caps = set(_ASKABLE)
    out: list[str] = []
    for cap in sorted(caps):
        for kiro_tool in _CLAUDE_KIRO_TOOLS.get(cap, ()):
            out.extend(CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL.get(kiro_tool, ()))
    if "mcp" in caps:
        from kiro_crew.agent_sdk.claude_worker_tools import claude_mcp_server_rule

        out.extend(claude_mcp_server_rule(name) for name in mcp_servers if name)
    return list(dict.fromkeys(out))


def is_bot_helper_session(session_key: str, agent: str) -> bool:
    """Whether *session_key* is a named helper (``subagent:*``, not a Bot's worker).

    No disk read, so a caller on the event loop can ask it before handing
    :func:`kiro_cli_helper_refusal` to a thread.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    return session_key.startswith("subagent:") and agent != MATE_WORKER_AGENT


def kiro_cli_helper_refusal(session_key: str, agent: str, *, cwd: str | Path | None = None) -> str:
    """Why a helper the Bot behind *session_key* started may not run on Kiro CLI; ``""`` if it may.

    Kiro CLI reads a helper's spec from its own file, so the helper keeps that
    spec's pre-approvals and :func:`held_child_spec` cannot narrow them: a call
    they cover would run with no permission request and never reach
    :func:`held_for`. So a helper whose spec pre-approves something a held limit
    covers is refused (:func:`_pre_approves_held`); one that pre-approves none of
    it starts as before, and its held calls still ask at the gate. A spec that
    cannot be read counts as pre-approving. The Bot's own worker is narrowed
    where its spec is built and its other sessions are not helpers, so both
    still start. Never raises.
    """
    if not is_bot_helper_session(session_key, agent):
        return ""
    try:
        rules = _bot_rules(session_key, "")
    except Exception:  # noqa: BLE001 - a session start never breaks on this record
        logger.warning("held Bot rules: could not be read for a Kiro CLI helper", exc_info=True)
        return ""
    caps = {str(r.get("capability") or ANY) for r in rules if isinstance(r, dict)} - {NO_CALL}
    if not caps or not _pre_approves_held(_kiro_cli_spec(agent, cwd), caps):
        return ""
    return (
        f"The helper {agent or 'agent'} was not started on Kiro CLI. Its Bot has limits "
        "that could not be applied when the Bot moved, and this helper's own settings "
        "would let Kiro CLI run some of those actions without asking you. Start the "
        "helper without choosing Kiro CLI, and it runs where those limits ask you."
    )


def _kiro_cli_spec(agent: str, cwd: str | Path | None) -> dict[str, Any] | None:
    """The spec Kiro CLI loads for *agent*: the workspace's, else the user's; ``None`` if unreadable.

    The next directory is searched only when this one holds no spec for *agent*.
    A spec that is there but cannot be read stops the search: Kiro CLI may still
    load it, grants and all.
    """
    from kiro_crew.agent_sdk.agent_specs import AgentSpecUnreadable, load_agent_spec
    from kiro_crew.config import paths as paths_mod

    dirs = [paths_mod.project_agents_dir(cwd)] if cwd else []
    dirs.append(paths_mod.kiro_agents_dir())
    for agents_dir in dirs:
        try:
            return load_agent_spec(agents_dir, agent)
        except (AgentSpecUnreadable, OSError):
            if _holds_spec_for(agents_dir, agent):
                return None
    return None


def _holds_spec_for(agents_dir: Path, agent: str) -> bool:
    """Whether *agents_dir* has a spec for *agent*: one named for it, or one whose
    text names it (a spec declaring ``name`` wins over the file named for it)."""
    from kiro_crew.agent_spec_format import AGENT_SPEC_SUFFIXES, agent_spec_candidates

    try:
        if any(p.exists() for p in agent_spec_candidates(agents_dir, agent)):
            return True
        if not agents_dir.is_dir():
            return False
        needles = (json.dumps(agent), f"name: {agent}")
        for path in agents_dir.iterdir():
            if path.suffix in AGENT_SPEC_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                if any(needle in text for needle in needles):
                    return True
    except OSError:
        return True
    return False


def _pre_approves_held(spec: dict[str, Any] | None, caps: set[str]) -> bool:
    """Whether *spec* pre-approves a call of *caps*: what :func:`_strip_caps` would take
    away, or a grant-only ``toolsSettings`` key on a held tool. ``None`` (unread) does.

    A held read limit, or one of unknown capability, always does: Kiro CLI reads
    files inside the session folder with no permission request for every agent.
    """
    if spec is None or caps & {"fs_read", ANY}:
        return True
    if _strip_caps(copy_json_document(spec), caps) != spec:
        return True
    from kiro_crew.mate_restriction_carry import _GRANT_ONLY_SETTINGS

    settings = spec.get("toolsSettings")
    for tool, entry in settings.items() if isinstance(settings, dict) else ():
        held = ANY in caps or _tool_capability(str(tool)) in caps
        if held and isinstance(entry, dict) and set(entry) & _GRANT_ONLY_SETTINGS:
            return True
    return False


def claude_holds_mcp(session_key: str) -> bool:
    """Whether a held limit of the Bot behind *session_key* covers MCP calls.

    The ``mcp__<server>`` asks of :func:`claude_ask_rules` name only the servers
    Crew sends, so such a session must also run with ``strictMcpConfig``: a
    server from the user's own claude config would otherwise run unasked.
    Never raises; like :func:`claude_ask_rules`, an error reads as nothing held
    (an unreadable ledger already reads as every call held, in :func:`_bot_rules`).
    """
    try:
        rules = _bot_rules(session_key, "")
    except Exception:  # noqa: BLE001 - a session start never breaks on this record
        logger.warning("held Bot rules: could not be read for a claude session", exc_info=True)
        return False
    caps = {str(r.get("capability") or ANY) for r in rules if isinstance(r, dict)}
    return bool(caps & {ANY, "mcp"})


def _ref_capability(ref: str) -> str:
    """The capability an ``allowedTools`` reference pre-approves, ``""`` when unknown.

    A reference that can pre-approve more than one capability (``*``, a glob,
    ``@builtin``) reads as :data:`ANY`, so it never outlives a held limit.
    """
    if any(c in ref for c in "*?["):
        return ANY
    from kiro_crew.mate_held_scope import named_tool

    if ref.startswith("@builtin"):
        tool = ref.partition("/")[2]
        return (_capability_of_tool(tool) or ANY) if tool else ANY
    if ref.startswith("@"):
        return "mcp"
    # A known tool no capability names is matched by its kiro-cli name
    # (``tool:use_aws`` for ``useAws`` too): a limit held for that tool strips
    # its pre-approval in every spelling, and any limit of unknown
    # capability (``ANY``) still strips every one.
    return _capability_of_tool(ref) or named_tool(ref)


def strip_held_approvals(spec: dict[str, Any], member_id: str, name: str = "") -> dict[str, Any]:
    """*spec* without the pre-approvals of the capabilities Bot *member_id*'s held limits cover.

    A pre-approved call (an ``allowedTools`` entry, a server's ``autoApprove``) runs
    with no permission request on kiro-cli and on KAS, so Crew's gate would never
    see it; without the entry the harness asks, and :func:`held_for` makes that
    ask a person. Applied where the worker spec is built
    (``mate_worker.resolve_worker_spec``), beside the owner's own revocations. A
    limit of unknown capability strips every approval. Never raises.
    """
    try:
        entries = _held_entries()
        if entries == {}:
            return spec
        caps: set[str] = set()
        if entries is None:
            caps.add(ANY)
        else:
            for key, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                recorded = str(entry.get("member_id") or "")
                if (recorded and recorded == member_id) or (not recorded and name and key == name):
                    caps.update(
                        str(r.get("capability") or ANY)
                        for r in entry.get("rules") or []
                        if isinstance(r, dict)
                    )
        caps.discard(NO_CALL)
        return _strip_caps(spec, caps)
    except Exception:  # noqa: BLE001 - the worker always starts
        logger.warning("held Bot rules: approvals not narrowed for %s", member_id, exc_info=True)
        return spec


class HeldLimitsNotApplied(RuntimeError):
    """A child spec of a Bot with held limits could not be narrowed, so it is not started."""


def held_child_spec(spec: dict[str, Any], session_key: str) -> dict[str, Any]:
    """*spec* for a KAS session or sub-agent target running under a Bot with held limits.

    A child the Bot starts (a named ``spawn_run`` agent, an ``availableAgents``
    target) runs under the Bot's identity but on its own agent's spec, so the
    Bot's held capabilities lose that spec's pre-approvals and gain an ``ask``:
    KAS then sends each such call to the gate, which asks a person. *spec* when
    the session's identity holds nothing, or when its record cannot be read
    (as :func:`held_for` reads it). Raises :class:`HeldLimitsNotApplied` only
    when the session's Bot holds limits and *spec* could not be narrowed: the
    child is refused rather than started with its full pre-approvals.
    """
    try:
        rules = _bot_rules(session_key, "") if session_key else []
        caps = {str(r.get("capability") or ANY) for r in rules if isinstance(r, dict)}
        caps.discard(NO_CALL)
    except Exception:  # noqa: BLE001 - the session always starts, as the gate reads it
        logger.warning("held Bot rules: a child spec's record could not be read", exc_info=True)
        return spec
    if not caps:
        return spec
    try:
        from kiro_crew.agent_sdk.kas_tool_view import permissions_block_readable

        out = _strip_caps(copy_json_document(spec), caps)
        perms = out.get("permissions")
        perms = dict(perms) if isinstance(perms, dict) else {}
        raw_rules = perms.get("rules")
        child_rules: list[Any] = list(raw_rules) if isinstance(raw_rules, list) else []
        wanted = [_ask(c) for c in sorted(_ASKABLE if ANY in caps else caps & _ASKABLE)]
        wanted = [a for a in wanted if a not in child_rules]
        # A block KAS cannot read is refused whole, denies included, so each ask
        # is kept only while the block still reads (the count KAS checks is after
        # each long ``match`` is split). An ask with no room is left out: its
        # pre-approval is gone, so the call still asks at Crew's gate.
        added = 0
        for ask in wanted:
            trial = {**perms, "rules": [*child_rules, ask]}
            if not permissions_block_readable(trial):
                break
            child_rules.append(ask)
            added += 1
        if added < len(wanted):
            logger.warning("held Bot rules: %d child asks had no room", len(wanted) - added)
        if added:
            perms["rules"] = child_rules
            out["permissions"] = perms
        return out
    except Exception as exc:  # noqa: BLE001 - any failure refuses the child
        logger.warning("held Bot rules: a child spec was not narrowed", exc_info=True)
        raise HeldLimitsNotApplied(
            "this session's Bot has held limits that could not be applied to the "
            f"agent's spec ({type(exc).__name__}), so it was not started"
        ) from exc


def _strip_caps(spec: dict[str, Any], caps: set[str]) -> dict[str, Any]:
    """*spec* without its pre-approvals and ``permissions`` allows for *caps*."""
    from kiro_crew import mate_approval_revocations as revocations

    if not caps:
        return spec
    refs = [r for r in _strings(spec.get("allowedTools"))]
    servers = spec.get("mcpServers")
    if isinstance(servers, dict):
        for server, entry in servers.items():
            approve = entry.get("autoApprove") if isinstance(entry, dict) else None
            refs.extend(f"@{server}/{t}" for t in _strings(approve))
    held = [r for r in refs if ANY in caps or _ref_capability(r) in caps | {ANY}]
    out = revocations.strip(spec, held)
    if ANY in caps or "subagent" in caps:
        # ``trustedAgents`` runs the named agents with no permission request.
        out = _without_agent_grants(out)
    # Template and file ``permissions`` allows for a held capability go too.
    return _without_allows(out, caps)


#: The subagent ``toolsSettings`` key that lists agents run with no permission request.
_AGENT_GRANT_KEY = "trustedAgents"


def _without_agent_grants(spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* without the :data:`_AGENT_GRANT_KEY` list of its subagent tool's settings."""
    settings = spec.get("toolsSettings")
    if not isinstance(settings, dict):
        return spec
    changed = {}
    for tool, entry in settings.items():
        if (
            isinstance(entry, dict)
            and _AGENT_GRANT_KEY in entry
            and _capability_of_tool(str(tool)) == "subagent"
        ):
            changed[tool] = {k: v for k, v in entry.items() if k != _AGENT_GRANT_KEY}
    if not changed:
        return spec
    return {**spec, "toolsSettings": {**settings, **changed}}


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def held_for(event: Any, *, session_key: str = "", agent: str = "") -> list[str]:
    """The held restrictions of the Bot behind *session_key* (or *agent*) *event* falls under.

    Non-empty means no gate may approve the call without a person. A call whose
    capability cannot be told falls under every held restriction. While
    ``migrated.json`` exists but cannot be read, every call of a Bot it still
    names is held (:data:`UNREADABLE`): what was recorded for it is unknown.
    Never raises. If the record itself cannot be read, nothing is held, so a call
    nobody recorded anything for behaves as before; once a Bot's rules are read,
    any later error holds every one of them.
    """
    try:
        rules = _bot_rules(session_key, agent)
    except Exception:  # noqa: BLE001 - the gate never breaks on this record
        logger.warning("held Bot rules: could not be read for a call", exc_info=True)
        return []
    if not rules:
        return []
    try:
        caps = event_capabilities(event)
        names = event_tool_names(event)
        return [str(r.get("text") or "") for r in rules if _covers(r, caps, names, event)]
    except Exception:  # noqa: BLE001 - on doubt, the Bot's limits hold
        logger.warning("held Bot rules: a call could not be matched; holding it", exc_info=True)
        held = [
            str(r.get("text") or "") or UNREADABLE
            for r in rules
            if not (isinstance(r, dict) and r.get("capability") == NO_CALL)
        ]
        return held or [UNREADABLE]


def _covers(rule: Any, caps: set[str], names: set[str], event: Any) -> bool:
    """Whether held *rule* covers a call of *caps*, named *names* (``mate_held_scope``).

    A call whose capability cannot be told falls under every rule. A ``tool:<name>``
    rule covers that tool's calls; a rule with ``paths`` covers the calls of its
    capability that touch one of them, or whose target cannot be told.
    """
    from kiro_crew.mate_held_scope import TOOL_PREFIX, covers_paths, covers_tool

    if not isinstance(rule, dict):
        return False
    capability = str(rule.get("capability") or ANY)
    if capability == NO_CALL:
        return False
    if capability == ANY or (not caps and not names):
        return True
    if capability.startswith(TOOL_PREFIX):
        return covers_tool(capability, names)
    if not caps:
        # A tool whose capability cannot be told: under every capability's limit.
        return True
    if capability not in caps:
        return False
    paths = rule.get("paths")
    if isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths):
        return covers_paths(paths, event)
    return True


def held_spawn_limits(session_key: str, agent: str = "") -> list[str]:
    """The held ``subagent`` limits of the Bot behind *session_key*, as their texts.

    *agent* is the session's live agent, which names the Bot of a session that
    records none (a channel's), as at the tool gates. Crew's own spawn admission
    reads it for the PARENT session: under one, no auto-approve rung starts a
    sub-agent on its own, and the spawn's prompt is marked held (:func:`held_call`)
    so only a person answers it. Only capability
    ``subagent``: a limit of unknown capability, and a ledger that cannot be
    read, already strip every pre-approval from the worker spec built under
    them, so a spawn tool asks at the tool gate. Blocking file I/O; never raises.
    """
    if not session_key:
        return []
    try:
        rules = _bot_rules(session_key, agent)
    except Exception:  # noqa: BLE001 - the gate never breaks on this record
        logger.warning("held Bot rules: could not be read for a spawn", exc_info=True)
        return []
    return [
        str(r.get("text") or "")
        for r in rules
        if isinstance(r, dict) and r.get("capability") == "subagent"
    ]


def held_sentence(held: list[dict[str, str]], *, kiro_cli: bool = False) -> str:
    """The notice's sentence on what did not apply, in plain words.

    A limit no call falls under (:data:`NO_CALL`) never makes the Bot ask, so it
    is named apart, as one that does not run on the worker. On Kiro CLI
    (*kiro_cli*), reads inside the worker's own folder run without a permission
    request, as they do for any Kiro CLI agent, so a held read limit says so.
    """

    def _texts(items: list[dict[str, str]]) -> str:
        return "; ".join(dict.fromkeys(h["text"] for h in items if h.get("text")))

    asking = [h for h in held if h.get("capability") != NO_CALL]
    stopped = [h for h in held if h.get("capability") == NO_CALL]
    parts = []
    if asking:
        parts.append(
            "some limits from the agent it ran on could not be applied as written ("
            + _texts(asking)
            + "), so it now asks you before any action they cover, even when it would "
            "otherwise go ahead on its own"
        )
        if kiro_cli and any(h.get("capability") in ("fs_read", ANY) for h in asking):
            parts.append(
                "it runs on Kiro CLI, so it still reads files inside its own folder "
                "without asking you"
            )
    if stopped:
        parts.append("these from the agent it ran on do not run on its worker: " + _texts(stopped))
    return "; and ".join(parts)


def asks_for(held: list[dict[str, str]]) -> bool:
    """Whether any of *held* makes the Bot ask a person before a call."""
    return any(h.get("capability") != NO_CALL for h in held)


def owe_held_notice(name: str, held: list[dict[str, str]], *, kiro_cli: bool = False) -> None:
    """Owe Bot *name*'s user one notice, in its chat and the bell, naming *held*.

    For a Bot this start moved: its notice says the limits apply from now on, not
    that earlier work ran without them (``upgrade_notice_held``). The bell note is
    only owed when one of them makes the Bot ask. *kiro_cli*: see :func:`held_sentence`.
    """
    from kiro_crew.mate_upgrade_notice import mark_policy_owed

    mark_policy_owed(name, held_sentence(held, kiro_cli=kiro_cli), held=asks_for(held), fresh=True)


#: The most characters of held limit text a card notice quotes.
_NOTICE_TEXT_CAP = 300


def card_notice(held: list[str]) -> str:
    """The line an approval card shows when held limits *held* (their texts) made it ask.

    It names the limits, so a person under Trust can tell why this call asks. It
    points to no list: no dashboard surface shows held limits yet. The stored text
    came from the old agent's file and may name a secret: the surface that shows it
    redacts it first.
    """
    quoted = "; ".join(dict.fromkeys(t for t in held if t)) or UNREADABLE
    if len(quoted) > _NOTICE_TEXT_CAP:
        quoted = quoted[: _NOTICE_TEXT_CAP - 1] + "…"
    return (
        "Auto-approve not applied: this bot kept a limit from its old settings that "
        f"asks you each time ({quoted})."
    )


#: Set while an unattended turn's approver is asked about a held call
#: (``llm_helpers_parts.tool_approval``): an approver that would answer on its
#: own (an ``auto`` cron, the heartbeat allowlist) reads it and asks a person or
#: refuses instead.
_HELD_CALL: ContextVar[tuple[str, ...]] = ContextVar("mate_held_call", default=())


@contextmanager
def held_call(phrases: list[str]) -> Iterator[None]:
    """Mark the approval asked inside this block as one a person must make."""
    token = _HELD_CALL.set(tuple(phrases))
    try:
        yield
    finally:
        _HELD_CALL.reset(token)


def call_is_held() -> bool:
    """Whether the approval being asked now falls under a held Bot limit."""
    return bool(_HELD_CALL.get())


def current_held() -> list[str]:
    """The held limits the approval being asked now falls under, ``[]`` when none."""
    return list(_HELD_CALL.get())
