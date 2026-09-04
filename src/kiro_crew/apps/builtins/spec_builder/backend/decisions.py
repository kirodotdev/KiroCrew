"""Protected decision-ledger operations for Spec Builder."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

from .parsers import (
    _MAX_DECISION_PROMPT,
    _clean_str,
    _decision_fingerprint,
    _decision_key,
    _normalize_spec_state,
    _same_spec_dir,
)
from .repository import (
    _DELETING,
    _INDEX_LOCK,
    _load_index,
    _read_spec_text,
    _verified_spec_dir,
)

logger = logging.getLogger("kirocrew.app.spec-builder")
_DECISIONS_PATH: Path | None = None


def _decisions_path() -> Path:
    """Decisions this backend has already dispatched to an agent.

    Under the security keystone's ``trust/`` directory, NOT in this app's own state
    dir. Two reasons, and the second is why the leaf alone was not enough:

      * the index is agent-writable by design, and this record is the app's promise
        that a decision the user answered cannot be answered again -- so an agent
        able to edit it could erase an entry to re-open a settled decision, or forge
        one to lock a decision the user never answered;
      * gating only the FILE left its parent replaceable. ``workspace/spec-builder``
        is not itself a sensitive path, so one ``ln -s`` or ``mv`` naming the
        directory redirected every read and write this backend makes -- it opens the
        path directly, as keystone writers must. The whole ``trust`` directory is
        gated (it is the SEL trust root), so the parent, the leaf and every shell
        verb naming either are refused.

    This backend opens the path directly, which is how the keystone leaves are always
    written (see the Notes vault registry and the Ops Mission Control policy leaf).
    """
    if _DECISIONS_PATH is not None:
        return _DECISIONS_PATH
    return config_dir() / "trust" / "spec-builder-decisions.json"


async def _aload_index_with_decision_alias_status(
    spec_dir: str,
) -> tuple[dict, bool, bool]:
    """Read index + durable-ledger alias status in one hop. BLOCKING work is off-loop."""

    def _read() -> tuple[dict, bool, bool]:
        with _INDEX_LOCK:
            index = _load_index()
            conflict, ledger_usable = _decision_alias_status_locked(index, spec_dir)
            return index, conflict, ledger_usable

    return await asyncio.to_thread(_read)


def _current_decision(spec_dir: Path, decision_id: str) -> tuple[dict[str, Any] | None, bool]:
    """Return the normalized current decision and whether state was readable.

    An absent decision is different from a decision with no options. A card is a
    snapshot of agent-authored state; once that question disappears, accepting the
    stale card would let it mint a durable answer for an id the agent may later reuse
    for another question. An unreadable state is distinct too, because absence cannot
    be established from a failed read.

    BLOCKING -- reads and normalizes ``.spec-state.json``; call via
    ``asyncio.to_thread``. Normalizes through ``_normalize_spec_state`` rather than
    reading fields raw, so both the fingerprint and offered options are computed from
    exactly what the detail endpoint serves.
    """
    raw_text = _read_spec_text(spec_dir, ".spec-state.json")
    if raw_text is None:
        return None, False
    try:
        state = _normalize_spec_state(json.loads(raw_text))
    except json.JSONDecodeError:
        return None, False
    if state is None:
        return None, False
    for item in (state or {}).get("decisions") or []:
        if isinstance(item, dict) and item.get("id") == decision_id:
            return item, True
    return None, True


# ── recorded decisions ───────────────────────────────────────────────────────
#
# A decision answer is a one-way door. Once an option has been dispatched to the
# agent it is part of the conversation the agent is already acting on, so the
# card must never offer options for that decision again.
#
# The agent-authored state file cannot enforce that, for two reasons that both
# happened in practice:
#
#  * it lags. The turn that writes ``answer`` runs AFTER the message is
#    dispatched, so between the click and that write the card still reads as
#    pending and a second click sends a different answer for a decision the
#    agent already has.
#  * it is the agent's own output. A later state write can re-emit the same
#    decision id with ``answer: null`` -- a re-render of a question already
#    settled -- and the card comes back offering options. A user reading that
#    repeat as a NEW question then "answers" it again and silently reverses
#    their earlier decision.
#
# So the backend keeps a protected record of its own and claims a pending outbox
# entry atomically before dispatching. Concurrent clicks resolve to one answer;
# the detail read locks only a card with the same normalized question fingerprint;
# and a crash before relay leaves an entry the next detail poll can replay.

#: Cap on the ledger. It is per spec and grows only when a decision is answered
#: for the FIRST time, so this is far above any real spec -- it is here so an
#: agent that emits ids in a loop cannot grow the file without bound.
#:
#: There is deliberately NO separate cap on a decision id. The ledger key has to
#: be byte-identical to the id ``_normalize_spec_state`` serves, or the overlay
#: silently misses and the card stays clickable after being answered -- so the id
#: goes through that same ``_clean_str`` (redact + ``_MAX_FIELD``) and nothing
#: else. A tighter cap here was exactly that mismatch for any id over its length.
_MAX_RECORDED = 500

#: Serializes read-modify-write on the decisions file across worker threads, the
#: same discipline ``_INDEX_LOCK`` gives the index.
_DECISIONS_LOCK = threading.Lock()


def _decision_alias_status_locked(index: dict, spec_dir: str) -> tuple[bool, bool]:
    """Return alias conflict and ledger usability for one physical directory.

    BLOCKING -- callers run on a worker thread with ``_INDEX_LOCK`` held. This acquires
    ``_DECISIONS_LOCK`` second, preserving the global lock order. The ledger key
    deliberately stays lexical and independent of mutable filesystem state. A
    pre-existing case alias or a rewrite of the sole indexed spelling therefore cannot
    be reconciled by silently choosing a key; operations that could serve, mint or
    strand an answer fail closed instead.
    """
    with _DECISIONS_LOCK:
        store, usable = _read_decisions()
        conflict = _decision_alias_conflict_in_snapshot(index, store, spec_dir)
    return conflict, usable


def _decision_alias_conflict_in_snapshot(index: dict, store: dict, spec_dir: str) -> bool:
    """Whether an index + protected-ledger snapshot contains a physical alias."""
    key = _decision_key(spec_dir)
    candidate_dirs: list[str] = []
    for meta in index.values():
        if not isinstance(meta, dict):
            continue
        candidate_dirs.append(str(meta.get("spec_dir", "")))
    candidate_dirs.extend(store)
    for other_dir in candidate_dirs:
        if _decision_key(other_dir) == key:
            continue
        if _same_spec_dir(other_dir, spec_dir):
            return True
    return False


def _decision_alias_conflict_locked(index: dict, spec_dir: str) -> bool:
    """Whether one live physical directory has multiple durable ledger keys."""
    conflict, _ledger_usable = _decision_alias_status_locked(index, spec_dir)
    return conflict


def _read_decisions() -> tuple[dict, bool]:
    """Read the whole decisions file. BLOCKING -- call under the lock.

    Returns ``(store, usable)``. ``usable`` is False when a file that EXISTS could
    not be read or parsed, and that distinction decides whether a caller may write:

      * a READ (the detail overlay) fails soft to ``{}`` -- toward answerable,
        never toward a locked card nobody can clear;
      * a WRITE must refuse. Treating an unreadable file as an empty ledger and
        saving over it would erase every other spec's answers and make settled
        decisions answerable again -- a corrupt read must not become a data loss.

    A MISSING file is the ordinary first-run case: empty and writable.
    """
    path = _decisions_path()
    try:
        # ``atomic_write`` emits UTF-8, so reads must not depend on the host's default
        # code page. Otherwise non-ASCII answers could be changed or rejected.
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except (OSError, UnicodeError):
        # Decode errors are not OSError subclasses. Preserve the ``(store, usable)``
        # contract so malformed bytes are reported as an unusable store, not a 500.
        logger.warning("could not read the decision record at %s", path, exc_info=True)
        return {}, False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("the decision record at %s is not valid JSON", path)
        return {}, False
    if not isinstance(data, dict):
        logger.warning("the decision record at %s is not an object", path)
        return {}, False
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}, True


def _save_decisions(store: dict) -> None:
    """Persist the decisions file. Atomic (temp + rename), like every writer here.

    No mkdir of its own: ``atomic_write`` creates the target's parent, which matters
    because the ledger lives under the keystone's ``trust/`` root rather than this
    app's state dir, and that directory may not exist yet on a fresh install.
    """
    atomic_write(_decisions_path(), json.dumps(store, indent=2))


def _decision_entries(store: dict, spec_dir: str) -> dict[str, dict[str, str]]:
    """Normalized durable entries for the spec living in THIS directory.

    A delete clears the record, so a re-import into the same directory legitimately
    starts clean; one into a different directory is a different spec and has its own
    key. Nothing here consults the index, which is what keeps an index rewrite from
    reaching a settled answer: it can change what a name points AT, but it cannot
    replace, erase or move the record for a directory.
    """
    entry = store.get(_decision_key(spec_dir))
    if not isinstance(entry, dict):
        return {}
    answers = entry.get("answers")
    if not isinstance(answers, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for storage_key, raw in answers.items():
        if not isinstance(storage_key, str) or not storage_key:
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("option"), str):
            continue
        status = str(raw.get("status", "final"))
        if status not in ("pending", "relayed", "final"):
            continue
        decision_id = str(raw.get("decision_id", storage_key))
        if not decision_id:
            continue
        out[storage_key] = {
            "decision_id": decision_id,
            "option": str(raw.get("option", "")),
            "fingerprint": str(raw.get("fingerprint", "")),
            "status": status,
            "message": str(raw.get("message", "")),
            "delivery_id": str(raw.get("delivery_id", "")),
        }
    return out


def _apply_recorded_answers(
    spec_state: dict | None, recorded: dict[str, dict[str, str]]
) -> dict | None:
    """Overlay the recorded answers onto agent-authored state, ledger wins.

    A decision this backend has dispatched is reported with that answer and
    ``locked``, whatever the state file says about it -- including a pending
    re-emission of the same id. Decisions the agent has dropped from its state
    file are NOT resurrected: there is no card to lock, and synthesising one
    would put a title on screen that no longer exists anywhere.
    """
    if not recorded or not isinstance(spec_state, dict):
        return spec_state
    decisions = spec_state.get("decisions")
    if not isinstance(decisions, list):
        return spec_state
    for d in decisions:
        if not isinstance(d, dict):
            continue
        decision_id = str(d.get("id", ""))
        fingerprint = _decision_fingerprint(d)
        candidates = [
            entry for entry in recorded.values() if entry.get("decision_id") == decision_id
        ]
        entry = next(
            (entry for entry in candidates if entry.get("fingerprint") == fingerprint),
            next((entry for entry in candidates if not entry.get("fingerprint")), None),
        )
        if entry is None:
            continue
        # Redacted on the way out like every other served value: this path does not
        # go through the state file's own scrub.
        d["answer"] = _clean_str(entry.get("option", ""))
        d["locked"] = True
    return spec_state


#: Outcomes of a decision claim. ``stale`` means the spec's identity moved (or its
#: delete was reserved) while the request was in flight, which the caller reports as
#: a stale client rather than as anything about the decision. ``unreadable`` means
#: the record exists but could not be read, so writing would erase it.
_CLAIM_RECORDED = "recorded"
_CLAIM_PENDING = "pending_delivery"
_CLAIM_TAKEN = "already_answered"
_CLAIM_STALE = "stale"
_CLAIM_FULL = "ledger_full"
_CLAIM_UNREADABLE = "unreadable"
_CLAIM_WRITE_FAILED = "write_failed"
_CLAIM_ALIAS_CONFLICT = "directory_alias_conflict"


def _spec_is_live(index: dict, name: str, *, expect_spec_dir: str, expect_slot_key: str) -> bool:
    """True when *name* is still the same indexed, non-deleting spec.

    The identity check the ledger cannot make for itself: the index is what says
    which directory and creation a name currently means, and whether a delete is
    reserved. Refusing on a reservation matters because a claim that commits while
    the dispatch is refused would lock a decision to an answer the agent never got.

    Takes an index SNAPSHOT rather than reading it, so the caller can hold
    ``_INDEX_LOCK`` across the check and its own write.
    """
    meta = index.get(name)
    if meta is None or meta.get(_DELETING):
        return False
    if str(meta.get("spec_dir", "")) != expect_spec_dir:
        return False
    if expect_slot_key and str(meta.get("slot_key", "")) != expect_slot_key:
        return False
    return True


def _claim_decision_locked(
    name: str,
    decision_id: str,
    option: str,
    expect_spec_dir: str,
    expect_slot_key: str,
    fingerprint: str = "",
    message: str = "",
    delivery_id: str = "",
) -> tuple[str, str]:
    """Liveness check + ledger write as ONE transaction. Returns the claim outcome.

    BLOCKING -- call via ``asyncio.to_thread`` (``_claim_decision`` is the only
    caller). It reads the index synchronously on purpose: the check and the commit
    must be inseparable, because a DELETE reserving between them would have the
    answer recorded for a spec already being torn down. ``_mark_deleting`` writes
    that reservation under ``_INDEX_LOCK``, so holding the same lock here serializes
    the two -- either the reservation is visible and this refuses, or it lands after
    this commit and the delete's own cleanup removes the record.

    Lock ORDER is ``_INDEX_LOCK`` then ``_DECISIONS_LOCK``, everywhere. Nothing
    takes them the other way round, which is what keeps this deadlock-free.
    """
    with _INDEX_LOCK:
        index = _load_index()
        if not _spec_is_live(
            index,
            name,
            expect_spec_dir=expect_spec_dir,
            expect_slot_key=expect_slot_key,
        ):
            return _CLAIM_STALE, ""
        with _DECISIONS_LOCK:
            store, usable = _read_decisions()
            if not usable:
                return _CLAIM_UNREADABLE, ""
            # Alias validation and the write consume this one protected snapshot. A
            # second read could recover after a transient failure and then mint a new
            # lexical key beside an alias the failed read concealed.
            if _decision_alias_conflict_in_snapshot(index, store, expect_spec_dir):
                return _CLAIM_ALIAS_CONFLICT, ""
            # The directory must still verify as ITSELF before anything is recorded
            # under its key. This is the half that keeps the alias-by-spelling hole
            # closed now that _decision_key no longer resolves: an entry whose spec_dir
            # disagrees with realpath is either a directory swapped after indexing or a
            # hand-written index entry spelling one directory two ways, and either way
            # recording under it would mint a second record for documents that already
            # have one -- the alias hole, which is what the directory key exists to
            # close.
            #
            # Refusing on the WRITE side only is deliberate. A read that refused would
            # return "no record", which unlocks a card and hands back the reversal this
            # whole file prevents; reads answer from the lexical key and stay locked.
            if _verified_spec_dir(Path(expect_spec_dir)) is None:
                return _CLAIM_STALE, ""
            answers = _decision_entries(store, expect_spec_dir)
            existing = next(
                (
                    entry
                    for entry in answers.values()
                    if entry.get("decision_id") == decision_id
                    and (
                        not fingerprint
                        or not entry.get("fingerprint", "")
                        or entry.get("fingerprint", "") == fingerprint
                    )
                ),
                None,
            )
            if existing is not None:
                if existing.get("status") in ("pending", "relayed"):
                    held = existing.get("option", "")
                    return (_CLAIM_PENDING if held == option else _CLAIM_TAKEN), held
                return _CLAIM_TAKEN, existing.get("option", "")
            if len(answers) >= _MAX_RECORDED:
                return _CLAIM_FULL, ""
            # HTTP claims are an outbox entry first. A process can exit after this
            # durable write and before the in-memory turn dispatch; a final record at
            # this point would lock the card forever even though the agent never saw the
            # answer. The next detail poll replays pending entries and only then changes
            # the status to final. Direct internal callers omit a delivery id and retain
            # the original one-step final write.
            storage_key = decision_id
            if storage_key in answers:
                storage_key = f"{decision_id}:{fingerprint}"
                collision = 1
                while storage_key in answers:
                    collision += 1
                    storage_key = f"{decision_id}:{fingerprint}:{collision}"
            answers[storage_key] = {
                "decision_id": decision_id,
                "option": option,
                "fingerprint": fingerprint,
                "status": "pending" if delivery_id else "final",
                "message": message[:_MAX_DECISION_PROMPT] if delivery_id else "",
                "delivery_id": delivery_id,
            }
            # Keyed on the directory; `name` is carried for readability only and is
            # never matched on, so a later rename or alias cannot strand the record.
            store[_decision_key(expect_spec_dir)] = {"name": name, "answers": answers}
            try:
                _save_decisions(store)
            except OSError:
                # A full or unwritable data home. Raising here would 500 the request,
                # and a 500 carries no code the client can act on -- so its optimistic
                # lock would stay while nothing was recorded OR dispatched. A named
                # pre-dispatch refusal is the honest answer: nothing happened, and the
                # card re-opens.
                logger.warning("could not record the decision answer for %s", name, exc_info=True)
                return _CLAIM_WRITE_FAILED, ""
            return _CLAIM_RECORDED, ""


async def _claim_decision(
    name: str,
    decision_id: str,
    option: str,
    *,
    expect_spec_dir: str,
    expect_slot_key: str,
    fingerprint: str = "",
    message: str = "",
    delivery_id: str = "",
) -> tuple[str, str]:
    """Record *option* as the answer to *decision_id*, once and only once.

    Returns ``(outcome, recorded_option)``. On ``_CLAIM_TAKEN`` the second value
    is the answer that IS recorded, so the caller can tell the client what the
    agent was actually given instead of a bare refusal.

    One worker-thread hop (see ``_claim_decision_locked``), so two concurrent
    requests for the same decision cannot both observe it unanswered -- the
    double-click case, and the one a client-side lock cannot close.
    """
    return await asyncio.to_thread(
        _claim_decision_locked,
        name,
        decision_id,
        option,
        expect_spec_dir,
        expect_slot_key,
        fingerprint,
        message,
        delivery_id,
    )


def _pending_decisions_locked(spec_dir: str) -> list[dict[str, str]]:
    """Pending delivery records for one spec. BLOCKING -- call off the loop."""
    with _DECISIONS_LOCK:
        store, usable = _read_decisions()
        if not usable:
            return []
        return [
            dict(entry)
            for entry in _decision_entries(store, spec_dir).values()
            if entry.get("status") in ("pending", "relayed")
            and entry.get("fingerprint")
            and entry.get("delivery_id")
            and entry.get("message")
        ]


async def _pending_decisions(spec_dir: str) -> list[dict[str, str]]:
    """Pending decision deliveries, read without blocking the event loop."""
    return await asyncio.to_thread(_pending_decisions_locked, spec_dir)


def _mark_decision_relayed_locked(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Durably record dispatch intent before the model can consume the prompt."""
    with _DECISIONS_LOCK:
        store, usable = _read_decisions()
        if not usable:
            return False
        entries = _decision_entries(store, spec_dir)
        matched = next(
            (
                (storage_key, entry)
                for storage_key, entry in entries.items()
                if entry.get("decision_id") == decision_id
                and entry.get("fingerprint") == fingerprint
                and entry.get("delivery_id") == delivery_id
            ),
            None,
        )
        if matched is None:
            return False
        storage_key, entry = matched
        if entry.get("status") in ("relayed", "final"):
            return True
        entry["status"] = "relayed"
        container = store.get(_decision_key(spec_dir))
        answers = container.get("answers") if isinstance(container, dict) else None
        if not isinstance(answers, dict):
            return False
        answers[storage_key] = entry
        try:
            _save_decisions(store)
        except OSError:
            logger.warning(
                "could not mark decision delivery relayed for %s", spec_dir, exc_info=True
            )
            return False
        return True


