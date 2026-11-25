"""Hold a KAS sub-agent run to what the agents above it are refused.

A run started with ``spawn_run`` / ``spawn_sub_agents`` on another agent than its
parent's runs that agent's own spec. kiro-cli holds a sub-agent to its parent's
denies and to its own, and a KAS run is held the same way: when its session starts
(``KasHarness.session_extras``), the agent of each session above it is projected the
way that session projects it, and from that entry

* every engine ``deny`` rule (except the ``subagent`` capability, which the spawn
  gate's ``availableAgents`` check owns) joins every entry of the run's batch, and
* the limits only Crew's gate applies travel on the run's own entry under
  :data:`CREW_PARENT_LIMITS_KEY`, for the session handle to stamp on the run's
  requests (``ToolCall.spec_parent_limits``).

Denies only: no ``allow`` or ``ask`` rule, hook, tool list or grant of a parent is
carried, except the host ``ask`` that brings a fetch on a parent's URL-shaped
``web_fetch.blocked`` entry to the gate. The agents above a run are the agents of
the sessions that started it: each run notes its parent as it starts
(:func:`note_parent`), the run's record names its first one, and the walk repeats
for a parent that is itself a run. Each session above holds the run with its own
limits, anchored where it runs: an agent name is not one set of limits. A parent on
the run's own agent adds nothing only when its limits are the run's own, in the
run's own directory.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any

from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp.kas_permissions import (
    _MAX_RULES,
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_SPEC_DENIES_KEY,
    CREW_UNJUDGED_DENIES_KEY,
    CREW_WRITE_ALLOWLIST_KEY,
    _entry_shell_allowlist,
)
from kiro_crew.acp.kas_spec_grants import CREW_SHELL_READONLY_KEY, _entry_shell_readonly
from kiro_crew.platform.spec_denied_grant import SpecParentLimits, denies_only

logger = logging.getLogger(__name__)

#: The key the run's own entry carries its parents' gate-only limits under. Crew-only:
#: ``_dispatch.attach_kas_custom_agents`` drops it with ``CREW_ONLY_ENTRY_KEYS``.
CREW_PARENT_LIMITS_KEY = "_crewParentLimits"
#: The crew-only keys of a projected entry that only Crew's gate applies.
_GATE_KEYS = (
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_SHELL_READONLY_KEY,
    CREW_WRITE_ALLOWLIST_KEY,
    CREW_SPEC_DENIES_KEY,
    CREW_UNJUDGED_DENIES_KEY,
)
#: Where the carried limits name the tools whose deny list the gate cannot read whole.
_UNREAD_KEY = "_unreadDenies"
#: The spec keys a parent's denies are projected from.
_LIMIT_SPEC_KEYS = ("toolsSettings", "permissions", "tools", "allowedTools")
#: How many levels above a run are read; the spawn gate's own depth cap is lower.
_MAX_LEVELS = 8
#: How many run keys keep their noted parents; an evicted one reads its record.
_MAX_NOTED = 4096
#: How many distinct sessions one run key keeps as its parents. A new one past it
#: is never recorded, in place of an earlier one or at all (:func:`note_parent`).
MAX_PARENTS_PER_RUN = 64

#: Run session key -> the session keys that started a run on it, oldest first.
_NOTED: OrderedDict[str, tuple[str, ...]] = OrderedDict()
#: Session key -> the directory that session was bound to when it last started a
#: run, as the gateway knew it then. Read only for a session whose directory the
#: resolver does not know (:func:`_unresolved_dirs`).
_NOTED_DIRS: OrderedDict[str, str] = OrderedDict()
#: Run keys a parent was started from that :func:`note_parent` did not record, so
#: the parents noted for them are not all of them. A KAS run whose chain reaches one
#: is refused (:func:`holding_agents`). Every key here has a row in :data:`_NOTED`
#: and leaves only with it, so the mark is bounded by the notes and never outlived
#: by the parents it says are not all of them.
_INCOMPLETE: set[str] = set()


class ParentLimitsUnreadable(RuntimeError):
    """A parent agent's limits exist or may exist and could not be applied."""


class TooManyParents(ParentLimitsUnreadable):
    """A run's chain holds a key that was started from more distinct sessions than
    :data:`MAX_PARENTS_PER_RUN`, so not every parent above it is known."""


