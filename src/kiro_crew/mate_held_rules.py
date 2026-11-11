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
  grant sends it to the approval card instead, and a surface with no card refuses it.

Every other call behaves as before. Blocking file I/O; never raises from the gate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
        return (_capability_of_tool(tool) or ANY) if tool else ANY
    if matcher.startswith("@") or "/" in matcher:
        return "mcp"
    return _capability_of_tool(matcher) or ANY


def _pieces(policy: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each restriction in *policy*, as ``(capability, the policy holding it alone)``."""
    out: list[tuple[str, dict[str, Any]]] = []
    hooks = policy.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            for entry in entries if isinstance(entries, list) else [entries]:
                out.append((_hook_capability(event, entry), {"hooks": {event: [entry]}}))
    elif isinstance(hooks, list):
        # The list form names each entry's event inside it (``trigger``, or ``event``).
        for entry in hooks:
            event = (
                (entry.get("trigger") or entry.get("event")) if isinstance(entry, dict) else None
            )
            out.append((_hook_capability(event, entry), {"hooks": [entry]}))
    elif "hooks" in policy:
        out.append((ANY, {"hooks": hooks}))
    perms = policy.get("permissions")
    if isinstance(perms, dict) and set(perms) <= {"rules"} and isinstance(perms.get("rules"), list):
        for rule in perms["rules"]:
            cap = rule.get("capability") if isinstance(rule, dict) else None
            cap = cap if isinstance(cap, str) and cap in _KNOWN else ANY
            out.append((cap, {"permissions": {"rules": [rule]}}))
    elif "permissions" in policy:
        out.append((ANY, {"permissions": perms}))
    settings = policy.get("toolsSettings")
    if isinstance(settings, dict):
        for tool, entry in settings.items():
            out.append((_capability_of_tool(str(tool)) or ANY, {"toolsSettings": {tool: entry}}))
    elif "toolsSettings" in policy:
        out.append((ANY, {"toolsSettings": settings}))
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
        held: list[dict[str, str]] = []
        for capability, piece in pieces:
            trial = copy_json_document(out)
            try:
                layer_policy(trial, piece)
            except SpecPolicyUncarried:
                trial = None
            if applies and trial is not None and _lands(base, trial, piece):
                out = trial
                continue
            held.extend({"text": t, "capability": capability} for t in _phrases(base, piece))
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


def record_held(name: str, member_id: str, held: list[dict[str, str]]) -> None:
    """Record *held* as Bot *name*'s restrictions that need a person; none clears it."""
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
    except (OSError, ValueError):
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


def event_capabilities(event: Any) -> set[str]:
    """The capabilities a permission request *event* speaks about; empty when unknown."""
    from kiro_crew.hooks import ToolCall
    from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES

    call = ToolCall.from_event(event)
    caps: set[str] = set()
    if call.is_shell or call.command:
        caps.add("shell")
    told = False
    if call.mcp_server:
        caps.add("mcp")
        told = True
    else:
        # The tool the harness itself stamped (kiro-cli's ``toolName``, KAS's
        # ``toolId``, claude's built-in), never the model's title. A qualified
        # id (``claude#WebFetch``) is read under the kiro-cli names it maps to.
        from kiro_crew.agent_sdk.spec_hooks import spec_hook_tool_names

        harness_id = str(getattr(event, "harness_tool_id", "") or "")
        stamped = (
            call.mcp_tool,
            *(spec_hook_tool_names(harness_id) or (harness_id,)),
            str(getattr(event, "harness_builtin_tool", "") or ""),
        )
        for name in filter(None, stamped):
            for spelling in (name, KIRO_TOOL_ALIASES.get(name, name)):
                if _capability_of_tool(spelling):
                    caps.add(_capability_of_tool(spelling))
                    told = True
    if call.kind in _KIND_CAPABILITY:
        caps.add(_KIND_CAPABILITY[call.kind])
    if call.kind == "fetch" and not told:
        # A web call whose tool was not told may be a search as well as a fetch.
        caps.add("web_search")
    return caps


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
        return [{"text": UNREADABLE, "capability": ANY}] if member_ids else []
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
        out.extend(f"mcp__{name}" for name in mcp_servers if name)
    return list(dict.fromkeys(out))


def _ref_capability(ref: str) -> str:
    """The capability an ``allowedTools`` reference pre-approves, ``""`` when unknown.

    A reference that can pre-approve more than one capability (``*``, a glob,
    ``@builtin``) reads as :data:`ANY`, so it never outlives a held limit.
    """
    if any(c in ref for c in "*?["):
        return ANY
    if ref.startswith("@builtin"):
        tool = ref.partition("/")[2]
        return (_capability_of_tool(tool) or ANY) if tool else ANY
    if ref.startswith("@"):
        return "mcp"
    return _capability_of_tool(ref)


def strip_held_approvals(spec: dict[str, Any], member_id: str, name: str = "") -> dict[str, Any]:
    """*spec* without the pre-approvals of the capabilities Bot *member_id*'s held limits cover.

    A pre-approved call (an ``allowedTools`` entry, a server's ``autoApprove``) runs
    with no permission request on kiro-cli and on KAS, so Crew's gate would never
    see it; without the entry the harness asks, and :func:`held_for` makes that
    ask a person. Applied where the worker spec is built
    (``mate_worker.resolve_worker_spec``), beside the owner's own revocations. A
    limit of unknown capability strips every approval. Never raises.
    """
    from kiro_crew import mate_approval_revocations as revocations

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
        if not caps:
            return spec
        refs = [r for r in _strings(spec.get("allowedTools"))]
        servers = spec.get("mcpServers")
        if isinstance(servers, dict):
            for server, entry in servers.items():
                approve = entry.get("autoApprove") if isinstance(entry, dict) else None
                refs.extend(f"@{server}/{t}" for t in _strings(approve))
        held = [r for r in refs if ANY in caps or _ref_capability(r) in caps | {ANY}]
        # Template and file ``permissions`` allows for a held capability go too.
        return _without_allows(revocations.strip(spec, held), caps)
    except Exception:  # noqa: BLE001 - the worker always starts
        logger.warning("held Bot rules: approvals not narrowed for %s", member_id, exc_info=True)
        return spec


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def held_for(event: Any, *, session_key: str = "", agent: str = "") -> list[str]:
    """The held restrictions of the Bot behind *session_key* (or *agent*) *event* falls under.

    Non-empty means no gate may approve the call without a person. A call whose
    capability cannot be told falls under every held restriction. While
    ``migrated.json`` exists but cannot be read, every call of a Bot is held
    (:data:`UNREADABLE`): what was recorded for it is unknown. Never raises; any
    other error reads as nothing held, so a call nobody recorded anything for
    behaves as before.
    """
    try:
        rules = _bot_rules(session_key, agent)
        if not rules:
            return []
        caps = event_capabilities(event)
        return [
            str(r.get("text") or "")
            for r in rules
            if isinstance(r, dict)
            and r.get("capability") != NO_CALL
            and (r.get("capability") == ANY or not caps or r.get("capability") in caps)
        ]
    except Exception:  # noqa: BLE001 - the gate never breaks on this record
        logger.warning("held Bot rules: could not be read for a call", exc_info=True)
        return []


def held_sentence(held: list[dict[str, str]]) -> str:
    """The notice's sentence on what did not apply, in plain words."""
    texts = list(dict.fromkeys(h["text"] for h in held if h.get("text")))
    return (
        "some limits from the agent it ran on could not be applied as written ("
        + "; ".join(texts)
        + "), so it now asks you before any action they cover, even when it would "
        "otherwise go ahead on its own"
    )


def owe_held_notice(name: str, held: list[dict[str, str]]) -> None:
    """Owe Bot *name*'s user one notice, in its chat and the bell, naming *held*.

    For a Bot this start moved: its notice says the limits apply from now on, not
    that earlier work ran without them (``upgrade_notice_held``).
    """
    from kiro_crew.mate_upgrade_notice import mark_policy_owed

    mark_policy_owed(name, held_sentence(held), held=True, fresh=True)


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
