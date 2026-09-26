"""Fork lineage of chat sessions, in one place.

A fork copies its source's messages with their ``ts`` intact, and a chat
image's durable copy is keyed by ``ts`` alone, so a fork legitimately renders
the copies registered under any ancestor's session key. Two things depend on
that fact and MUST agree about it:

* the artifact asset endpoint, which serves a copy to its owner or to a session
  descended from the owner and to nobody else;
* the permanent-delete reap, which keeps an ancestor's copies while any
  descendant survives.

Both consult this module. Lineage is read from transcript metadata:
``forked_from`` (the immediate source, written by the fork handler) and
``fork_ancestors`` (the FULL chain, materialized at fork time from the source's
own chain). The materialized chain is what makes ancestry provable after an
intermediate fork has been deleted — its metadata is gone with it, so a walk
over ``forked_from`` alone would stop short; the descendant's own record still
names every ancestor.

Every key is compared as a transcript stem (:func:`fold`): catalog entries are
stems, ``forked_from`` is a live key, and the dashboard spells one session as
``dashboard:<slot>``, ``dashboard_<slot>`` or the bare slot.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from kiro_crew.history import transcript_stem, transcript_stems

_log = logging.getLogger(__name__)

__all__ = [
    "MAX_FORK_ANCESTORS",
    "MAX_ANCESTOR_KEY_CHARS",
    "fold",
    "recorded_chain",
    "admitted_chain",
    "owner_spellings",
    "strip_dashboard_prefix",
    "walk_ancestors",
    "ancestors_of",
    "ancestry_chain",
    "descends_from",
    "materialize_ancestors",
    "legacy_aliases",
]


def strip_dashboard_prefix(key: str) -> str:
    """``key`` without its dashboard transport prefix (``dashboard:`` or one or
    more ``dashboard_``) — the bare slot name."""
    bare = key.removeprefix("dashboard:")
    while bare.startswith("dashboard_"):
        bare = bare[len("dashboard_") :]
    return bare


#: A cron job's tab is named ``cron-<job_id>`` (``cron_inject.py``) while its
#: session key is ``cron:<job_id>`` — and ``transcript_stem`` keeps ``-`` but
#: folds ``:`` to ``_``, so the two spellings do NOT meet at the stem. Image
#: copies are owned by the slot name (``register_images`` receives ``slot.key``)
#: while ``forked_from`` and the catalog carry the session key, so the slot
#: spelling is mapped onto the session here. ``dashboard_slot_key`` in
#: ``chat_utils`` owns the forward mapping for open tabs; this is its inverse
#: for sessions whose tab may be closed or gone.
_CRON_TAB_PREFIX = "cron-"
_CRON_KEY_PREFIXES = ("cron:", "cron_")


def _tab_to_session(bare: str) -> str:
    """A linked tab's name spelled as its session key; other keys unchanged."""
    if bare.startswith(_CRON_TAB_PREFIX):
        return "cron:" + bare[len(_CRON_TAB_PREFIX) :]
    return bare


def owner_spellings(key: str) -> set[str]:
    """Every spelling under which an image copy may record session ``key``.

    The copy carries the bare slot name; the caller usually holds the transcript's
    history key or file stem. Both dashboard forms are returned, plus the tab
    name of a linked (cron) session, whose slot is not a fold of its key.
    """
    bare = strip_dashboard_prefix(key)
    out = {key, bare}
    for prefix in _CRON_KEY_PREFIXES:
        if bare.startswith(prefix):
            out.add(_CRON_TAB_PREFIX + bare[len(prefix) :])
    return out


def fold(key: str) -> str:
    """One canonical spelling of a session for lineage comparison."""
    return transcript_stem(_tab_to_session(strip_dashboard_prefix(key)))


#: Bounds on a persisted ``fork_ancestors`` chain. The chain grows by one
#: entry per fork-of-a-fork, and a session key is a slot name or a channel
#: thread id, so an honest chain is short and every key is short. Transcript
#: metadata is agent-writable, so the bound is enforced at every admission and
#: read: a chain AT or beyond the count bound, or carrying an over-long entry,
#: is treated as UNPROVABLE (:func:`recorded_chain` returns ``None``) rather
#: than truncated, because a truncated chain would silently drop the very
#: ancestors this module exists to prove. Both consumers already fail closed on
#: an unreadable record, so an overflowing one takes the same path: the asset
#: endpoint refuses, the reap keeps the copies.
MAX_FORK_ANCESTORS = 1024
MAX_ANCESTOR_KEY_CHARS = 512