def _too_many_parents() -> TooManyParents:
    return TooManyParents(
        f"This conversation was already continued from {MAX_PARENTS_PER_RUN} different "
        "sessions; Kiro Crew refuses another so the limits of every earlier one keep "
        "holding it. Start a new sub-agent instead."
    )


def note_parent(run_key: str, parent_key: str, parent_dir: str = "") -> None:
    """Record that the session *parent_key* started a run on *run_key*, and
    *parent_dir*, the directory its own session is bound to, when the caller knows it.

    Never raises. A new distinct parent of a run key that already holds
    :data:`MAX_PARENTS_PER_RUN` is not recorded: the key is marked incomplete
    instead, every recorded parent keeps holding it, and a KAS run whose chain
    reaches the key is refused when its session starts (:func:`holding_agents`).
    A run on any other harness reads no parent limits, so it runs as before. A
    parent already recorded is accepted as before.
    """
    if not run_key or not parent_key or run_key == parent_key:
        return
    known = _NOTED.pop(run_key, ())
    past_cap = parent_key not in known and len(known) >= MAX_PARENTS_PER_RUN
    if past_cap:
        _INCOMPLETE.add(run_key)
    # Re-inserted either way, so a key that was just used is the last one evicted.
    _NOTED[run_key] = known if past_cap else tuple(dict.fromkeys((*known, parent_key)))
    while len(_NOTED) > _MAX_NOTED:
        evicted, _parents = _NOTED.popitem(last=False)
        _INCOMPLETE.discard(evicted)
    if parent_dir and not past_cap:
        _NOTED_DIRS.pop(parent_key, None)
        _NOTED_DIRS[parent_key] = parent_dir
        while len(_NOTED_DIRS) > _MAX_NOTED:
            _NOTED_DIRS.popitem(last=False)


def _unresolved_dirs(parent_key: str, run_cwd: str) -> tuple[str, ...]:
    """The directories to read the session on *parent_key* in, for a parent the
    resolver knows no directory for (a sub-agent run started with no ``cwd``).

    ``("",)`` reads it with no directory and anchors its path limits at *run_cwd*,
    the run's: nothing was noted (a process that restarted since), or the two run
    in one folder. Else the noted directory, where its session is bound, and then
    ``""`` so a limit only the read with no directory finds is still carried.
    """
    noted = _NOTED_DIRS.get(parent_key, "")
    if not noted or (run_cwd and os.path.realpath(noted) == os.path.realpath(run_cwd)):
        return ("",)
    return (noted, "")


def _parent_keys(run_key: str) -> tuple[str, ...]:
    """The sessions that started a run on *run_key*: noted ones, then the record's."""
    from kiro_crew.subagent_persistence import read_state, subagent_id_from_conversation_key

    run_id = subagent_id_from_conversation_key(run_key)
    state = read_state(run_id) if run_id else None
    recorded = state.get("parent_session") if isinstance(state, dict) else None
    keys = (*_NOTED.get(run_key, ()), recorded)
    return tuple(dict.fromkeys(k for k in keys if isinstance(k, str) and k))


def holding_agents(run_key: str) -> list[tuple[str, str]]:
    """``(agent, its session key)`` for each session above the run on *run_key*.

    Nearest first, each session once. Two sessions on one agent are both listed: the
    directory or the session can decide which spec a name resolves to, and a path
    limit is anchored where its session runs. Raises :class:`ParentLimitsUnreadable`
    when a parent's record exists and cannot be read, and :class:`TooManyParents`
    when the run's key or a key above it is marked incomplete (:func:`note_parent`).
    """
    from kiro_crew.subagent import ParentRecordUnreadable, _parent_template_for_spawn

    found: list[tuple[str, str]] = []
    seen, level = {run_key}, [run_key]
    for _ in range(_MAX_LEVELS):
        if any(key in _INCOMPLETE for key in level):
            raise _too_many_parents()
        above = [p for key in level for p in _parent_keys(key) if p not in seen]
        for parent in dict.fromkeys(above):
            seen.add(parent)
            try:
                agent = _parent_template_for_spawn(parent)
            except ParentRecordUnreadable as exc:
                raise ParentLimitsUnreadable(
                    "the session that started this sub-agent has a record that could not "
                    "be read, so its agent's limits are unknown and the sub-agent was not "
                    "started"
                ) from exc
            if agent:
                found.append((agent, parent))
        level = list(dict.fromkeys(above))
        if not level:
            break
    return found


