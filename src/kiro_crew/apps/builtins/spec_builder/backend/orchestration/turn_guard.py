"""Directory serialization and occupancy for turns over one spec.

One ``asyncio.Lock`` per canonical spec directory serializes every Spec Builder
entry point that can start or destroy a turn. The lock alone cannot see a second
index name over the same documents, nor a dashboard chat that starts an app-owned
slot directly, so the occupancy probes here scan every alias, and the final probe
must be the last await before a dispatch publishes its task.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat

from ..parsers import _decision_key, _owns_slot_key, _same_spec_dir
from ..repository import (
    _INDEX_LOCK,
    _load_index,
    _observed_slot_keys_for_dir,
    _slot_key,
    _unindexed_observed_slot_keys,
)
from .execution_state import _exec_loop_active_for_slot

#: One asyncio lock per spec, held across "is a turn running? -> claim pending ->
#: relay -> finalize", and across a DELETE's whole destructive sequence. Every
#: handler that can start or destroy a turn takes it, so the spec cannot be deleted
#: while an answer moves through the outbox.
#:
#: A decision answer must never be QUEUED: Pause clears the queue, so the answer may
#: never arrive. The lock makes the idle check authoritative for Spec Builder entry
#: points, while the pending status makes a process exit before relay recoverable
#: without a compensating delete that could itself fail.
#:
#: The LOOP is stored alongside each lock and compared on every lookup. An
#: ``asyncio.Lock`` binds to the loop that first awaits it, so a module-level
#: registry that outlived a loop would hand back a lock bound to the dead one and
#: raise "is bound to a different event loop" on acquisition -- which is what a
#: second gateway loop in one process (and the test suite) does.
# Keyed by CANONICAL SPEC DIRECTORY (see _turn_lock), never by name.
_TURN_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}

#: macOS ONLY, and the asymmetry is deliberate: ``_decision_key`` already runs
#: ``os.path.normcase``, which lowercases on Windows, so NTFS's case-insensitive
#: default is folded before this flag is ever consulted. Darwin is the one
#: case-insensitive-by-default filesystem ``normcase`` leaves untouched (it is a
#: no-op on POSIX), so it needs the extra fold. Stated because the obvious
#: "Windows is case-insensitive too" patch double-folds for no gain.
_CASE_FOLD_TURN_KEYS = platform_compat.IS_MACOS


def _turn_key(spec_dir: str) -> str:
    """Stable lexical key used only to serialize directory operations.

    Darwin normally preserves case while resolving paths even when its volume treats
    case variants as one directory. Folding there may serialize two distinct directories
    on a case-sensitive Darwin volume, which is safe; the index collision check uses
    ``samefile`` and still admits them. The conservative lock prevents two filesystem-
    equivalent spellings from racing create against create or delete cleanup.

    Windows needs no extra fold here -- ``_decision_key``'s ``normcase`` has already
    lowercased the key and unified the separators.
    """
    key = _decision_key(spec_dir)
    return key.casefold() if _CASE_FOLD_TURN_KEYS else key


def _turn_lock(spec_dir: str) -> asyncio.Lock:
    """The turn-start lock for a spec DIRECTORY on the RUNNING loop, created on first use.

    NORMALIZES its own argument through ``_turn_key`` rather than trusting the caller
    to have done it. That is not defensive habit: ``_decision_key`` applies ``normcase``,
    which lowercases on Windows, and ``_turn_key`` additionally folds Darwin paths so
    the default case-insensitive volume cannot mint two locks for one directory. A raw
    path and a pre-normalized path therefore reach the same dictionary entry. Both
    helpers are pure and lexical, so this remains safe on the event loop.

    Keyed on the directory, not the name, for the same reason the decision ledger is: the
    index can hold several names for one directory, and a per-name lock let two of them
    start turns on the same documents concurrently -- each seeing only its own idle slot,
    so both dispatched. One directory is one turn.

    Safe to build lazily without its own mutex: every caller runs on the event
    loop, and the get-or-create below contains no await.
    """
    loop = asyncio.get_running_loop()
    dir_key = _turn_key(spec_dir)
    entry = _TURN_LOCKS.get(dir_key)
    if entry is not None and entry[0] is loop:
        return entry[1]
    lock = asyncio.Lock()
    _TURN_LOCKS[dir_key] = (loop, lock)
    return lock


def _alias_slots_locked(
    dir_key: str, *, own_slot_key: str, own_name: str = ""
) -> dict[str | None, str]:
    """slot_key -> name for every OTHER indexed spec on this directory.

    ``None`` is an alias whose persisted slot identity is not ownership-valid.
    Such an alias is occupied: its worker may still be running under the
    per-creation key that the agent-writable index does not reveal.

    BLOCKING -- call via ``asyncio.to_thread`` (``_alias_slots`` is the only caller). It
    reads the index and resolves each entry's directory, both filesystem work, which is
    exactly why it does not belong on the loop.

    Excludes the caller's own slot: that one is the same session, where an ordinary
    message is legitimately QUEUED rather than refused. Another name is a different
    session over the same documents, so a turn running under it is a concurrent editor.

    Each alias's key is RESOLVED, never read raw from its entry. index.json is
    agent-writable, so ``meta["slot_key"]`` is attacker-controlled, and trusting it gave
    an alias two ways to make itself invisible to the busy scan: delete the field and the
    entry was skipped for having no key, or copy the caller's key and it was skipped as
    "our own slot". Either way a live concurrent editor read as absent and both agents
    wrote the same spec files. ``_slot_key`` answers from the ownership-validated map
    instead -- a key only survives ``_owns_slot_key`` if it structurally encodes its own
    indexed name -- and falls back to the name-derived form otherwise, which is the same
    key ``_ensure_worker_slot`` would have run that alias under. That fallback is only
    authoritative for a legacy entry, though: if a per-creation key was removed while
    its worker was active, the fallback names a DIFFERENT slot. An ownership-invalid
    entry therefore refuses dispatch instead of guessing which worker owns the files.

    Resolution happens HERE, once and off the loop, rather than in ``_busy_alias``: that
    one runs on the event loop, and one validated resolution per alias is also cheaper
    than re-deriving keys per question asked.
    """
    out: dict[str | None, str] = {}
    own_entry_found = not own_name
    with _INDEX_LOCK:
        index = _load_index()
    for other, meta in index.items():
        if not isinstance(meta, dict):
            continue
        other_dir = str(meta.get("spec_dir", ""))
        persisted = meta.get("slot_key")
        valid_slot = isinstance(persisted, str) and _owns_slot_key(other, persisted)
        slot_key = _slot_key(other) if valid_slot else ""
        if own_name and other == own_name:
            own_entry_found = True
            # The current entry itself may be rewritten while a dispatch awaits this
            # scan. Stop would then derive a different lexical lock key. The process
            # barrier revokes by slot even across that move, and the scan also refuses
            # the stale dispatch regardless of whether both paths still alias.
            if (
                not valid_slot
                or slot_key != own_slot_key
                or _decision_key(other_dir) != _decision_key(dir_key)
            ):
                out[None] = other
            continue
        if not own_name and valid_slot and slot_key == own_slot_key:
            continue
        if not _same_spec_dir(other_dir, dir_key):
            continue
        if not valid_slot:
            out[None] = other
            continue
        out[slot_key] = other
    if not own_entry_found:
        out[None] = "current spec"
    return out


async def _alias_slots(
    dir_key: str, *, own_slot_key: str, own_name: str = ""
) -> dict[str | None, str]:
    """``_alias_slots_locked`` off the event loop."""
    return await asyncio.to_thread(
        _alias_slots_locked,
        dir_key,
        own_slot_key=own_slot_key,
        own_name=own_name,
    )


def _busy_alias(state: Any, aliases: dict[str | None, str]) -> str:
    """The name of an alias that is mid-turn OR holding an armed execution loop, or "".

    A running turn is not the only way an alias occupies these documents. An autonudge
    execution loop (a handoff/build) sits IDLE between its nudge cycles, so asking only
    whether the slot is running right now let an alias with a live loop read as free: the
    other name dispatched, then the loop's timer fired, and two agents wrote the same spec
    files. The loop is as much an occupant as the turn it periodically starts.

    Deliberately on the loop and deliberately in-memory only: the slot registry and the
    nudge registry both live here, so reading them from a worker thread would race the very
    state this is trying to observe. Both questions are answered by slot KEY, and the keys
    arrive already resolved and ownership-validated from ``_alias_slots_locked`` -- so this
    function derives nothing and touches no file. Keeping derivation out of here is the
    point: the by-name ``_exec_loop_active`` would re-derive a key per call, and one
    resolver, off the loop, is what makes every alias key validated the same way.
    """
    if state is None:
        return ""
    for slot_key, other in aliases.items():
        if slot_key is None:
            return other
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        if slot is not None and getattr(slot, "running", False):
            return other
        if _exec_loop_active_for_slot(slot_key):
            return other
    return ""


def _busy_observed_directory_slot(state: Any, dir_key: str, own_slot_key: str) -> str:
    """Return an older authenticated slot still working on this directory."""
    if state is None:
        return ""
    observed_keys = _observed_slot_keys_for_dir(dir_key) | _unindexed_observed_slot_keys()
    for slot_key in observed_keys:
        if slot_key == own_slot_key:
            continue
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        slot_task = getattr(slot, "task", None) if slot is not None else None
        if (
            bool(getattr(slot, "running", False))
            or (slot_task is not None and not slot_task.done())
            or _exec_loop_active_for_slot(slot_key)
        ):
            return slot_key
    return ""


def _alias_turn_snapshot(
    state: Any, aliases: dict[str | None, str]
) -> dict[str, tuple[Any, Any, int]]:
    """Capture each live alias slot and its monotonic turn history on the loop."""
    if state is None:
        return {}
    out: dict[str, tuple[Any, Any, int]] = {}
    for slot_key in aliases:
        if slot_key is None:
            continue
        alias_slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        out[slot_key] = (
            alias_slot,
            getattr(alias_slot, "task", None),
            int(getattr(alias_slot, "_turn_generation", 0)),
        )
    return out


def _alias_turn_started_since(
    state: Any,
    aliases: dict[str | None, str],
    snapshot: dict[str, tuple[Any, Any, int]],
) -> bool:
    """True when any alias published a turn after the serialized busy scan.

    Task identity detects a turn that both started and finished while this request
    awaited filesystem work; checking only ``running`` loses that whole interval.
    A newly discovered alias with any task is likewise ambiguous and fails closed.
    """
    if state is None:
        return False
    for slot_key in aliases:
        if slot_key is None:
            return True
        alias_slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        prior = snapshot.get(slot_key)
        if prior is None:
            if alias_slot is not None and (
                getattr(alias_slot, "task", None) is not None
                or int(getattr(alias_slot, "_turn_generation", 0)) > 0
            ):
                return True
            continue
        prior_slot, prior_task, prior_generation = prior
        if (
            alias_slot is not prior_slot
            or getattr(alias_slot, "task", None) is not prior_task
            or int(getattr(alias_slot, "_turn_generation", 0)) != prior_generation
        ):
            return True
    return False


async def _final_alias_conflict(
    state: Any,
    dir_key: str,
    own_slot_key: str,
    initial_aliases: dict[str | None, str],
    snapshot: dict[str, tuple[Any, Any, int]],
    *,
    own_name: str = "",
) -> str:
    """Return an alias that invalidated a dispatch window, or ``""``.

    This must be the last await on every successful dispatch path. The caller checks
    its own slot and publishes the task synchronously after this returns.
    """
    fresh_aliases = await _alias_slots(dir_key, own_slot_key=own_slot_key, own_name=own_name)
    all_aliases = {**initial_aliases, **fresh_aliases}
    if busy_slot := _busy_observed_directory_slot(state, dir_key, own_slot_key):
        return busy_slot
    if busy_under := _busy_alias(state, all_aliases):
        return busy_under
    if _alias_turn_started_since(state, all_aliases, snapshot):
        return next(iter(all_aliases.values()), "another view")
    return ""


def _slot_is_writing(slot: Any) -> bool:
    """True once a slot has published an in-flight agent turn."""
    task = getattr(slot, "task", None)
    return bool(
        getattr(slot, "running", False)
        or getattr(slot, "_in_stage_execution", False)
        or (task is not None and not task.done())
    )


def _agent_is_writing(request: web.Request, name: str) -> bool:
    """True while this spec's agent turn is in flight.

    Both the editor and the per-task run refuse in that window. The agent writes
    the spec documents itself, so accepting a save mid-turn means one of the two
    writes silently wins -- and the compare-and-swap hash cannot help, because the
    editor's base hash was valid when the turn STARTED. Refusing is the honest
    answer: the user is told to wait rather than told the save succeeded.
    """
    state = request.app.get("state")
    if state is None:
        return False
    slot = state.get_slot(_slot_key(name))
    return _slot_is_writing(slot)
