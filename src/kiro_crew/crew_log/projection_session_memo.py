"""A session's folds, kept warm: the session memo, its byte budget, and the read of one
session projection (:func:`read_projection`).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from typing import Any, Final, NamedTuple

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.projection_budget import _UNMEASURED_ROW_BYTES
from kiro_crew.crew_log.projection_catalogue import Projection, logger
from kiro_crew.crew_log.projection_kernel import projection_of, require_name
from kiro_crew.crew_log.projection_registry import EAGER_SESSION_FOLD_NAMES
from kiro_crew.crew_log.projection_session import SessionProjections, fold_session
from kiro_crew.crew_log.projection_slot_memo import (
    _acquire_fold_lock,
    _minted_revision,
    _release_fold_lock,
)
from kiro_crew.crew_log.schema import KIND_SESSION


def read_projection(session_id: str, name: str) -> Projection:
    """One projection for *session_id*, as current as its crew log.

    An EAGER session fold is answered from the warm memo (:func:`fold_session_warm`), so
    it carries the revision a pushed frame for it carries. Any other is folded from the
    savepoint beside the log, or from seq 1 when there is none.
    """
    if require_name(name) in EAGER_SESSION_FOLD_NAMES:
        return fold_session_warm(session_id).projection(name)
    bundle = fold_session(session_id, (name,))
    return bundle.projection(name)


# --------------------------------------------------------------------------- #
# A session's folds, kept warm
# --------------------------------------------------------------------------- #
#
# The session-keyed half of eager folding. The eager folder (:mod:`.eager`) wakes on
# every committed entry, and for the entry's own unit it calls :func:`fold_session_warm`,
# which continues that unit's bundle from where the last call left it. The route that
# serves the panel calls the same function, so a read after a wake is a memo lookup.
#
# THE SAME DISCIPLINE AS THE SLOT MEMO, in each of its three parts. A cell is continued
# only from a bundle :func:`fold_session` itself accepts as ``since=`` (same file, same
# origin, not ahead of the log), so the memo cannot hold a value a cold fold would not
# reach. A revision is minted from the one process-wide counter the slot memo uses
# (:func:`_minted_revision`), so it is monotonic per (session, fold) across a unit
# recreated under the same id and across a resume from disk. And the table is bounded in
# BYTES, least recently advanced first.
#
# THE SAVEPOINT IS STILL BROUGHT FORWARD. ``fold_session`` writes nothing on a read that
# reused ``since=``, because it cannot vouch for the prefix that bundle came from. A memo
# that always passed ``since=`` would therefore leave the disk savepoint where the first
# fold put it for the life of the process, and a restart would replay the whole gap. So a
# pass whose bundle has moved far enough past its savepoint to earn a write
# (``checkpoint.write_is_earned``) folds from the DISK savepoint instead, which replays at
# most that many entries and writes the savepoint back.

#: Default ceiling on the bytes every warm SESSION cell may hold together. 64 MiB.
#:
#: Charged by each fold's SERIALIZED size, unlike the slot table, and that difference is
#: affordable here for a reason the slot table does not have: a session fold's state is
#: flat and capped near a megabyte (see the measured figures in
#: ``docs/system-specs/modules/crew-log-projection.md``), and the fold already COPIES that
#: state for every entry that moves it (``copy_state``). Weighing a CHANGED fold once per
#: pass is the same order of work as one of those copies, so it buys an exact charge for no
#: new cost class. A slot state can be 2.4 GiB, where the same step would cost more than
#: the fold.
DEFAULT_SESSION_FOLD_CACHE_BYTES: Final[int] = 64 * 1024 * 1024

#: The variable an operator lowers the session ceiling with, in BYTES; same rules as
#: :data:`SLOT_FOLD_CACHE_BYTES_ENV`.
SESSION_FOLD_CACHE_BYTES_ENV: Final[str] = "KIROCREW_SESSION_FOLD_CACHE_BYTES"


def session_fold_cache_bytes() -> int:
    """The live ceiling on all warm session cells together, in bytes."""
    raw = os.environ.get(SESSION_FOLD_CACHE_BYTES_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_SESSION_FOLD_CACHE_BYTES


def session_fold_state_bytes(state: Mapping[str, Any]) -> int:
    """What one session fold's *state* weighs: its UTF-8 JSON length.

    The same measure every cell figure in this module is quoted in, so the ceiling and
    the numbers beside it are in one unit. A state that will not serialize is charged
    :data:`_UNMEASURED_ROW_BYTES` rather than raising: the pass storing it must not fail
    over a charge.
    """
    try:
        return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover - every fold state is JSON
        return _UNMEASURED_ROW_BYTES


class _SessionMemo(NamedTuple):
    """One session's warm bundle, its revision per fold, and what each fold weighs."""

    bundle: SessionProjections
    revisions: Mapping[str, int]
    weights: Mapping[str, int]

    @property
    def weight(self) -> int:
        return sum(self.weights.values())


class WarmSession(NamedTuple):
    """What :func:`fold_session_warm` answers.

    ``changed`` names the folds whose revision this pass MINTED, in registry order --
    the ones whose value differs from the last one this process handed out. It is what
    the eager folder publishes, and why an entry that moved only ``status`` costs one
    event rather than seven.
    """

    bundle: SessionProjections
    revisions: Mapping[str, int]
    changed: tuple[str, ...]

    def projection(self, name: str) -> Projection:
        """One fold of the bundle, rendered, carrying its revision."""
        return projection_of(
            self.bundle.checkpoints[require_name(name)], revision=self.revisions.get(name, 0)
        )


_session_memos: "dict[tuple[str, str], _SessionMemo]" = {}
_session_memo_guard = threading.Lock()

