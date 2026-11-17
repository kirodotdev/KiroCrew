"""One walk of a slot's units feeds each of its cold or stale folds, every cell built by the
steps of its own pass in :mod:`kiro_crew.crew_log.projection_slot_memo`."""

from __future__ import annotations

import copy
import logging
from collections.abc import Sequence
from typing import Any

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log import projection_slot_memo as memo
from kiro_crew.crew_log.projection_catalogue import WORKERS_FOLD_NAME, logger
from kiro_crew.crew_log.projection_kernel import Checkpoint, require_name
from kiro_crew.crew_log.projection_readers import _slot_units_for_fold
from kiro_crew.crew_log.store import log_exception_text


def prime_slot_projections(slot: str, names: "Sequence[str]", *, cold: bool = True) -> None:
    """*slot*'s cells *names* (never ``workers``, a merge) over ONE walk per unit list. Any
    other cell, and every cell of a walk that raises, folds alone. Never raises.

    ``cold=False`` shares only the cells resumed from a savepoint: the cold ones are left
    to their own prime, which folds them in the backfill child."""
    groups: "dict[tuple[str, ...], set[str]]" = {}
    for name in names:
        units = _slot_units_for_fold(slot, name) if name != WORKERS_FOLD_NAME else ()
        if units:
            groups.setdefault(tuple(units), set()).add(require_name(name))
    for units, group in groups.items():
        try:
            if len(group) > 1:
                _prime_group(slot, units, sorted(group), share_cold=cold)
        except Exception:
            log_exception_text(logger, logging.DEBUG, "crew log slot %s shared prime failed", slot)


def _prime_group(
    slot: str, units: "tuple[str, ...]", names: "Sequence[str]", *, share_cold: bool
) -> None:
    """:func:`prime_slot_projections` for one unit list. Only joined folds keep their lock.

    A lock is only TAKEN when free, never waited for: a busy cell is another pass's and is
    left out. A held key is in ``_cold_keys`` while held, so a ``cold=False`` read of it
    answers "not ready" at once instead of waiting out this walk."""
    from kiro_crew.crew_log import read_budget

    home = str(data_home())
    marks = memo._unit_marks(units, home)
    if not all(mark.origin is not None for mark in marks):
        return
    held: "dict[str, memo._FoldLock]" = {}
    starts: "dict[str, memo._Admitted | str | None]" = {}
    try:
        for name in names:  # sorted, so two shared primes take the locks in one order
            entry = memo._acquire_fold_lock((home, slot, name))
            if not entry.lock.acquire(blocking=False):
                memo._release_fold_lock((home, slot, name), entry)
                continue
            held[name] = entry
            with memo._slot_memo_guard:
                memo._cold_keys.add((home, slot, name))
            starts[name] = _shared_start(name, units, marks, slot=slot)
        cold = [name for name in held if starts[name] == "cold" and share_cold]
        resumed = {name: at for name, at in starts.items() if isinstance(at, memo._Admitted)}
        for name in list(held):
            if not (len(cold) > 1 and name in cold or len(resumed) > 1 and name in resumed):
                _release_held((home, slot, name), held.pop(name))
        with read_budget.fold_scope(slot, "+".join(held)):
            if len(cold) > 1:
                _prime_joined(slot, home, units, marks, cold)
            if len(resumed) > 1:
                _prime_joined(slot, home, units, marks, list(resumed), resumed)
    finally:
        for name, entry in held.items():
            _release_held((home, slot, name), entry)


def _release_held(key: "tuple[str, str, str]", entry: "memo._FoldLock") -> None:
    with memo._slot_memo_guard:
        memo._cold_keys.discard(key)
    entry.lock.release()  # then drop the claim, as fold_slot_warm_revised does
    memo._release_fold_lock(key, entry)


def _shared_start(
    name: str, units: "tuple[str, ...]", marks: "tuple[memo._UnitMark, ...]", *, slot: str
) -> "memo._Admitted | str | None":
    """How :func:`_fold_slot_warm_locked` would start *name*, read without a walk: ``"cold"``,
    a savepoint beside the newest unit, or ``None`` for a warm cell or an older unit's."""
    with memo._slot_memo_guard:
        if (str(data_home()), slot, name) in memo._slot_memos:
            return None
    admitted = memo._admit_slot_resume(name, units, marks, slot=slot)
    if admitted is not None:
        return admitted
    older = range(1, len(units))
    if any(memo._admit_slot_resume(name, units[:n], marks[:n], slot=slot) for n in older):
        return None
    if memo._admit_backfill(name, units[-1]):
        return memo._admit_slot_resume(name, units, marks, slot=slot) or "cold"
    return "cold"


def _prime_joined(
    slot: str,
    home: str,
    units: "tuple[str, ...]",
    marks: "tuple[memo._UnitMark, ...]",
    names: "list[str]",
    admitted: "dict[str, memo._Admitted] | None" = None,
) -> None:
    """*names* over one walk (cold, or the tail from the lowest *admitted*), then each cell."""
    registry, folds = memo._slot_registry(names, slot)
    before = memo._digest_to_carry(units, marks)
    if admitted is None:
        savers = [memo._save_during_fold(n, units, marks, folds[n], slot=slot) for n in names]

        def save(*at: Any) -> None:
            for saver in savers:
                saver(*at)

        stream = memo._SlotStream(units, save=save)
        memo._walk(folds, lambda: registry.prime(slot, stream))
    else:
        stream = min(admitted.values(), key=lambda at: at.saved.reached).tail
        memo._walk(folds, lambda: memo._prime_resumed(registry, slot, admitted or {}, stream))
    for name, (state, watermark) in registry.cells(slot).items():
        cp = {name: Checkpoint(name=name, last_seq=watermark, state=state)}
        own, _folds = memo._slot_registry((name,), slot, cp)
        key = (home, slot, name)
        if admitted is None:
            memo._fold_slot_cold(
                name, key, units, marks, slot=slot, known=True, primed=(own, stream, before)
            )
            continue
        view = copy.copy(stream)  # its own: the resume may raise ``reached`` to its savepoint's
        view.heights = dict(stream.heights)
        cell = memo._resume_slot_fold(
            name, units, marks, slot=slot, primed=(admitted[name], own, view)
        )
        if cell is not None:
            memo._note_slot_fold_origin("savepoint")
            memo._remember_slot_fold(
                key, memo._persist_slot_fold(name, cell, slot=slot, sampled=marks[-1])
            )