def recorded_chain(meta: Any) -> list[str] | None:
    """The ``fork_ancestors`` chain in ``meta``: ``[]`` when none is recorded,
    ``None`` when the record is over the shared bounds.

    Transcript metadata is user-editable JSON: a value that is not a list (or a
    non-dict ``meta``) is treated as "no chain recorded" so a corrupt record
    degrades to the ``forked_from`` walk instead of raising into a request. An
    oversized record is different — it may be hiding real ancestors — so it is
    reported as unprovable (``None``) and callers fail closed on it.
    """
    if not isinstance(meta, dict):
        return []
    chain = meta.get("fork_ancestors")
    if not isinstance(chain, list):
        return []
    # Bound at the point of retention: nothing is materialized from a record
    # that is already over the count bound, and an over-long entry stops the
    # walk before the rest of the list is touched.
    if len(chain) >= MAX_FORK_ANCESTORS:
        _log.warning("fork_ancestors over bounds (%d entries); treating as unprovable", len(chain))
        return None
    out: list[str] = []
    for a in chain:
        if not a:
            continue
        s = a if isinstance(a, str) else str(a)
        if len(s) > MAX_ANCESTOR_KEY_CHARS:
            _log.warning(
                "fork_ancestors entry over %d chars; treating as unprovable",
                MAX_ANCESTOR_KEY_CHARS,
            )
            return None
        out.append(s)
    return out


def admitted_chain(meta: Any) -> list[str]:
    """The chain to carry on a slot restored from ``meta``: the recorded chain,
    or ``[]`` when none is recorded OR the record is over bounds (the overflow
    is logged by :func:`recorded_chain`; a slot never re-persists it)."""
    return recorded_chain(meta) or []


def walk_ancestors(
    start: str,
    parent_of: Callable[[str], str | None],
    known_of: Callable[[str], Iterable[str]],
) -> set[str]:
    """THE ancestry walk, over folded stems.

    From ``start``, union each visited node's materialized chain (``known_of``)
    and follow its immediate edge (``parent_of``). Cycle-safe by visited set,
    no depth cap. Both the catalog-backed :func:`ancestors_of` and the reap's
    snapshot walk call this, so there is one definition of "ancestor".
    """
    out: set[str] = set()
    visited: set[str] = set()
    cur: str | None = fold(start)
    while cur and cur not in visited:
        visited.add(cur)
        out.update(fold(a) for a in known_of(cur))
        parent = parent_of(cur)
        cur = fold(parent) if parent else None
        if cur:
            out.add(cur)
    out.discard(fold(start))
    return out


def _read_meta(log: Any, session: str) -> tuple[dict, bool]:
    """Metadata for ``session``, trying every spelling the catalog answers to.

    Returns ``({}, False)`` when NO spelling could be read; a readable but empty
    record (a bare slot name that is not a transcript) is skipped in favour of
    one that carries data.
    """
    bare = strip_dashboard_prefix(session)
    readable = False
    found: dict = {}
    spellings = (session, f"dashboard:{bare}", bare, _tab_to_session(bare))
    for spelling in dict.fromkeys(spellings):
        try:
            meta, ok = log.get_metadata_status(spelling)
        except Exception:
            continue
        if ok and isinstance(meta, dict):
            readable = True
            if meta and not found:
                found = meta
    return found, readable


def ancestors_of(log: Any, session: str) -> set[str]:
    """Ancestor stems of ``session``, read from the catalog.

    Combines the materialized ``fork_ancestors`` of each visited node with the
    ``forked_from`` walk (for forks made before the chain was recorded). An
    unreadable link ends the walk there: the result is what is PROVABLE, so a
    caller that needs a positive match fails closed on its own. A chain over the
    shared bounds on ANY visited node makes the whole answer unprovable: the
    result is empty, so no positive match can be drawn from it.
    """
    cache: dict[str, dict] = {}
    overflow = False

    def _meta(stem: str) -> dict:
        if stem not in cache:
            meta, _readable = _read_meta(log, stem)
            cache[stem] = meta
        return cache[stem]

    def _known(stem: str) -> list[str]:
        nonlocal overflow
        chain = recorded_chain(_meta(stem))
        if chain is None:
            overflow = True
            return []
        return chain

    found = walk_ancestors(
        session,
        parent_of=lambda s: (lambda p: str(p) if p else None)(_meta(s).get("forked_from")),
        known_of=_known,
    )
    return set() if overflow else found