#: The fold-lock family a session memo is serialized under. A NUL cannot appear in a
#: registered fold name, so a session key can never share a lock with a slot key.
_SESSION_LOCK_TAG: Final[str] = "\0session"


def fold_session_warm(session_id: str) -> WarmSession:
    """Every eager SESSION fold for *session_id*, continued from this process's memo.

    One call folds them all, because they share one pass over one file -- the property
    :func:`fold_session` exists for -- and the route that serves the panel needs them
    together from the same moment.

    Serialized per session under the fold-lock table the slot memo uses, so the eager
    worker and a route cannot interleave a read of the memo with another pass's store.
    """
    home = str(data_home())
    key = (home, session_id)
    lock_key = (home, session_id, _SESSION_LOCK_TAG)
    entry = _acquire_fold_lock(lock_key)
    try:
        with entry.lock:
            return _fold_session_warm_locked(session_id, key, EAGER_SESSION_FOLD_NAMES)
    finally:
        _release_fold_lock(lock_key, entry)


def _fold_session_warm_locked(
    session_id: str, key: "tuple[str, str]", names: "tuple[str, ...]"
) -> WarmSession:
    """:func:`fold_session_warm`'s body, with this session's lock already held."""
    from kiro_crew.crew_log import checkpoint as savepoints

    with _session_memo_guard:
        held = _session_memos.get(key)
    since = held.bundle if held is not None else None
    if since is not None and savepoints.write_is_earned(since.last_seq, since.saved_seq):
        # Far enough past the disk savepoint to owe it a write, and only a pass that
        # folds the prefix itself may write one. See this section's header.
        since = None
    bundle = fold_session(session_id, names, since=since)
    # A different file, or one whose identity could not be held still: nothing the memo
    # vouched for describes it, so every non-empty fold is new.
    rebuilt = held is None or bundle.origin is None or held.bundle.origin != bundle.origin
    revisions: dict[str, int] = {}
    weights: dict[str, int] = {}
    changed: list[str] = []
    for name in names:
        checkpoint = bundle.checkpoints[name]
        before = None if rebuilt or held is None else held.bundle.checkpoints.get(name)
        previous = 0 if rebuilt or held is None else held.revisions.get(name, 0)
        # IDENTITY FIRST, equality only when it fails. A fold the pass did not move hands
        # back the very object it was given, which is the common case and costs nothing;
        # a pass that resumed from disk hands back an equal object that is not the same
        # one, and minting for it would push a value nothing changed.
        same = before is not None and (
            before.state is checkpoint.state or before.state == checkpoint.state
        )
        if same and previous > 0 and held is not None:
            revisions[name] = previous
            weights[name] = held.weights.get(name, 0)
            continue
        weights[name] = session_fold_state_bytes(checkpoint.state)
        if checkpoint.last_seq <= 0:
            # Nothing folded: an empty value is not worth an order, and 0 tells a
            # consumer there is none.
            revisions[name] = 0
            continue
        revisions[name] = _minted_revision()
        changed.append(name)
    memo = _SessionMemo(bundle=bundle, revisions=revisions, weights=weights)
    if bundle.origin is not None:
        _remember_session_fold(key, memo)
    else:
        # Served, never kept: an identity that could not be held still is one a later
        # pass must not continue from.
        with _session_memo_guard:
            _session_memos.pop(key, None)
    return WarmSession(bundle=bundle, revisions=revisions, changed=tuple(changed))


def _remember_session_fold(key: "tuple[str, str]", memo: _SessionMemo) -> None:
    """Store *memo*, evicting least recently advanced cells until the table fits.

    The rules of :func:`_remember_slot_fold`, for the same reasons: a cell above the
    whole ceiling is not stored (a cold fold costs time, never an answer), and insertion
    order is advance order because a cell is never edited in place.
    """
    ceiling = session_fold_cache_bytes()
    with _session_memo_guard:
        _session_memos.pop(key, None)
        if memo.weight > ceiling:
            logger.debug(
                "crew log session %s weighs %d bytes, above the %d-byte ceiling (%s), so "
                "it is not kept warm",
                key[1],
                memo.weight,
                ceiling,
                SESSION_FOLD_CACHE_BYTES_ENV,
            )
            return
        _session_memos[key] = memo
        held = sum(cell.weight for cell in _session_memos.values())
        while held > ceiling and len(_session_memos) > 1:
            oldest, evicted = next(iter(_session_memos.items()))
            _session_memos.pop(oldest)
            held -= evicted.weight


def forget_session_folds(session_id: str = "") -> None:
    """Drop *session_id*'s warm cell in every home, or every cell when it is empty.

    What a ``session/closed`` does once the closing entry has been folded and pushed:
    the unit will not append again, its savepoint holds the state, and a warm cell would
    be bytes held for a reader that already has the value.
    """
    with _session_memo_guard:
        if not session_id:
            _session_memos.clear()
            return
        for key in [key for key in _session_memos if key[1] == session_id]:
            _session_memos.pop(key, None)


def session_fold_memo_bytes() -> int:
    """What every warm session cell held right now weighs, together."""
    with _session_memo_guard:
        return sum(cell.weight for cell in _session_memos.values())


def slot_of_session(session_id: str) -> str:
    """The slot *session_id*'s crew log belongs to, or ``""`` when unprovable.

    The bridge a SESSION-addressed caller needs to reach a slot-keyed fold. The
    header is the answer rather than a session mapping: it is written once inside
    the fenced tree and never rewritten, so it cannot be made to name another
    conversation's slot by anything that can write the mapping file.
    """
    from kiro_crew.crew_log.store import unit_header_slot

    return unit_header_slot(KIND_SESSION, session_id) or ""