def _allows_only(permissions: Any) -> bool:
    """Whether a spec's ``permissions`` value can project no deny: absent, or a block
    of ``rules`` alone in which every rule is a readable ``allow`` or ``ask``."""
    if permissions is None:
        return True
    if not isinstance(permissions, dict) or set(permissions) != {"rules"}:
        return False
    rules = permissions["rules"]
    return isinstance(rules, list) and all(
        isinstance(rule, dict) and rule.get("effect") in ("allow", "ask") for rule in rules
    )


def parent_spec(agent: str, parent_key: str, cwd: str) -> dict[str, Any] | None:
    """The spec the session on *parent_key* runs *agent* from, read from *cwd*;
    ``None`` when no agent file defines it."""
    from kiro_crew.acp.harness.kas import (
        MATE_WORKER_AGENT,
        mate_worker_projection,
        resolve_projected_spec,
    )
    from kiro_crew.apps.agent_launch import read_bound_app_spec
    from kiro_crew.config import paths as paths_mod

    # An app's own agent runs the file its session is bound to, read by its path.
    spec = read_bound_app_spec(parent_key, agent)
    if spec is None:
        try:
            spec, prompt_dir = resolve_projected_spec(
                paths_mod.kiro_agents_dir(), agent, cwd or None, project_hooks=False
            )
        except kas_agents_mod.KasAgentSpecAbsent:
            return None
        if agent == MATE_WORKER_AGENT:
            spec = mate_worker_projection(spec, prompt_dir, parent_key)
    return spec


def _parent_limits(
    agent: str, parent_key: str, cwd: str
) -> tuple[list[Any], list[Any], dict[str, Any]] | None:
    """*agent*'s ``(deny rules, host asks, gate-only limits)``; ``None`` when no agent
    file defines it or its spec sets no ``toolsSettings`` and no ``permissions`` deny."""
    from kiro_crew.acp.harness.kas import MATE_WORKER_AGENT
    from kiro_crew.acp.kas_spec_denies import with_blocked_host_asks, with_unjudged_denies
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    spec = parent_spec(agent, parent_key, cwd)
    if spec is None:
        return None
    limits = {key: spec[key] for key in _LIMIT_SPEC_KEYS if key in spec}
    if not limits.get("toolsSettings") and _allows_only(limits.get("permissions")):
        return None
    entry = kas_agents_mod.to_client_custom_agent(agent, limits, "", session_key=parent_key)
    if agent == MATE_WORKER_AGENT:
        [entry] = with_unjudged_denies([entry], agent, limits)
    held = entry.get("permissions")
    rules = held.get("rules") if isinstance(held, dict) else None
    denies = [
        rule
        for rule in (rules if isinstance(rules, list) else [])
        if isinstance(rule, dict)
        and rule.get("effect") == "deny"
        and rule.get("capability") != "subagent"
    ]
    settings, _ = canonical_tools_settings(limits.get("toolsSettings"))
    asks = with_blocked_host_asks({}, settings).get("rules") or []
    gate = {key: entry[key] for key in _GATE_KEYS if key in entry}
    # The entry records a deny for the gate only beside a grant it held back. A run
    # has grants of its own, so every deny the gate can read is recorded for it.
    read = _gate_read_denies(settings)
    if any(read.values()):
        record = gate.get(CREW_SPEC_DENIES_KEY) or {"builtins": [], "commands": []}
        merged = {k: [*dict.fromkeys([*(record.get(k) or []), *v])] for k, v in read.items()}
        gate[CREW_SPEC_DENIES_KEY] = {**record, **merged}
    unread = _unread_denies(settings, read)
    if unread:
        gate[_UNREAD_KEY] = unread
    return denies, asks, gate


