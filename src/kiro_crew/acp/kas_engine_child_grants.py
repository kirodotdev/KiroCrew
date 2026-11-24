"""Which grants of the session's agent a person's own agent gets as an engine sub-agent.

The default agent's engine mount registers the person's own agents beside the
shipped helper (``kas_engine_subagents._held_children``). Run as a sub-agent, such
an agent is governed as kiro-cli governs one: the session agent's grants answer
its calls, its own grants answer nothing, and the denies of both hold. The engine
applies that to its own rules. Kiro Crew's gate applies it to the grants it
answers itself, and this module says for which request and for which servers:

* :func:`named_own_agent`: the registered entry the engine names on one permission
  request, with its parent's and the entries that sit between the two, or
  ``None``. No name, no grant.
* :func:`note_start_frame`, :func:`note_start_request`, :func:`started_by`: which
  sub-agents started the one a request names, read from the engine's own frames
  of each start. Where a link is missing, every registered agent counts as
  between.
* :func:`narrow`: the session agent's grants, stamped on that request, less what
  this agent does not get: a grant on a tool its own rules ask about, and a
  grant on an MCP tool of a server it does not share.
* :func:`holding_parents_servers`: the entry declaring, for each server its file
  sets up as the session agent's file does (:func:`set_up_alike`), the session
  agent's own declaration, so both launch one thing.
* :func:`shared_servers_of`: the MCP servers that entry holds exactly as the
  session's agent does. A grant on an MCP tool reaches the sub-agent only for
  those; a server of the same name that launches anything else is another server.
* :func:`asking_outside`: the entry's own ``ask`` on every MCP call, sparing those
  servers, so the engine answers a call to one as it answers the session agent's.
* :func:`within`: the MCP grant refs that survive for it.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.acp.kas_engine_subagent_target import (
    DIRECT_CHILD_DEPTH,
    KAS_HELPER_AGENT,
    is_mount_deny,
)

logger = logging.getLogger(__name__)


def _rules(entry: Any) -> list[Any]:
    held = entry.get("permissions") if isinstance(entry, dict) else None
    rules = held.get("rules") if isinstance(held, dict) else None
    return rules if isinstance(rules, list) else []


def _entries(registered: Any) -> list[dict[str, Any]]:
    return [a for a in registered if isinstance(a, dict)] if isinstance(registered, list) else []


#: The session handle's attribute holding the starts it heard of (:class:`_Starts`).
_STARTS_ATTR = "_kas_subagent_starts"
#: The fields of a start's permission request that name what it starts.
_FIELD_CONSENT, _FIELD_CAPABILITY, _FIELD_RESOURCE = "consent", "capability", "resource"
_FIELD_TOOL_CALL, _FIELD_TOOL_CALL_ID = "toolCall", "toolCallId"
_CAPABILITY_SUBAGENT = "subagent"


class _Starts:
    """What one session's engine said about each start of a sub-agent.

    ``started`` maps a sub-agent's id to the tool call that started it,
    ``asked`` that tool call to ``(name, id, depth, started name)`` of the
    sub-agent that asked for it. Both are keyed by ids the engine minted, never
    by an agent's name. ``None`` stands for an id the engine spoke of twice with
    different words: nothing is read from it.

    Every id, name and depth is the engine's own word, so each is bounded where it
    is kept, by the bounds of the session's roster of native sub-agents
    (:func:`_bounds`): each map holds at most the roster's count, the oldest going
    first, and a start with any field past its bound is not kept at all. A start
    that is not kept, or has left to make room, is one nobody heard of to
    :func:`started_by`: every registered agent then counts as between.
    ``dropped`` counts those starts, and the first one is logged.
    """

    __slots__ = ("asked", "dropped", "started")

    def __init__(self) -> None:
        self.started: dict[str, str | None] = {}
        self.asked: dict[str, tuple[str, str, int, str] | None] = {}
        self.dropped = 0

    def drop(self, why: str) -> None:
        """Count a start that is not kept, or that left to make room, and say so once."""
        self.dropped += 1
        if self.dropped == 1:
            logger.warning(
                "a sub-agent start was not kept (%s); a sub-agent it leads to is asked "
                "as one whose starter is unknown",
                why,
            )


def _bounds() -> tuple[int, int, int]:
    """``(most starts, longest sub-agent id, longest tool call id or name)``.

    The roster's own: its count of sub-agents, the id length it admits one by, and
    the length it stores a name or a tool call id at. Nothing here is cut to fit:
    a cut id or name could read as another one.
    """
    from kiro_crew.acp.session_handle_native_children import (
        NATIVE_CHILD_LABEL_CAP,
        NATIVE_CHILD_ROSTER_CAP,
    )
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

    return NATIVE_CHILD_ROSTER_CAP, MAX_ACP_SESSION_ID_LEN, NATIVE_CHILD_LABEL_CAP


def _remember(held: dict[str, Any], key: str, value: Any) -> bool:
    """*value* under *key*, the first word kept; two different words leave ``None``.
    Whether the oldest start went to make room."""
    if key in held:
        if held[key] != value:
            held[key] = None
        return False
    full = len(held) >= _bounds()[0]
    if full:
        del held[next(iter(held))]
    held[key] = value
    return full


def _roster_admits(handle: Any, subtask: str) -> bool:
    """Whether the session's roster tracks, or would admit, sub-agent *subtask*.

    The roster's own admission (``_note_native_child``), asked without counting:
    an id of its length, not the session's own, and already tracked or with room
    left this turn. A start of a sub-agent the roster refuses is not kept.
    """
    most, longest, _ = _bounds()
    if len(subtask) > longest or subtask == getattr(handle, "_session_id", None):
        return False
    tracked = getattr(handle, "_native_child_sids", None)
    return not isinstance(tracked, set) or subtask in tracked or len(tracked) < most


def _starts(handle: Any, create: bool = False) -> _Starts | None:
    held = getattr(handle, _STARTS_ATTR, None)
    if not isinstance(held, _Starts) and create:
        held = _Starts()
        setattr(handle, _STARTS_ATTR, held)
    return held if isinstance(held, _Starts) else None


def _asker(params: Any) -> tuple[str, str, int] | None:
    """``(name, id, depth)`` of the sub-agent a permission request names, each checked."""
    from kiro_crew.acp import kas_wire

    kiro = kas_wire.kiro_meta(params) if isinstance(params, dict) else None
    requester = kiro.get(kas_wire.FIELD_REQUESTER) if kiro is not None else None
    subagent = requester.get(kas_wire.FIELD_SUBAGENT) if isinstance(requester, dict) else None
    if not isinstance(subagent, dict):
        return None
    name = subagent.get(kas_wire.FIELD_AGENT_NAME)
    subtask = subagent.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
    depth = subagent.get(kas_wire.FIELD_DEPTH)
    if not isinstance(name, str) or not name or not isinstance(subtask, str) or not subtask:
        return None
    if type(depth) is not int or depth < DIRECT_CHILD_DEPTH:
        return None
    return name, subtask, depth


def note_start_frame(handle: Any, update: Any) -> None:
    """Remember which sub-agent a start's tool call starts.

    *update* is a ``tool_call`` frame the engine marks as a start
    (``kind: agent-subtask``): its ``toolCallId`` is the call that starts a
    sub-agent and its ``agentSubtaskId`` the id of the one it starts. Anything
    else is left alone.
    """
    from kiro_crew.acp import kas_wire

    kiro = kas_wire.kiro_meta(update) if isinstance(update, dict) else None
    if kiro is None or kiro.get(kas_wire.FIELD_KIND) != kas_wire.KIND_AGENT_SUBTASK:
        return
    subtask = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
    call = update.get(_FIELD_TOOL_CALL_ID)
    if not isinstance(subtask, str) or not subtask or not isinstance(call, str) or not call:
        return
    held = _starts(handle, create=True)
    if held is None:
        return
    if len(call) > _bounds()[2] or not _roster_admits(handle, subtask):
        held.drop("an id past its bound, or a sub-agent the roster does not track")
    elif _remember(held.started, subtask, call):
        held.drop("the oldest start made room for a new one")


def note_start_request(handle: Any, params: Any) -> None:
    """Remember which sub-agent asked to start another.

    *params* is a ``session/request_permission``. Only one whose consent is for
    the ``subagent`` capability and whose requester is a sub-agent is kept: the
    tool call it is for, with the asker's name, id and depth and the name it
    asks to start.
    """
    from kiro_crew.acp import kas_wire

    asker = _asker(params)
    kiro = kas_wire.kiro_meta(params) if isinstance(params, dict) else None
    consent = kiro.get(_FIELD_CONSENT) if kiro is not None else None
    if asker is None or not isinstance(consent, dict):
        return
    if consent.get(_FIELD_CAPABILITY) != _CAPABILITY_SUBAGENT:
        return
    resource = consent.get(_FIELD_RESOURCE)
    tool_call = params.get(_FIELD_TOOL_CALL)
    call = tool_call.get(_FIELD_TOOL_CALL_ID) if isinstance(tool_call, dict) else None
    if not isinstance(resource, str) or not resource or not isinstance(call, str) or not call:
        return
    held = _starts(handle, create=True)
    if held is None:
        return
    most, longest_id, longest = _bounds()
    name, subtask, depth = asker
    # A chain is no deeper than the sub-agents the roster can hold.
    if (
        max(len(name), len(resource), len(call)) > longest
        or len(subtask) > longest_id
        or depth > most
    ):
        held.drop("an id, a name or a depth past its bound")
    elif _remember(held.asked, call, (*asker, resource)):
        held.drop("the oldest start made room for a new one")


def started_by(handle: Any, params: Any) -> tuple[str, ...] | None:
    """The sub-agents between the one *params* names and the session's agent, nearest first.

    Read link by link from what the engine said of each start
    (:func:`note_start_frame`, :func:`note_start_request`): the tool call that
    started this sub-agent, the sub-agent that asked for that call, and so on up
    to one the session's own agent started. Each link must fit: the start names
    the sub-agent it started by the name the request carries, and the asker runs
    exactly one level above. Empty for a sub-agent the session's agent started.
    ``None`` when the request names no sub-agent, or any link is missing or does
    not fit: a start nobody was asked about leaves no word of who made it.
    """
    asker = _asker(params)
    if asker is None:
        return None
    name, subtask, depth = asker
    held = _starts(handle)
    chain: list[str] = []
    while depth > DIRECT_CHILD_DEPTH:
        call = held.started.get(subtask) if held is not None else None
        start = held.asked.get(call) if held is not None and call is not None else None
        if start is None or start[3] != name or start[2] != depth - 1:
            return None
        name, subtask, depth = start[0], start[1], start[2]
        chain.append(name)
    return tuple(chain)


def named_own_agent(
    requester: Any, registered: Any, agent: str, starters: Any = None
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """The registered sub-agent the engine says raised a request, *agent*'s entry, and
    the entries that started it, or ``None``.

    *requester* is the ``(name, depth)`` of ``kas_wire.requesting_subagent``. An
    answer is given only when the name is a string, the depth is an integer of
    :data:`~kiro_crew.acp.kas_engine_subagent_target.DIRECT_CHILD_DEPTH` or more,
    and the name is one the mount's deny on *agent*'s entry spares, with an entry
    of that id in *registered*. The session's agent, a name the batch does not
    hold and a request the engine names no one on all answer ``None``.

    At the direct depth the helper answers ``None`` (the caller judges it on its
    own) and the third value is empty: *agent* itself started the sub-agent. Deeper,
    another sub-agent started it, the helper included. *starters* names the ones
    between it and *agent* (:func:`started_by`): the third value is their entries,
    and the answer is ``None`` when one of them is not a name the mount spares
    with an entry in the batch, or their count is not the depth less one. With no
    *starters* the request alone does not say who started it: the third value is
    every other entry the mount spares, any of which may sit above it.

    The caller first asks whether the registration mounted the shipped helper for
    *agent*: only then is that deny the mount's own.
    """
    if not isinstance(requester, tuple) or len(requester) != 2:
        return None
    name, depth = requester
    if not isinstance(name, str) or not name or name == agent:
        return None
    if type(depth) is not int or depth < DIRECT_CHILD_DEPTH:
        return None
    direct = depth == DIRECT_CHILD_DEPTH
    if direct and name == KAS_HELPER_AGENT:
        return None
    batch = _entries(registered)
    parent = next((a for a in batch if agent and a.get("id") == agent), None)
    spared: list[Any] = next((r["exclude"] for r in _rules(parent) if is_mount_deny(r)), [])
    child = next((a for a in batch if a.get("id") == name), None) if name in spared else None
    if child is None or parent is None:
        return None
    if direct:
        return child, parent, []
    if starters is None:
        return child, parent, [a for a in batch if a.get("id") in spared and a is not child]
    if not isinstance(starters, tuple) or len(starters) != depth - DIRECT_CHILD_DEPTH:
        return None
    by_id = {a.get("id"): a for a in batch}
    between = [
        by_id.get(starter) if isinstance(starter, str) and starter in spared else None
        for starter in starters
    ]
    if any(a is None for a in between):
        return None
    return child, parent, [a for a in between if a is not None]


#: The built-ins whose grant also carries the shell's ``allowedCommands`` and the
#: write and read tools' ``allowedPaths``.
_SHELL, _WRITE, _READ = "execute_bash", "fs_write", "fs_read"


def narrow(event: Any, child: dict[str, Any], parent: dict[str, Any], above: Any = ()) -> None:
    """Take from *event* the grants of *parent* that *child* does not get.

    *above* are the entries between the two, for a sub-agent another sub-agent
    started (:func:`named_own_agent`). The engine applies the rules of every
    sub-agent above a call to it: a deny of one refuses the call, an ask of one
    raises the request. So an ``ask`` rule of any of *above* holds back a grant as
    *child*'s own does, and no grant on an MCP tool is kept at all below the first
    level: the servers the ones in between share are not read.

    *event* already carries every grant field of the session's own request. Two
    things come off. A grant on an MCP tool stays only for a server *child* shares
    with *parent* (:func:`within`). And a grant on a built-in stays only when no
    ``ask`` rule of *child*'s own names that built-in's capability, read as the
    projection reads the session agent's own
    (``kas_permissions.withhold_answered_under_authored_rules``): its own "ask me"
    is not answered by its parent's grant, and a request cannot say which rule
    raised it, so every call of that capability is asked for. *child*'s own rules
    are the ones its entry holds and *parent*'s does not; a rule copied from
    *parent* already shaped *parent*'s grants. Its own ``deny`` rules need nothing
    here: the engine refuses what they name before it asks, and an agent with a
    deny only the gate could apply is never registered
    (``kas_subagent_targets._gate_only_limits``). The shell's granted commands and the write and read tools'
    granted paths follow their built-in, and once any of the three is held back
    the deny record keeps its denies and loses the grants it lifted.
    """
    from kiro_crew.acp.kas_permissions import withhold_answered_under_authored_rules
    from kiro_crew.platform.spec_denied_grant import denies_only

    between = [a for a in above if isinstance(a, dict)]
    servers = frozenset() if between else shared_servers_of(child, parent)
    theirs = _rules(parent)
    own = {
        "rules": [
            r
            for entry in (child, *between)
            for r in _rules(entry)
            if isinstance(r, dict) and r.get("effect") == "ask" and r not in theirs
        ]
    }
    # Its own "ask me" on an MCP server is not answered by its parent's grant either:
    # a server its own ask covers grants nothing, in both @-grant fields below.
    servers = servers - _asked_servers(own["rules"], servers)
    granted = event.spec_granted_builtins
    builtins = {ref for ref in granted if isinstance(ref, str) and not ref.startswith("@")}
    event.spec_granted_builtins = withhold_answered_under_authored_rules(builtins, own) | within(
        granted - builtins, servers
    )
    event.spec_hook_held = within(event.spec_hook_held, servers)
    free = withhold_answered_under_authored_rules({_SHELL, _WRITE, _READ}, own)
    if _SHELL not in free:
        event.spec_granted_commands = ()
    if _WRITE not in free:
        event.spec_granted_paths = ()
    if _READ not in free:
        event.spec_granted_read_paths = ()
    if len(free) < 3:
        event.spec_denied_grant = denies_only(event.spec_denied_grant)


#: A server part of an ``mcp`` match that is a pattern, not one server's name.
_GLOB_CHARS = frozenset("*?[]{}!")


def _asked_servers(asks: list[Any], servers: frozenset[str]) -> frozenset[str]:
    """The *servers* an ``ask`` rule of *asks* covers.

    A rule of capability ``mcp`` (or a meta capability that holds it) names a server
    by the part of each ``match`` before its ``/``. A rule with no ``match`` covers
    every server but the ones its ``exclude`` spares whole (``<server>/*``, as
    :func:`asking_outside` writes it); any other ``exclude`` entry spares nothing
    here. A rule whose servers cannot be read exactly (a pattern or a non-string
    where the server goes, a ``match`` or ``exclude`` that is not a list) covers
    every one of *servers*: fail closed. A ``match`` rule's ``exclude`` only narrows
    it, so its servers are covered whole.
    """
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION

    covered: set[str] = set()
    for rule in asks:
        if not isinstance(rule, dict):
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str):
            continue
        expanded = META_CAPABILITY_EXPANSION.get(capability, (capability,))
        if "mcp" not in expanded:
            continue
        matches = rule.get("match")
        if not matches:
            excluded = rule.get("exclude") or []
            if not isinstance(excluded, list):
                return servers
            spared = {
                e[: -len("/*")]
                for e in excluded
                if isinstance(e, str)
                and e.endswith("/*")
                and "/" not in e[: -len("/*")]
                and not _GLOB_CHARS & set(e[: -len("/*")])
            }
            covered |= servers - spared
            continue
        if not isinstance(matches, list):
            return servers
        for pattern in matches:
            if not isinstance(pattern, str):
                return servers
            server = pattern.removeprefix("@").split("/", 1)[0]
            if not server or _GLOB_CHARS & set(server):
                return servers
            covered.add(server)
    return frozenset(covered) & servers


def shared_servers_of(child: Any, parent: Any) -> frozenset[str]:
    """The MCP servers *child*'s entry declares byte for byte as *parent*'s does.

    Two entries that declare a name alike launch the same program, so the
    session agent's grant on that server's tool means the same call. A server the
    session agent's ``excludedTools`` names in any way is left out: the grant
    would reach a tool the session's agent itself does not hold.
    """
    mine = child.get("mcpServers") if isinstance(child, dict) else None
    theirs = parent.get("mcpServers") if isinstance(parent, dict) else None
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return frozenset()
    withheld = _withheld_servers(parent)
    return frozenset(
        name
        for name, declaration in mine.items()
        if isinstance(name, str)
        and name in theirs
        and name not in withheld
        and _same(declaration, theirs[name])
    )


def _same(a: Any, b: Any) -> bool:
    try:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    except (TypeError, ValueError):
        return False


def _withheld_servers(parent: Any) -> set[str]:
    """The servers *parent*'s ``excludedTools`` names in any way."""
    excluded = parent.get("excludedTools") if isinstance(parent, dict) else None
    return {
        ref[1:].split("/", 1)[0]
        for ref in (excluded if isinstance(excluded, list) else ())
        if isinstance(ref, str) and ref.startswith("@")
    }


def set_up_alike(mine: Any, theirs: Any, read: Callable[[Any], Any] = lambda entry: entry) -> bool:
    """Whether a sub-agent's server declaration *mine* sets up what the session agent's *theirs* does.

    The agent-spec rebuild rewrites the default agent's declaration and no other
    agent's: a bare ``command`` becomes the absolute path it resolved to, and the
    entry gains the rebuild's record of both values (``mcp_provenance``). So the
    two are compared like with like, and either match counts: *mine* against
    *theirs* as stored, the record aside, and *mine* against *theirs* as its file
    was written (``mcp_provenance.source_view``, which restores the command only
    while the stored one is still the value the record says the rebuild emitted).
    Never one side as written against the other as rewritten, and never by name.

    A record on *mine* proves nothing, since no rebuild writes that file: such an
    entry matches only *theirs* whole. *read* narrows both sides to the keys that
    count before they are compared; every other key must be equal.
    """
    from kiro_crew.mcp_provenance import DERIVED_KEY, source_view

    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return False
    if DERIVED_KEY in mine:
        return _same(read(mine), read(theirs))
    stored = {k: v for k, v in theirs.items() if k != DERIVED_KEY}
    return _same(read(mine), read(stored)) or _same(read(mine), read(source_view(theirs)))


def holding_parents_servers(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """*child* declaring *parent*'s own entry for each server both set up alike.

    The engine keys a running server by its name and declaration, and a bare
    command in a sub-agent's entry is looked up on the engine's own search path.
    So a server *child*'s file sets up as *parent*'s file does (:func:`set_up_alike`)
    is declared here exactly as *parent*'s entry declares it: both launch one
    thing, the command the rebuild resolved, and :func:`shared_servers_of` reads
    the two as the same server. A server *parent* withholds a tool of keeps
    *child*'s own declaration, as does every server set up any other way. No
    launch file changes hands: a declaration the projection moved behind Kiro
    Crew's launcher names its own agent, so it never matches another agent's.
    """
    mine = child.get("mcpServers")
    theirs = parent.get("mcpServers")
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return child
    withheld = _withheld_servers(parent)
    alike = {
        name: copy.deepcopy(theirs[name])
        for name, declaration in mine.items()
        if isinstance(name, str)
        and name in theirs
        and name not in withheld
        and not _same(declaration, theirs[name])
        and set_up_alike(declaration, theirs[name])
    }
    return {**child, "mcpServers": {**mine, **alike}} if alike else child


#: The ``ask`` ``kas_subagent_targets._asking_for_mcp`` puts on a registered agent.
_ASK_EVERY_MCP_CALL = {"capability": "mcp", "effect": "ask"}


def asking_outside(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """*child* with its ``ask`` on every MCP call sparing the servers it shares.

    The engine answers a sub-agent by its parent's decision unless the
    sub-agent's own is a deny or an ask. For a server both hold alike the parent's
    decision is the right one, so the ask leaves it out; for every other server
    the ask stays. Only the entry's last rule is read, where the projection put
    that ask: an equal rule the agent's own file wrote sits before it and keeps
    asking for every call. An entry whose last rule is anything else, or sharing
    no server, is returned as it is.
    """
    shared = sorted(shared_servers_of(child, parent))
    rules = _rules(child)
    if not shared or not rules or rules[-1] != _ASK_EVERY_MCP_CALL:
        return child
    spared = {**_ASK_EVERY_MCP_CALL, "exclude": [f"{name}/*" for name in shared]}
    return {**child, "permissions": {**child["permissions"], "rules": [*rules[:-1], spared]}}


def within(refs: Any, servers: frozenset[str]) -> frozenset[str]:
    """The ``@server`` and ``@server/tool`` grant *refs* whose server is one of *servers*."""
    return frozenset(
        ref
        for ref in (refs if isinstance(refs, (set, frozenset, tuple, list)) else ())
        if isinstance(ref, str) and ref.startswith("@") and ref[1:].split("/", 1)[0] in servers
    )


__all__ = [
    "asking_outside",
    "holding_parents_servers",
    "named_own_agent",
    "narrow",
    "note_start_frame",
    "note_start_request",
    "set_up_alike",
    "shared_servers_of",
    "started_by",
    "within",
]
