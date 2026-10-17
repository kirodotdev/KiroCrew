"""The per-turn tool-call provenance caches, and the liveness keys over the same calls.

Each cache's scoped key; the count, payload, key and path caps; the overflow record and
the admission that applies them; the release at a terminal status; and the liveness
bookkeeping (:func:`liveness_key`, :class:`InFlightIdSet`) over the same in-flight
population.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Iterator
from typing import Any

from kiro_crew.acp._dispatch_parts.surfaced_text import UNSERIALISABLE_SIBLING_VALUE

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp._dispatch")


def scoped_tool_cache_key(cache_scope: str, tool_call_id: str) -> str:
    """The key a per-tool cache entry is stored under, for ONE emitting origin.

    Cache entries are keyed by the emitting frame's ``sessionId`` as well as the
    ``toolCallId``, so a backend-internal child cannot replay a consumed PARENT id and
    inherit trusted params for a different operation, while a same-origin repeat frame
    still resolves. An empty scope degrades to the bare id, which is the shape
    ``AcpClient`` uses -- one client drives one session, so it has no second origin.

    One authoring because every reader must compute the key the writer used: a site
    that spells it differently does not raise, it MISSES, and a missed trusted-params
    lookup reads as "this call carries no identity".
    """
    return f"{cache_scope}|{tool_call_id}" if cache_scope else tool_call_id


#: Bound on the number of DISTINCT scoped tool-call ids the per-turn provenance
#: caches (``raw_params`` / ``shell`` / ``mcp_server_name`` / ``tool_name`` /
#: ``diff_path`` / ``tool_kind`` / ``tool_input`` + its redaction bit) retain at
#: once. ONE constant for the whole population: every cache is keyed on the same
#: ``scoped_tool_cache_key`` and written for the same frames, so two structures
#: with two caps would split one announced population into two classes by
#: arrival order. The population is the calls IN FLIGHT, not the calls a turn has
#: made: a row is released the moment its call's result frame reports a
#: terminal status (:func:`release_tool_call_provenance`, run by the handle after
#: the result's own readers), because the permission request precedes execution
#: and nothing reads a settled call's provenance afterwards. A harness runs a
#: handful of calls at once (a native sub-agent fan-out is the widest shape);
#: the cap is well above that and exists so a runaway or hostile stream of
#: unique ids whose results never arrive cannot grow the caches without limit
#: until the turn resets.
TOOL_CALL_CACHE_MAX_ENTRIES = 256


def release_tool_call_provenance(key: str, *caches: dict[str, Any] | None) -> None:
    """Drop every provenance row under *key* -- a call that reached a terminal status.

    The counterpart of :func:`admit_tool_call_cache_entry`'s eviction, for the
    ordinary end of a call rather than a refusal: the caller passes the same
    sibling stores the admission gates, so a settled call leaves no row anywhere
    and the count bound measures calls in flight. Reached from the handle only
    after the result frame's own readers ran (the spec-disabled tripwire reads
    ``raw_params`` under this key at the terminal), never from the parser, which
    would release the row before they did. A key no store holds is a no-op, so a
    stray terminal frame for an unannounced id changes nothing.
    """
    for c in caches:
        if c is not None:
            c.pop(key, None)


#: Bound on the LARGEST externally-sized field a retained row carries: the
#: frame's ``rawInput``, measured as the LARGER of the rendered JSON of the
#: dict the row stores (``raw_params``) and the final string it stores
#: (``tool_input``, which a diff block or derived diff may have replaced with a
#: smaller rendering -- measuring that alone would retain the dict unmeasured).
#: The two
#: other externally-sized fields -- the scoped key and the diff-block path --
#: have their own bounds below, applied in the same admission; every remaining
#: retained field is bounded on its own (a bool, a ``WRITE_PLANE_KINDS``
#: literal, a ``_harness_identifier``-checked name). The transport caps a
#: whole frame at ``runtime._STDOUT_BUFFER_LIMIT`` (10 MiB), which bounds one
#: row but not the population; these bounds are what make
#: ``TOOL_CALL_CACHE_MAX_ENTRIES`` a bound on memory (at most
#: ``MAX_ENTRIES * (MAX_PAYLOAD_CHARS + MAX_KEY_CHARS + MAX_PATH_CHARS)`` per
#: turn, ~512 MiB). Generous on purpose: a 2 MB tool input is far beyond any
#: real file write's ``fileText``; a call over it is not retained and its
#: permission request is refused, never trusted on a partial reading.
TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS = 2_000_000

#: Bound on the scoped KEY a row is stored under -- the emitting frame's
#: ``sessionId`` joined to its ``toolCallId``, both backend-authored strings.
#: Real ids are UUIDs or short ``toolu_``/``call_``-prefixed tokens; a key over
#: this length is not one, and the frame is refused as overflow so a stream of
#: near-limit ids cannot spend the count cap on key bytes alone.
TOOL_CALL_CACHE_MAX_KEY_CHARS = 512

#: Bound on the diff-block ``path`` a row retains (``diff_path_cache``). The
#: longest path any host filesystem accepts is a few KiB (Linux ``PATH_MAX``
#: is 4096), so a longer value is not a file the write went to and the frame is
#: refused rather than the path cut -- a truncated path would name a different
#: file to the target gate.
TOOL_CALL_CACHE_MAX_PATH_CHARS = 4096

#: Most tool calls the liveness bookkeeping holds at once -- the session
#: handle's attribution map (``AcpSessionHandle._active_tool_calls``) and the
#: ``AcpClient``'s open-call set alike. It is the provenance caches' count bound
#: by name, not a second literal: all three bound the same population, the calls
#: of one turn with no terminal result yet, and a turn cannot legitimately have
#: this many open. Past it the oldest entry is evicted and the eviction counted.
MAX_ACTIVE_TOOL_CALLS = TOOL_CALL_CACHE_MAX_ENTRIES

#: The prefix a digested liveness key carries. Reserved: a raw id spelling it is
#: digested as well (:func:`liveness_key`), so the prefix names a digest and
#: nothing else.
LIVENESS_DIGEST_PREFIX = "sha256:"


def liveness_key(tool_call_id: object) -> str:
    """The key the liveness bookkeeping holds for a call.

    A ``toolCallId`` is backend-authored and bounded on the wire only by the
    transport frame cap. An id within :data:`TOOL_CALL_CACHE_MAX_KEY_CHARS` is
    kept as it is; a longer one is held as its digest, so the key is bounded
    without the two ids a long key could collide on being conflated -- the same
    id maps to the same digest at dispatch, at its streamed output and at its
    terminal. An id that itself spells the digest prefix is digested too,
    whatever its length, so no raw id can equal another call's digest key:
    every key that starts with the prefix is a digest, and two digests agree
    only when the ids do. A value that is not a string (a JSON number,
    ``None``) is no key: the result parser hands the frame's ``toolCallId``
    through as it arrived, and a harness that spells one as a number must not
    crash the turn here.
    """
    if not isinstance(tool_call_id, str):
        return ""
    if len(tool_call_id) <= TOOL_CALL_CACHE_MAX_KEY_CHARS and not tool_call_id.startswith(
        LIVENESS_DIGEST_PREFIX
    ):
        return tool_call_id
    return (
        LIVENESS_DIGEST_PREFIX
        + hashlib.sha256(tool_call_id.encode("utf-8", "surrogatepass")).hexdigest()
    )


class InFlightIdSet:
    """The ``AcpClient``'s bounded set of open tool-call ids.

    That transport asks one question of its calls -- *is any still open* -- to
    arm the tool-stall clock and disarm the stale clock, so it keeps ids only,
    never a state. The ids are backend-authored, so the set is bounded like the
    handle's attribution map: each id is held under :func:`liveness_key`, at
    most :data:`MAX_ACTIVE_TOOL_CALLS` are held (insertion order is dispatch
    order, so the OLDEST is evicted first and the call just dispatched is always
    kept), and every eviction is counted until :meth:`clear`. Once a row has
    been evicted an empty set does not prove that nothing runs, which is what
    :attr:`possibly_open` reads; the counter is what the turn says out loud.
    The empty id a backend omits is a member like any other, as it was in the
    plain set this replaces.
    """

    __slots__ = ("_ids", "_max_ids", "_evicted_count")

    def __init__(self, max_ids: int = MAX_ACTIVE_TOOL_CALLS) -> None:
        self._ids: dict[str, None] = {}
        self._max_ids = max_ids
        self._evicted_count = 0

    def add(self, tool_call_id: object) -> None:
        key = liveness_key(tool_call_id)
        if key not in self._ids and len(self._ids) >= self._max_ids:
            self._ids.pop(next(iter(self._ids)))
            self._evicted_count += 1
        self._ids[key] = None

    def discard(self, tool_call_id: object) -> None:
        self._ids.pop(liveness_key(tool_call_id), None)

    def difference_update(self, tool_call_ids: Iterable[object]) -> None:
        for tool_call_id in tool_call_ids:
            self.discard(tool_call_id)

    def clear(self) -> None:
        self._ids.clear()
        self._evicted_count = 0

    def __contains__(self, tool_call_id: object) -> bool:
        return liveness_key(tool_call_id) in self._ids

    def __iter__(self) -> Iterator[str]:
        return iter(self._ids)

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def evicted_count(self) -> int:
        """Ids evicted at the cap since the last :meth:`clear`."""
        return self._evicted_count

    @property
    def possibly_open(self) -> bool:
        """Whether a call may still be running: one is held, or one was evicted."""
        return bool(self._ids) or self._evicted_count > 0


#: Marker a caller-owned overflow record carries once it holds
#: ``TOOL_CALL_CACHE_MAX_ENTRIES`` distinct scope digests: from then on EVERY
#: scope reads as overflowed, so the record stays bounded without a refused
#: scope ever going unrecorded (the fail-safe direction: an unretained request
#: is refused, never judged on its title).
OVERFLOW_EVERY_SCOPE = "*"


def overflow_scope_key(cache_scope: str) -> str:
    """The fixed-size key an overflow record stores for *cache_scope*.

    The scope is the emitting frame's own ``sessionId`` -- a backend-authored
    string with no length bound -- and the record exists precisely to remember
    scopes whose frames were refused for being oversized, so storing the text
    would retain what the admission refused. A SHA-256 digest is 64 characters
    whatever the scope was; the one reader (the handle's refusal of an
    unretained permission request) digests the request's scope the same way.
    """
    return hashlib.sha256(cache_scope.encode("utf-8", "surrogatepass")).hexdigest()


def scope_overflowed(cache_overflow: set[str], cache_scope: str) -> bool:
    """Whether *cache_scope* is recorded as overflowed in *cache_overflow*."""
    return (
        OVERFLOW_EVERY_SCOPE in cache_overflow or overflow_scope_key(cache_scope) in cache_overflow
    )


def admit_tool_call_cache_entry(
    key: str,
    cache_scope: str,
    payload_chars: int,
    cache_overflow: set[str] | None,
    *caches: dict[str, Any] | None,
    path_chars: int = 0,
) -> bool:
    """Whether the per-turn provenance caches may take an entry under *key*.

    The admission answer gates EVERY sibling store, and three bounds decide it:

    * COUNT -- a key already present in any store (a same-id refinement or repeat
      frame) is admitted, so an announced call keeps refining; a key none of them
      holds is admitted only while the population -- the union of the sibling key
      sets -- is below :data:`TOOL_CALL_CACHE_MAX_ENTRIES`.
    * SIZE -- *payload_chars* must not exceed :data:`TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS`,
      whether the id is new or held: a refinement carrying an oversized payload is
      refused too, so a small first frame cannot reserve a row a huge second
      frame then fills. The callers pass the LARGER of the params rendering and
      the final retained rendering, because a row keeps both the ``raw_params``
      dict and the (diff-replaced, redacted) ``tool_input`` string, and a diff
      rendering is usually smaller than the params it was derived from.
    * SIZE of the other two retained frame strings -- *key* itself (the scoped
      ``toolCallId``) must not exceed :data:`TOOL_CALL_CACHE_MAX_KEY_CHARS` and
      *path_chars* (the diff-block ``path`` the frame named, 0 when it named
      none) must not exceed :data:`TOOL_CALL_CACHE_MAX_PATH_CHARS`. Every byte a
      row keeps is then measured by a bound the gate applies, so the count cap
      is a bound on memory rather than on rows of unbounded width.

    A refused key gets no row anywhere -- including any row an earlier admitted
    frame wrote under it, which is evicted from every store -- and its SCOPE is
    recorded in *cache_overflow* as a fixed-size digest (:func:`overflow_scope_key`;
    one entry per emitting session, cleared with the caches, and past
    :data:`TOOL_CALL_CACHE_MAX_ENTRIES` distinct scopes the record holds
    :data:`OVERFLOW_EVERY_SCOPE` instead, so it is bounded and every later scope
    reads as overflowed): the permission request for a refused id then misses
    every cache, and the
    handle REFUSES it outright (``AcpSessionHandle._refuse_unretained_provenance``)
    rather than letting a consumer judge a call whose kind, identity and target
    were never retained -- so the bound narrows what is provable, never what is
    permitted, and a write-plane deny cannot stop binding by being flooded past
    the cap. Overflow is logged once per scope PER SNAPSHOT: the warning is
    deduplicated against *cache_overflow* itself, which is cleared with the
    caches at turn reset, so the next turn's overflow is said again rather than
    swallowed by a record that outlives the turn.
    """
    live = [c for c in caches if c is not None]
    admitted = (
        payload_chars <= TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS
        and len(key) <= TOOL_CALL_CACHE_MAX_KEY_CHARS
        and path_chars <= TOOL_CALL_CACHE_MAX_PATH_CHARS
    )
    if admitted and not any(key in c for c in live):
        population: set[str] = set()
        for c in live:
            population.update(c.keys())
        admitted = len(population) < TOOL_CALL_CACHE_MAX_ENTRIES
    if admitted:
        return True
    # A refused frame leaves NO row under its key: a refinement of a held id
    # that is refused for size would otherwise leave the earlier frame's
    # params, diff path, identity and kind in place -- and the permission
    # request that follows would be judged against the superseded target,
    # marked trusted, so the handle's unretained-provenance refusal would not
    # catch it. Evicting makes the request miss every cache and be refused.
    for c in live:
        c.pop(key, None)
    digest = overflow_scope_key(cache_scope)
    # The warning is deduplicated against the caller-owned record, which lives
    # and is cleared with the caches: a scope is said out loud the first time
    # THIS snapshot refuses it, and said again on the next snapshot. A record
    # that has saturated (OVERFLOW_EVERY_SCOPE) cannot tell scopes apart, so the
    # saturation itself is the last line it says; every later scope reads as
    # overflowed from the marker.
    first_sighting = cache_overflow is None or not (
        digest in cache_overflow or OVERFLOW_EVERY_SCOPE in cache_overflow
    )
    saturated = False
    if cache_overflow is not None and first_sighting:
        if len(cache_overflow) >= TOOL_CALL_CACHE_MAX_ENTRIES:
            cache_overflow.add(OVERFLOW_EVERY_SCOPE)
            saturated = True
        else:
            cache_overflow.add(digest)
    if first_sighting:
        logger.warning(
            "tool-call provenance caches refused a frame this turn (cap %d distinct ids, "
            "%d chars of input per call, %d-char ids, %d-char diff paths); the refused "
            "call's permission request is rejected as unverifiable (scope digest %s)%s",
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
            TOOL_CALL_CACHE_MAX_KEY_CHARS,
            TOOL_CALL_CACHE_MAX_PATH_CHARS,
            digest[:16],
            (
                "; the overflow record is saturated, so every later scope this turn "
                "reads as overflowed without a line of its own"
                if saturated
                else ""
            ),
        )
    return False


def _params_chars_for_admission(rendering: str) -> int:
    """The size the admission measures for the retained ``raw_params`` dict.

    The rendering stands for the dict (see the call sites), so its length is the
    dict's measured size -- EXCEPT when the encoder degraded. A degraded
    rendering is the fixed placeholder, not a measurement: the dict behind it
    was too deep to serialise and may hold strings of any length, so measuring
    the placeholder would admit and retain a payload the bound never saw. A
    payload the encoder could not measure is therefore counted as OVER the
    payload bound, and the admission refuses it like any oversized input (the
    placeholder is still what the event displays; only the retained provenance
    is withheld). A ``str(payload)`` degrade is a real rendering of the same
    bytes and keeps its measured length.
    """
    if rendering == UNSERIALISABLE_SIBLING_VALUE:
        return TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1
    return len(rendering)