async def _mark_decision_relayed(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Persist the pre-model delivery boundary without blocking the event loop."""
    return await asyncio.to_thread(
        _mark_decision_relayed_locked,
        spec_dir,
        decision_id,
        fingerprint,
        delivery_id,
    )


def _restore_decision_pending_locked(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Undo this process's relay boundary when dispatch has not started."""
    with _DECISIONS_LOCK:
        store, usable = _read_decisions()
        if not usable:
            return False
        entries = _decision_entries(store, spec_dir)
        matched = next(
            (
                (storage_key, entry)
                for storage_key, entry in entries.items()
                if entry.get("decision_id") == decision_id
                and entry.get("fingerprint") == fingerprint
                and entry.get("delivery_id") == delivery_id
            ),
            None,
        )
        if matched is None:
            return False
        storage_key, entry = matched
        if entry.get("status") == "pending":
            return True
        if entry.get("status") != "relayed":
            return False
        entry["status"] = "pending"
        container = store.get(_decision_key(spec_dir))
        answers = container.get("answers") if isinstance(container, dict) else None
        if not isinstance(answers, dict):
            return False
        answers[storage_key] = entry
        try:
            _save_decisions(store)
        except OSError:
            logger.warning("could not restore undelivered decision for %s", spec_dir, exc_info=True)
            return False
        return True


async def _restore_decision_pending(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Restore one exact, not-yet-dispatched relay without blocking the event loop."""
    return await asyncio.to_thread(
        _restore_decision_pending_locked,
        spec_dir,
        decision_id,
        fingerprint,
        delivery_id,
    )


def _finalize_decision_locked(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Mark exactly one matching outbox entry final. BLOCKING -- call off-loop."""
    with _DECISIONS_LOCK:
        store, usable = _read_decisions()
        if not usable:
            return False
        entries = _decision_entries(store, spec_dir)
        matched = next(
            (
                (storage_key, entry)
                for storage_key, entry in entries.items()
                if entry.get("decision_id") == decision_id
                and entry.get("fingerprint") == fingerprint
                and entry.get("delivery_id") == delivery_id
            ),
            None,
        )
        if matched is None:
            return False
        storage_key, entry = matched
        if entry.get("status") == "final":
            return True
        entry["status"] = "final"
        container = store.get(_decision_key(spec_dir))
        if not isinstance(container, dict):
            return False
        answers = container.get("answers")
        if not isinstance(answers, dict):
            return False
        answers[storage_key] = entry
        try:
            _save_decisions(store)
        except OSError:
            logger.warning("could not finalize decision delivery for %s", spec_dir, exc_info=True)
            return False
        return True


async def _finalize_decision(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Finalize one replayable delivery without blocking the event loop."""
    return await asyncio.to_thread(
        _finalize_decision_locked, spec_dir, decision_id, fingerprint, delivery_id
    )


def _abandon_pending_decision_locked(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Remove exactly one unconsumed outbox entry. BLOCKING -- call off-loop.

    A pending answer is only valid while the agent-authored question still has the
    fingerprint and offered option it was claimed against. Removing a stale pending
    row is safe because the model has not consumed it; retaining or finalizing it
    would make a later re-emission look answered when the agent never saw the answer.
    """
    with _DECISIONS_LOCK:
        store, usable = _read_decisions()
        if not usable:
            return False
        entries = _decision_entries(store, spec_dir)
        storage_key = next(
            (
                key
                for key, entry in entries.items()
                if entry.get("status") == "pending"
                and entry.get("decision_id") == decision_id
                and entry.get("fingerprint") == fingerprint
                and entry.get("delivery_id") == delivery_id
            ),
            None,
        )
        if storage_key is None:
            return True
        container = store.get(_decision_key(spec_dir))
        answers = container.get("answers") if isinstance(container, dict) else None
        if not isinstance(answers, dict):
            return False
        answers.pop(storage_key, None)
        if not answers:
            store.pop(_decision_key(spec_dir), None)
        try:
            _save_decisions(store)
        except OSError:
            logger.warning(
                "could not abandon stale decision delivery for %s", spec_dir, exc_info=True
            )
            return False
        return True


async def _abandon_pending_decision(
    spec_dir: str, decision_id: str, fingerprint: str, delivery_id: str
) -> bool:
    """Remove one stale, unconsumed delivery without blocking the event loop."""
    return await asyncio.to_thread(
        _abandon_pending_decision_locked,
        spec_dir,
        decision_id,
        fingerprint,
        delivery_id,
    )


def _forget_decisions_locked(spec_dir: str) -> tuple[bool, bool]:
    """Clear the ledger record for a deleted spec's directory.

    Returns ``(ok, still_referenced)``. ``still_referenced`` is what tells the caller
    another indexed name is serving these documents, which decides both this cleanup and
    whether the directory's turn lock may be dropped -- one index read answering both,
    under one lock, rather than two reads that could disagree.

    BLOCKING -- call via ``asyncio.to_thread`` (``_forget_decisions`` is the only
    caller). It reads the index synchronously because the decision it makes depends on
    what the index still says, and splitting that across a hop would only widen the
    window in which the answer changes.

    Lock ORDER is ``_INDEX_LOCK`` then ``_DECISIONS_LOCK``, as everywhere else.
    """
    with _INDEX_LOCK:
        key = _decision_key(spec_dir)
        # The record belongs to the DIRECTORY, so it outlives any one name pointing at
        # it. The doomed name is already out of the index by now, so anything left here
        # is a live spec still serving these documents -- and clearing the record would
        # hand it a clean slate for decisions that are already settled. Keeping the entry
        # is the same cheap residue a failed cleanup leaves.
        if any(
            isinstance(meta, dict) and _same_spec_dir(str(meta.get("spec_dir", "")), spec_dir)
            for meta in _load_index().values()
        ):
            return True, True  # still referenced -- deliberately nothing to do
        with _DECISIONS_LOCK:
            store, usable = _read_decisions()
            if not usable:
                return False, False
            if key not in store:
                return True, False  # nothing recorded -- already in the wanted state
            del store[key]
            _save_decisions(store)
            return True, False


async def _forget_decisions(spec_dir: str) -> tuple[bool, bool]:
    """Drop a deleted spec's answers. Housekeeping, and deliberately best-effort.

    Runs AFTER the index entry is gone, and its failure is not fatal, because of what
    the two residues cost. Clearing the ledger FIRST means a crash before the index
    write leaves a spec that still exists with its settled decisions answerable again
    -- a silent reversal, the one outcome this file exists to prevent. Leaving an entry
    behind costs nothing comparable: it is keyed on the directory, so the only spec that
    can read it again is one serving those same documents, which is who those answers
    were given for. Bounded, too -- ``_MAX_RECORDED`` caps it.

    So a stale entry is at worst a few bytes and at best correct, while an erased one is
    a reversal. One case needed closing on the other side, though: a spec created LATER at
    the same path is not the spec these answers were given for, and would have inherited
    them. Create clears an orphaned record itself, where "the documents are new" is
    observable; see the call beside ``_forget_deleted``. Returns ``(ok,
    still_referenced)``; see ``_forget_decisions_locked``.
    """
    try:
        return await asyncio.to_thread(_forget_decisions_locked, spec_dir)
    except Exception:
        logger.warning("could not clear the decision record for %s", spec_dir, exc_info=True)
        # Unknown whether another name still serves the directory, so claim it does: that
        # keeps the turn lock in place, which is the safe direction for a shared lock.
        return False, True


async def _pending_decision_is_current(spec_dir: str, pending: dict[str, str]) -> bool | None:
    """Return whether a pending answer still names the rendered question.

    ``None`` means the agent-authored state could not be read, which defers delivery
    without deleting the durable row. ``False`` is an established mismatch and lets
    the caller abandon the exact unconsumed claim.
    """
    decision, usable = await asyncio.to_thread(
        _current_decision,
        Path(spec_dir),
        pending.get("decision_id", ""),
    )
    if not usable:
        return None
    if decision is None:
        return False
    if _decision_fingerprint(decision) != pending.get("fingerprint", ""):
        return False
    offered_options = list(decision.get("options") or [])
    return not offered_options or pending.get("option", "") in offered_options
