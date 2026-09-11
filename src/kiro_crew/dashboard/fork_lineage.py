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

from typing import Any, Callable, Iterable

from kiro_crew.history import transcript_stem, transcript_stems

__all__ = [
    "fold",
    "recorded_chain",
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


def recorded_chain(meta: Any) -> list[str]:
    """The ``fork_ancestors`` chain in ``meta``, or ``[]``.

    Transcript metadata is user-editable JSON: a value that is not a list (or a
    non-dict ``meta``) is treated as "no chain recorded" so a corrupt record
    degrades to the ``forked_from`` walk instead of raising into a request.
    """
    if not isinstance(meta, dict):
        return []
    chain = meta.get("fork_ancestors")
    if not isinstance(chain, list):
        return []
    return [str(a) for a in chain if a]


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
    caller that needs a positive match fails closed on its own.
    """
    cache: dict[str, dict] = {}

    def _meta(stem: str) -> dict:
        if stem not in cache:
            meta, _readable = _read_meta(log, stem)
            cache[stem] = meta
        return cache[stem]

    return walk_ancestors(
        session,
        parent_of=lambda s: (lambda p: str(p) if p else None)(_meta(s).get("forked_from")),
        known_of=lambda s: recorded_chain(_meta(s)),
    )


def ancestry_chain(log: Any, session_key: str) -> list[str]:
    """The ancestor chain of ``session_key`` as RAW keys, nearest first.

    Used at fork time to materialize a new fork's ``fork_ancestors`` from a
    source that is itself a fork. Prefers the source's own recorded chain; a
    pre-upgrade source (``forked_from`` only) is walked through the catalog so
    the new fork records the FULL chain rather than one link. Stops at an
    unreadable link (records what is provable).
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
        if chain:
            for a in chain:
                if a and fold(str(a)) not in seen:
                    out.append(str(a))
                    seen.add(fold(str(a)))
            break
        parent = meta.get("forked_from")
        if not parent:
            break
        out.append(str(parent))
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


def materialize_ancestors(source_key: str, source_ancestors: list[str] | None) -> list[str]:
    """The ``fork_ancestors`` list for a new fork of ``source_key``: the source
    itself followed by the source's own chain, deduplicated, nearest first."""
    out: list[str] = [source_key]
    for a in source_ancestors or ():
        if a and a not in out:
            out.append(str(a))
    return out


def legacy_aliases(key: str) -> dict[str, str]:
    """``{legacy stem: canonical stem}`` for a key that may also log under a
    pre-canonical stem (Slack threads predating ``slack:<ts>``)."""
    stems = transcript_stems(key)
    return {s: stems[0] for s in stems[1:]}