def _unread_denies(settings: Any, read: dict[str, list[str]]) -> list[str]:
    """The kiro-cli tools whose deny list *settings* sets and *read* does not hold:
    an entry the gate cannot read keeps the whole list out (``_checkable``). And
    ``web_fetch`` when its ``blocked`` list holds an entry that is not a plain URL."""
    from kiro_crew.acp.kas_spec_denies import (
        _SHELL_SETTINGS_KEYS,
        _WRITE_SETTINGS_KEYS,
        _web_fetch_lists,
    )

    fields = (
        ("execute_bash", _SHELL_SETTINGS_KEYS, "deniedCommands"),
        ("fs_write", _WRITE_SETTINGS_KEYS, "deniedPaths"),
    )
    if not isinstance(settings, dict):
        return []
    unread = [
        tool
        for tool, keys, field in fields
        if not read[field]
        and any(
            isinstance(settings.get(key), dict) and settings[key].get(field) not in (None, [])
            for key in keys
        )
    ]
    return unread if _web_fetch_lists(settings)[1] else [*unread, "web_fetch"]


def _gate_read_denies(settings: Any) -> dict[str, list[str]]:
    """The ``deniedCommands`` and write ``deniedPaths`` of *settings* the gate reads."""
    from kiro_crew.acp.kas_spec_denies import (
        _SHELL_SETTINGS_KEYS,
        _WRITE_SETTINGS_KEYS,
        _checkable,
    )
    from kiro_crew.platform import allowed_commands, allowed_paths

    return {
        "deniedCommands": _checkable(
            settings, _SHELL_SETTINGS_KEYS, "deniedCommands", allowed_commands.parse_deny
        ),
        "deniedPaths": _checkable(
            settings, _WRITE_SETTINGS_KEYS, "deniedPaths", allowed_paths.parse
        ),
    }


def _is_the_runs_own(limits: Any, cwd: str, agent: str, run_key: str, run_cwd: str) -> bool:
    """Whether *limits*, read in *cwd* for a parent on the run's own *agent*, are the
    limits the run's own spec sets where the run itself runs, *run_cwd*.

    A parent whose directory is not known is anchored at the run's. Limits that
    cannot be compared are not the run's own, so they are carried.
    """
    if cwd and (not run_cwd or os.path.realpath(cwd) != os.path.realpath(run_cwd)):
        return False
    try:
        return _parent_limits(agent, run_key, run_cwd) == limits
    except Exception:  # noqa: BLE001 - the parent's limits then hold the run
        logger.debug("own limits of agent %r could not be read", agent, exc_info=True)
        return False


