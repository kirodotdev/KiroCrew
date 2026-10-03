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

import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from kiro_crew import hooks
from kiro_crew.artifacts import MAX_SESSION_KEY_CHARS
from kiro_crew.history import transcript_stem, transcript_stems

_log = logging.getLogger(__name__)

__all__ = [
    "MAX_FORK_ANCESTORS",
    "MAX_FORK_ANCESTRY_BYTES",
    "MAX_ANCESTOR_KEY_CHARS",
    "fold",
    "recorded_chain",
    "admitted_chain",
    "owner_spellings",
    "parent_key",
    "MAX_SLOT_NAME_CHARS",
    "MAX_METADATA_LINE_BYTES",
    "metadata_line_within_bound",
    "chain_unprovable",
    "chain_record_for_save",
    "UNPROVABLE_CHAIN_RECORD",
    "UNPROVABLE",
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
#: A task-runner review tab is named ``task-review-<token>`` (``handlers/
#: taskrunner.py``) for the session key ``taskrunner:<task_id>:chat:<token>``;
#: the tab name carries only the token, so the tab spelling cannot be rebuilt
#: from the token alone, but the session key CAN be mapped onto its tab, and
#: the tab is what the image copies are owned by.
_TASK_REVIEW_TAB_PREFIX = "task-review-"
_TASKRUNNER_KEY_PREFIXES = ("taskrunner:", "taskrunner_")
_TASKRUNNER_CHAT_SEGMENTS = (":chat:", "_chat_")


def _taskrunner_review_tab(bare: str) -> str | None:
    """The ``task-review-<token>`` tab of a ``taskrunner:<id>:chat:<token>``
    session key (either separator spelling), or ``None`` for any other key."""
    for prefix in _TASKRUNNER_KEY_PREFIXES:
        if not bare.startswith(prefix):
            continue
        for segment in _TASKRUNNER_CHAT_SEGMENTS:
            _head, sep, token = bare.rpartition(segment)
            if sep and token:
                return _TASK_REVIEW_TAB_PREFIX + token
    return None


def _tab_to_session(bare: str) -> str:
    """A linked tab's name spelled as its session key; other keys unchanged."""
    if bare.startswith(_CRON_TAB_PREFIX):
        return "cron:" + bare[len(_CRON_TAB_PREFIX) :]
    return bare


def owner_spellings(key: str) -> set[str]:
    """Every spelling under which an image copy may record session ``key``.

    The copy carries the bare slot name; the caller usually holds the transcript's
    history key or file stem. Both dashboard forms are returned, plus the tab
    name of a linked session (cron, task-runner review), whose slot is not a
    fold of its key, and the transcript stems a channel-born tab is named by.
    """
    bare = strip_dashboard_prefix(key)
    out = {key, bare}
    # A channel-born tab is named by its transcript STEM (``slack_<ts>`` for the
    # session key ``slack:<ts>``), so the copies it registers are owned by that
    # stem; every stem the key's transcript may occupy is a spelling too.
    out.update(transcript_stems(key))
    out.update(transcript_stems(bare))
    for prefix in _CRON_KEY_PREFIXES:
        if bare.startswith(prefix):
            out.add(_CRON_TAB_PREFIX + bare[len(prefix) :])
    review_tab = _taskrunner_review_tab(bare)
    if review_tab is not None:
        out.add(review_tab)
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
#: Longest transcript metadata line a lineage reader parses. A real metadata
#: line is a few hundred bytes; the transcript directory and the trash are both
#: agent-writable, so a reader that must decode ``fork_ancestors`` bounds the
#: LINE before the JSON decoder materializes it, not the array afterwards. The
#: live-catalog reader (``handlers.sessions._fork_lineage``) and the staged
#: reader (``session_storage._read_lineage_meta``) share this one bound.
MAX_METADATA_LINE_BYTES = 64 * 1024
#: Largest a ``fork_ancestors`` chain may be ONCE SERIALIZED: half the metadata
#: line bound, so the chain can never by itself push the line it is written
#: into over :data:`MAX_METADATA_LINE_BYTES` and the rest of the line keeps the
#: other half for its own fields. The count and per-entry bounds alone do not
#: give that (1024 entries of 256 chars is ~260 KiB), so a deep chain of long
#: keys could be ADMITTED and then written into a line every lineage reader
#: refuses. Admission (:func:`materialize_ancestors`) and the reader
#: (:func:`recorded_chain`) share this bound, so what one admits the other reads.
MAX_FORK_ANCESTRY_BYTES = MAX_METADATA_LINE_BYTES // 2
#: The store's own owner-key bound, so a key admitted here is one the store
#: can record whole and the asset route can match whole.
MAX_ANCESTOR_KEY_CHARS = MAX_SESSION_KEY_CHARS
#: Longest slot NAME admission accepts. Two bounds; the tighter wins. The
#: lineage records a dashboard session as ``dashboard:<slot>``, so the name must
#: leave room for that prefix inside the store's owner-key bound, or a fork of
#: an admitted session would carry a source key over it. And the slot's
#: transcript is the file ``dashboard_<slot>.jsonl`` with a ``.jsonl.lock``
#: sidecar beside it (``history.ConversationLog._lock_path``), so the longest
#: name the transcript stem carries must fit a filesystem name component of
#: :data:`MAX_FILENAME_COMPONENT_BYTES`; the slot key is folded to printable
#: ASCII before it names a file (``state._normalize_slot_key``), so characters
#: are bytes here. A name admitted past that would open ``ENAMETOOLONG`` on
#: the sidecar, and the best-effort save would swallow it on every retry.
DASHBOARD_KEY_PREFIX = "dashboard:"
TRANSCRIPT_STEM_PREFIX = "dashboard_"
TRANSCRIPT_LOCK_SUFFIX = ".jsonl.lock"
MAX_FILENAME_COMPONENT_BYTES = 255
MAX_SLOT_NAME_CHARS = min(
    MAX_SESSION_KEY_CHARS - len(DASHBOARD_KEY_PREFIX),
    MAX_FILENAME_COMPONENT_BYTES - len(TRANSCRIPT_STEM_PREFIX) - len(TRANSCRIPT_LOCK_SUFFIX),
)


#: What a slot whose recorded chain was UNPROVABLE writes back in place of the
#: chain, so the fact survives every save instead of being erased into "no
#: chain recorded": a non-list value, which :func:`recorded_chain` reads as
#: ``None`` again. Present-but-invalid ancestry is never silently normalized.
UNPROVABLE_CHAIN_RECORD: dict[str, bool] = {"unprovable": True}


def recorded_chain(meta: Any) -> list[str] | None:
    """The ``fork_ancestors`` chain in ``meta``: ``[]`` when none is recorded,
    ``None`` when the record is UNPROVABLE.

    Transcript metadata is user-editable JSON. A record that is ABSENT (or a
    non-dict ``meta``) is "no chain recorded" and the ``forked_from`` walk takes
    over. A record that is PRESENT but not a list, or over the shared bounds,
    may be hiding real ancestors, so it is reported as unprovable and every
    caller fails closed on it; :data:`UNPROVABLE_CHAIN_RECORD` is one such value,
    written by a save that must preserve the fact.
    """
    if not isinstance(meta, dict):
        return []
    if "fork_ancestors" not in meta or meta["fork_ancestors"] is None:
        return []
    chain = meta["fork_ancestors"]
    if not isinstance(chain, list):
        _log.warning("fork_ancestors is present but not a list; treating as unprovable")
        return None
    # Bound at the point of retention: nothing is materialized from a record
    # that is already over the count bound, and an over-long entry stops the
    # walk before the rest of the list is touched.
    if len(chain) >= MAX_FORK_ANCESTORS:
        _log.warning("fork_ancestors over bounds (%d entries); treating as unprovable", len(chain))
        return None
    out: list[str] = []
    for a in chain:
        if a is None or a == "":
            continue
        # Type-checked BEFORE any conversion: a nested list or dict planted here
        # would be materialized whole by ``str()`` before the length bound could
        # see it, so anything that is not already a string is unprovable.
        if not isinstance(a, str):
            _log.warning("fork_ancestors entry is not a string; treating as unprovable")
            return None
        if len(a) > MAX_ANCESTOR_KEY_CHARS:
            _log.warning(
                "fork_ancestors entry over %d chars; treating as unprovable",
                MAX_ANCESTOR_KEY_CHARS,
            )
            return None
        out.append(a)
    if _chain_bytes(out) > MAX_FORK_ANCESTRY_BYTES:
        _log.warning(
            "fork_ancestors over %d serialized bytes; treating as unprovable",
            MAX_FORK_ANCESTRY_BYTES,
        )
        return None
    return out


def _chain_bytes(chain: list[str]) -> int:
    """Size of ``chain`` as a metadata save serializes it (``json.dumps``)."""
    return len(json.dumps(chain).encode("utf-8"))


#: A ``forked_from`` value that is present but not admissible: not a string,
#: or a string over ``MAX_ANCESTOR_KEY_CHARS``. Distinct from ``None`` (no
#: parent recorded) so a reader can fail closed on it.
UNPROVABLE = object()


def parent_key(meta: Any) -> str | None | object:
    """The ``forked_from`` edge in ``meta``: the key, ``None`` when none is
    recorded (absent, ``None`` or empty), or :data:`UNPROVABLE` when the value
    is not a string or is over the shared length bound. Type-checked before any
    conversion so a nested structure is never materialized as text."""
    if not isinstance(meta, dict):
        return None
    parent = meta.get("forked_from")
    if parent is None or parent == "":
        return None
    if not isinstance(parent, str):
        _log.warning("forked_from is not a string; treating as unprovable")
        return UNPROVABLE
    if len(parent) > MAX_ANCESTOR_KEY_CHARS:
        _log.warning("forked_from over %d chars; unprovable", MAX_ANCESTOR_KEY_CHARS)
        return UNPROVABLE
    return parent


def admitted_chain(meta: Any) -> list[str]:
    """The chain to carry on a slot restored from ``meta``: the recorded chain,
    or ``[]`` when none is recorded OR the record is unprovable. A restore that
    calls this must also record :func:`chain_unprovable` on the slot, so the
    unprovable fact is written back (:func:`chain_record_for_save`) instead of
    the record being erased by the next save."""
    return recorded_chain(meta) or []


def chain_unprovable(meta: Any) -> bool:
    """Whether ``meta`` carries a ``fork_ancestors`` record that is present but
    unprovable (not a list, or over the shared bounds)."""
    return recorded_chain(meta) is None


def chain_record_for_save(chain: list[str], unprovable: bool) -> Any:
    """What a save writes under ``fork_ancestors``: the chain when there is one,
    :data:`UNPROVABLE_CHAIN_RECORD` when the slot was restored from an unprovable
    record (so readers keep failing closed on it), else ``None`` (omit)."""
    if unprovable:
        return dict(UNPROVABLE_CHAIN_RECORD)
    return list(chain) if chain else None


def walk_ancestors(
    start: str,
    parent_of: Callable[[str], str | None],
    known_of: Callable[[str], Iterable[str]],
) -> set[str] | None:
    """THE ancestry walk, over folded stems.

    From ``start``, union each visited node's materialized chain (``known_of``)
    and follow its immediate edge (``parent_of``). Cycle-safe by visited set.
    Both the catalog-backed :func:`ancestors_of` and the reap's snapshot walk
    call this, so there is one definition of "ancestor".

    Bounded by the same two limits every other ancestry reader applies: the walk
    visits at most ``MAX_FORK_ANCESTORS`` nodes and every key it retains (from
    ``known_of`` or ``parent_of``) is at most ``MAX_ANCESTOR_KEY_CHARS``.
    ``forked_from`` is agent-writable, so a deeper or wider chain is not walked
    further: the answer is ``None`` (unprovable) and the caller fails closed.
    """
    out: set[str] = set()
    visited: set[str] = set()
    # ``start`` is caller-supplied (the asset route's ``?session=``), so it is
    # bounded like every key the walk retains, before it is folded or kept.
    if len(start) > MAX_ANCESTOR_KEY_CHARS:
        _log.warning("ancestry walk start over %d chars; unprovable", MAX_ANCESTOR_KEY_CHARS)
        return None
    cur: str | None = fold(start)
    while cur and cur not in visited:
        if len(visited) >= MAX_FORK_ANCESTORS:
            _log.warning("ancestry walk over %d nodes; treating as unprovable", MAX_FORK_ANCESTORS)
            return None
        visited.add(cur)
        for a in known_of(cur):
            if len(a) > MAX_ANCESTOR_KEY_CHARS:
                _log.warning("ancestry entry over %d chars; unprovable", MAX_ANCESTOR_KEY_CHARS)
                return None
            out.add(fold(a))
            if len(out) > MAX_FORK_ANCESTORS:
                _log.warning("ancestry set over %d entries; unprovable", MAX_FORK_ANCESTORS)
                return None
        parent = parent_of(cur)
        if parent and len(parent) > MAX_ANCESTOR_KEY_CHARS:
            _log.warning("forked_from over %d chars; unprovable", MAX_ANCESTOR_KEY_CHARS)
            return None
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

    A spelling counts as readable only when a transcript EXISTS under it: the
    catalog answers ``({}, readable)`` for an absent file by design, so without
    this check an alias that names no file would make an unreadable canonical
    record look like a readable root, and a fork would persist a one-link chain
    as if it were complete. A log without ``has_log`` (a test double) cannot make
    that distinction and is trusted as before.

    The metadata line is agent-writable, so it is bounded BEFORE the catalog
    decodes it (:func:`metadata_line_within_bound`); a line over the bound reads
    as unreadable, the same as a line that fails to decode.
    """
    bare = strip_dashboard_prefix(session)
    readable = False
    found: dict = {}
    exists = getattr(log, "has_log", None)
    spellings = (session, f"dashboard:{bare}", bare, _tab_to_session(bare))
    for spelling in dict.fromkeys(spellings):
        try:
            if callable(exists) and not exists(spelling):
                continue
            if not metadata_line_within_bound(log, spelling):
                continue
            meta, ok = log.get_metadata_status(spelling)
        except Exception:
            continue
        if ok and isinstance(meta, dict):
            readable = True
            if meta and not found:
                found = meta
    return found, readable


def metadata_line_within_bound(log: Any, key: str) -> bool:
    """Whether transcript ``key``'s first line fits :data:`MAX_METADATA_LINE_BYTES`,
    read through the sensitive-path chokepoint with a byte cap, so the answer
    itself costs at most that many bytes and never follows a link.

    The catalog's ``get_metadata_status`` decodes the whole first line and
    caches the result, and ``recorded_chain`` bounds the ``fork_ancestors``
    array only after the decoder has materialized it, so every lineage reader
    (the live-catalog walk here, the lineage snapshot in
    ``handlers.sessions``) bounds the LINE first, through this one check. The
    transcript directory is agent-writable, so the pre-read goes through
    :func:`hooks.safe_read_file_bytes_nolink` pinned to that directory: a
    transcript name swapped for a symlink, a hardlink to a file outside it, or
    a non-regular file is refused by the read itself, not by a check made
    before it, and the record reads as unreadable. A store without a path
    resolver (a test double) has nothing to pre-read and passes; a file that
    cannot be read fails closed, the same way its metadata read would, and so
    does a line over the bound: the walk is unprovable.
    """
    resolve = getattr(log, "_path", None)
    if resolve is None:
        return True
    try:
        path = resolve(key)
    except Exception:
        return False
    if not isinstance(path, Path):
        return True
    cap = MAX_METADATA_LINE_BYTES
    head = hooks.safe_read_file_bytes_nolink(
        str(path),
        within_root=str(path.parent),
        max_bytes=cap + 1,
        allow_truncate=True,
    )
    if head is None:
        return False
    first, newline, _rest = head.partition(b"\n")
    return bool(newline) or len(first) <= cap


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

    def _parent(stem: str) -> str | None:
        nonlocal overflow
        parent = parent_key(_meta(stem))
        if parent is UNPROVABLE:
            overflow = True
            return None
        return parent if isinstance(parent, str) else None

    found = walk_ancestors(session, parent_of=_parent, known_of=_known)
    return set() if overflow or found is None else found


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
    if not isinstance(key, str):
        _log.warning("fork_ancestors %s is not a string; chain is unprovable", what)
        return False
    s = key
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


def ancestry_chain(log: Any, session_key: str) -> list[str] | None:
    """The ancestor chain of ``session_key`` as RAW keys, nearest first, or
    ``None`` when the chain is UNPROVABLE.

    Used at fork time to materialize a new fork's ``fork_ancestors`` from a
    source that is itself a fork. Prefers the source's own recorded chain; a
    pre-upgrade source (``forked_from`` only) is walked through the catalog so
    the new fork records the FULL chain rather than one link. Every entry is
    retained through the shared bounds — the legacy walk reads agent-writable
    ``forked_from`` values one row at a time, so the bound is applied per
    append, not on the finished list. A walk that cannot be completed (an
    unreadable link, a record over the shared bounds, a non-string parent) is
    reported as ``None`` rather than as the prefix that was readable: a fork
    persisted with a partial chain would claim a complete ancestry it does not
    have, so the caller refuses the fork instead. A walk that ends because the
    root has no parent, or because it closed a cycle, is complete.
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
            return None
        chain = recorded_chain(meta)
        if chain is None:
            return None
        if chain:
            for a in chain:
                if a and fold(a) not in seen:
                    if not _retain(out, a, what="from a recorded chain"):
                        return None
                    seen.add(fold(a))
            break
        parent = parent_key(meta)
        if parent is None:
            break
        if not isinstance(parent, str) or not _retain(out, parent, what="from a forked_from walk"):
            return None
        cur = parent
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
) -> list[str] | None:
    """The ``fork_ancestors`` list for a new fork of ``source_key``: the source
    itself, then (for a linked session) the source TAB's own key, then the
    source's chain, deduplicated, nearest first; ``None`` when the chain is
    UNPROVABLE because an entry could not be retained (an over-long key, a
    chain at the count bound). A fork whose ancestry cannot be recorded whole
    is refused by the caller rather than persisted with a shorter one.

    ``source_slot_key`` is the source slot's ``key`` — the name image copies are
    registered under (``register_images`` receives ``slot.key``). For a
    dashboard-born tab it folds to the same stem as the session key and is
    skipped; for a linked session (a cron run, a task-review tab) it does not,
    and without it the fork could never reach the copies its source's tab owns.

    Every entry is retained through :func:`_retain`; the first refusal makes the
    whole chain unprovable (``None``). In particular the SOURCE key itself is
    bounded: a dashboard session key is ``dashboard:<slot>``, so a slot name
    admitted at the owner bound would exceed it once prefixed — admission
    therefore bounds the name at :data:`MAX_SLOT_NAME_CHARS`, and this is the
    fail-closed backstop for any key that still arrives over the bound.
    """
    out: list[str] = []
    if not _retain(out, source_key, what="for the fork source"):
        return None
    if source_slot_key and fold(source_slot_key) != fold(source_key):
        if not _retain(out, source_slot_key, what="for the fork source tab"):
            return None
    for a in source_ancestors or ():
        if a and a not in out and not _retain(out, a, what="from the source chain"):
            return None
    # The chain is written into the fork's metadata line, so it must also fit
    # the serialized budget the reader enforces; a deep chain of long keys can
    # pass the count and per-entry bounds and still not.
    if _chain_bytes(out) > MAX_FORK_ANCESTRY_BYTES:
        _log.warning(
            "fork_ancestors over %d serialized bytes; chain is unprovable",
            MAX_FORK_ANCESTRY_BYTES,
        )
        return None
    return out


def legacy_aliases(key: str) -> dict[str, str]:
    """``{legacy stem: canonical stem}`` for a key that may also log under a
    pre-canonical stem (Slack threads predating ``slack:<ts>``)."""
    stems = transcript_stems(key)
    return {s: stems[0] for s in stems[1:]}