def _retain(out: list[str], key: Any, *, what: str) -> bool:
    """Append ``key`` to a chain being built, IF it fits the shared bounds.

    The one place an ancestry entry is retained: ``ancestry_chain`` and
    ``materialize_ancestors`` both go through it, so the count bound and the
    per-entry length bound are checked before any entry is kept, whatever its
    source (a recorded chain, a legacy ``forked_from`` walk, the fork's own
    source key). ``False`` means the chain is full or the entry is over-long;
    the caller stops, and a chain that stopped AT the count bound reads back as
    unprovable (:func:`recorded_chain`), never as silently shorter.
    """
    s = key if isinstance(key, str) else str(key)
    if len(out) >= MAX_FORK_ANCESTORS or len(s) > MAX_ANCESTOR_KEY_CHARS:
        _log.warning(
            "fork_ancestors %s hit the shared bound (%d entries, %d chars); chain is unprovable",
            what,
            len(out),
            len(s),
        )
        return False
    out.append(s)
    return True


def ancestry_chain(log: Any, session_key: str) -> list[str]:
    """The ancestor chain of ``session_key`` as RAW keys, nearest first.

    Used at fork time to materialize a new fork's ``fork_ancestors`` from a
    source that is itself a fork. Prefers the source's own recorded chain; a
    pre-upgrade source (``forked_from`` only) is walked through the catalog so
    the new fork records the FULL chain rather than one link. Stops at an
    unreadable link (records what is provable), and every entry is retained
    through the shared bounds — the legacy walk reads agent-writable
    ``forked_from`` values one row at a time, so the bound is applied per
    append, not on the finished list.
    """
    out: list[str] = []
    seen: set[str] = set()
    cur = session_key
    while cur:
        stem = fold(cur)
        if stem in seen:
            break
        seen.add(stem)
        meta, readable = _read_meta(log, cur)
        if not readable:
            break
        chain = recorded_chain(meta)
        if chain is None:
            # Over bounds: nothing past this link is provable.
            break
        if chain:
            for a in chain:
                if a and fold(a) not in seen:
                    if not _retain(out, a, what="from a recorded chain"):
                        return out
                    seen.add(fold(a))
            break
        parent = meta.get("forked_from")
        if not parent or not _retain(out, parent, what="from a forked_from walk"):
            break
        cur = str(parent)
    return out


def descends_from(log: Any, session: str, owner: str) -> bool:
    """Whether ``session`` is ``owner`` or descends from it. Fails CLOSED when
    the chain cannot be read completely."""
    target = fold(owner)
    if fold(session) == target:
        return True
    # Only a positive match is accepted; an unreadable link contributes nothing.
    return target in ancestors_of(log, session)


def materialize_ancestors(
    source_key: str,
    source_ancestors: list[str] | None,
    *,
    source_slot_key: str | None = None,
) -> list[str]:
    """The ``fork_ancestors`` list for a new fork of ``source_key``: the source
    itself, then (for a linked session) the source TAB's own key, then the
    source's chain, deduplicated, nearest first.

    ``source_slot_key`` is the source slot's ``key`` — the name image copies are
    registered under (``register_images`` receives ``slot.key``). For a
    dashboard-born tab it folds to the same stem as the session key and is
    skipped; for a linked session (a cron run, a task-review tab) it does not,
    and without it the fork could never reach the copies its source's tab owns.

    Every entry is retained through :func:`_retain`: a chain that hits the count
    bound stops there and reads back as unprovable, so a fork that deep gets
    fail-closed lineage rather than a silently shortened one.
    """
    out: list[str] = []
    if not _retain(out, source_key, what="for the fork source"):
        return out
    if source_slot_key and fold(source_slot_key) != fold(source_key):
        if not _retain(out, source_slot_key, what="for the fork source tab"):
            return out
    for a in source_ancestors or ():
        if a and a not in out and not _retain(out, a, what="from the source chain"):
            break
    return out


def legacy_aliases(key: str) -> dict[str, str]:
    """``{legacy stem: canonical stem}`` for a key that may also log under a
    pre-canonical stem (Slack threads predating ``slack:<ts>``)."""
    stems = transcript_stems(key)
    return {s: stems[0] for s in stems[1:]}