def hold_to_parents(
    custom_agents: list[dict[str, Any]],
    agent: str,
    session_key: str,
    run_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """*custom_agents* for the run on *session_key*, held to the sessions above it.

    The same list for a session that is not a sub-agent run, for parents whose specs
    deny nothing, and for a parent on the run's own *agent* whose limits are the
    run's own (:func:`_is_the_runs_own`). *run_dir* is the directory the run runs
    in, else the one its record names. Raises :class:`ParentLimitsUnreadable` when a
    parent's limits cannot be read or applied, so the run is not started without
    them.
    """
    from kiro_crew.subagent import parent_project_dirs

    if not session_key.startswith("subagent:"):
        return custom_agents
    run_cwd = str(run_dir or "") or next(iter(parent_project_dirs(session_key)), "")
    added: list[Any] = []
    carried: list[dict[str, Any]] = []
    for parent, parent_key in holding_agents(session_key):
        # Every directory the parent may run in is read, as the spawn gate reads
        # them: which spec a name resolves to can depend on the directory, and a
        # spec found from any of them applies. The first is the parent's live one.
        # Where the resolver knows none, the directory the parent's session was
        # bound to is read first, when one was noted and it is not the run's own.
        read: list[Any] = []
        for cwd in parent_project_dirs(parent_key) or _unresolved_dirs(parent_key, run_cwd):
            try:
                limits = _parent_limits(parent, parent_key, cwd)
            except Exception as exc:  # noqa: BLE001 - any failure refuses the run
                logger.warning("limits of parent agent %r could not be read", parent, exc_info=True)
                raise ParentLimitsUnreadable(
                    f"the limits of agent {parent!r}, under which this sub-agent runs, could "
                    f"not be read ({type(exc).__name__}), so the sub-agent was not started"
                ) from exc
            if limits is None or limits in read:
                continue
            read.append(limits)
            if parent == agent and _is_the_runs_own(limits, cwd, agent, session_key, run_cwd):
                continue
            denies, asks, gate = limits
            added.extend(rule for rule in (*denies, *asks) if rule not in added)
            record = {"agent": parent, "cwd": cwd, "limits": gate}
            # Two sessions with the same limits anchored in the same place hold once.
            if gate and record not in carried:
                carried.append(record)
    if not added and not carried:
        return custom_agents
    out: list[dict[str, Any]] = []
    for entry in custom_agents:
        if isinstance(entry, dict) and added:
            own = entry.get("permissions")
            block = dict(own) if isinstance(own, dict) else {}
            kept = block.get("rules") or []
            block["rules"] = [*kept, *(rule for rule in added if rule not in kept)]
            if len(block["rules"]) > _MAX_RULES:
                raise ParentLimitsUnreadable(
                    f"agent {entry.get('id')!r} would carry more than {_MAX_RULES} permission "
                    "rules with the denies of the agents that started it, so the sub-agent "
                    "was not started"
                )
            entry = {**entry, "permissions": block}
        if isinstance(entry, dict) and carried and entry.get("id") == agent:
            entry = {**entry, CREW_PARENT_LIMITS_KEY: carried}
        out.append(entry)
    return out


def _carried_unread(limits: dict[str, Any]) -> frozenset[str]:
    """The carried :data:`_UNREAD_KEY` tools; a value that is not a list of names
    reads as every tool, so a record Kiro Crew did not write lifts nothing."""
    raw = limits.get(_UNREAD_KEY, [])
    if isinstance(raw, list) and all(isinstance(tool, str) for tool in raw):
        return frozenset(raw)
    return frozenset({"execute_bash", "fs_write", "web_fetch"})


def run_is_held(run_key: str, agent: str, run_dir: str = "") -> bool:
    """Whether :func:`hold_to_parents` adds to, or refuses, the run on *run_key*.

    For the warm pool: a pooled session's batch was built before any run named it,
    so a held run starts its own session instead. *run_dir* is the directory the
    session in question is bound to, the pool's. Blocking, like the hold itself.
    """
    from kiro_crew.acp.kas_parent_grants import runs_on_founding_grants

    batch = [{"id": agent}]
    try:
        held = hold_to_parents(batch, agent, run_key, run_dir) is not batch
    except ParentLimitsUnreadable:
        return True
    return held or runs_on_founding_grants(run_key, agent, run_dir)


def projected_parent_limits(
    custom_agents: Any, agent_id: str, cwd: str
) -> tuple[SpecParentLimits, ...]:
    """The records :func:`hold_to_parents` left on *agent_id*'s registered entry.

    Each parent's path limits are anchored at the directory its own session ran in,
    else at *cwd*, the run's; a relative write target is judged against *cwd*.
    """
    from kiro_crew.acp.kas_spec_denies import _entry_spec_denies, _entry_unjudged_denies
    from kiro_crew.acp.kas_write_allowlist import _entry_write_allowlist

    entry = kas_agents_mod._registered_entry(custom_agents, agent_id)
    raw = entry.get(CREW_PARENT_LIMITS_KEY) if isinstance(entry, dict) else None
    out: list[SpecParentLimits] = []
    for item in raw if isinstance(raw, list) else ():
        limits, anchor = item["limits"], item["cwd"] or cwd
        record = denies_only(_entry_spec_denies(limits, anchor))
        shell = _entry_shell_allowlist(limits)
        write = _entry_write_allowlist(limits, anchor)
        if record is None and CREW_SPEC_DENIES_KEY in limits:
            # A deny record that cannot be read here refuses every shell call and write.
            shell, write = (), ()
        out.append(
            SpecParentLimits(
                agent=item["agent"],
                shell_allowlist=shell,
                shell_readonly=_entry_shell_readonly(limits),
                write_allowlist=write,
                denies=replace(record, cwd=cwd) if record is not None else None,
                unjudged_denies=_entry_unjudged_denies(limits),
                unread_denies=_carried_unread(limits),
            )
        )
    return tuple(out)
