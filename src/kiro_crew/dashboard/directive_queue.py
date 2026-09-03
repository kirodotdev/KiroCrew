"""Provider-neutral delivery for session directives.

The directive marker (:mod:`kiro_crew.session_directive`) is model-visible text,
so the consumer may only honour it when the tool CALL it arrived under was
recorded as an MCP call served by Kiro Crew's own core server. That recording
comes from the provider's out-of-band ``_meta.kiro`` channel, which is a
kiro-cli engine feature: an ACP backend that does not emit it leaves the gate
with no trusted source, and a gate with no trusted source correctly refuses
every directive. The whole control plane (loops, project changes, cards) then
fails closed on that backend — silently, until #6970 added the diagnostic.

This module is the second delivery path, and it carries the payload OUT OF BAND
rather than through the model's tool result. The MCP tool, having validated its
arguments, POSTs them to the gateway over Kiro Crew's own internal API declaring
its ``X-Session-Key``; the gateway parks the record here; the turn's consumer
claims it. The marker is still emitted (the kiro-cli path is unchanged and
remains authoritative there), but on a backend without ``_meta.kiro`` the marker
is reduced to a HINT that a record may be waiting — its CONTENT is never read.

Why this is not weaker than the marker gate it backs up
------------------------------------------------------
TWO channels must agree, and neither is trusted alone. That is the whole design:

* The RECORD carries the payload and is unforgeable in CONTENT — it is what the
  tool validated, delivered out of band, never lifted from model-visible text.
  Its weak point is its TARGET: the session is named by an ``X-Session-Key``
  header, and the header is only kernel-attested on an AF_UNIX peer whose /proc
  ancestry resolves. Over TCP loopback (Windows has no AF_UNIX at all), or from a
  pooled backend whose ancestry does not resolve, a same-uid caller holding the
  internal secret could name somebody else's session.
* The MARKER is bound to the right session by construction — it arrives inside
  the tool result of a call made in THAT turn, on that session's own event
  stream. Its weak point is CONTENT: it is model-visible text, so a model can
  type one.
* So a directive applies only where BOTH hold: :func:`claim` requires a parked
  record whose ``(kind, args)`` equal the ones the frame's marker names, parked
  during the CLAIMING turn. A record aimed at another session waits for a marker
  that session's model never emits; a forged marker looks up a record that no
  tool ever validated. The applied payload is always the RECORD's, so the marker
  never contributes a value — only the choice of which record to look up.

A model can still drive its OWN session by publishing and marking honestly, which
is exactly what calling the tool does. No privilege is gained.

Deliberately NOT persisted. A directive is turn-scoped: the turn that requested
it is what gives it meaning. Surviving a gateway restart would let a loop arm, or
a project change land, against a turn that no longer exists — so records live in
memory and are additionally dropped on age.

In-memory means the store has to be bounded in BOTH dimensions, and the reclaim
runs on PUBLISH because for most sessions there is no read path at all: only the
dashboard consumer claims, while the messaging ``TurnDriver`` applies directives
from the verified marker and never calls in here. Records expire by age
(:data:`MAX_AGE_SECS`), a bucket is capped (:data:`MAX_PER_SESSION`), and a bucket
whose records have all expired is DELETED rather than left empty — so the map
holds only sessions that published recently, with :data:`MAX_SESSIONS` as the
backstop for a burst inside one expiry window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any

from kiro_crew.session_directive import DIRECTIVE_TOOLS, content_free_digest

logger = logging.getLogger(__name__)

#: Records older than this are dropped unclaimed. A directive belongs to the turn
#: that asked for it; a turn does not outlive this by any normal margin, and a
#: record that does has lost the context that made it meaningful.
MAX_AGE_SECS = 300.0

#: Per-session cap. One record is claimed per directive tool call, so depth
#: beyond this means records are not being claimed at all (a tabless session, a
#: backend emitting neither path). Bounding it keeps an unclaimed queue from
#: growing without limit; the OLDEST is dropped, so a live session always retains
#: its most recent intent.
MAX_PER_SESSION = 8

#: Process-wide cap on how many SESSIONS may hold a bucket at once — the second
#: dimension, and the one a per-session cap cannot bound. Only the dashboard
#: consumer claims or discards; the messaging ``TurnDriver`` (Slack, Discord,
#: Telegram, …) applies directives from the verified marker and never touches this
#: module, so every distinct channel conversation that calls a directive tool would
#: otherwise leave a bucket behind that nothing ever removes. Records expire on
#: age, so :func:`_sweep_locked` alone keeps the map to sessions that published
#: recently; this cap is the backstop for a burst inside one expiry window.
MAX_SESSIONS = 256

_lock = threading.Lock()
_pending: dict[str, list[dict[str, Any]]] = {}


def publish(session_key: str, kind: str, args: dict[str, Any]) -> str:
    """Park a validated directive for *session_key*; return its record id.

    Raises :class:`ValueError` for an unknown *kind* or an empty *session_key* —
    the caller is the gateway handler, and an unrecognized kind means the request
    did not come from one of Kiro Crew's own directive tools.
    """
    if kind not in DIRECTIVE_TOOLS:
        raise ValueError(f"unknown directive kind: {kind!r}")
    if not session_key:
        raise ValueError("session_key required")
    rec_id = uuid.uuid4().hex
    now = time.monotonic()
    record: dict[str, Any] = {
        "id": rec_id,
        "kind": kind,
        "args": dict(args or {}),
        "at": now,
    }
    with _lock:
        queue = _pending.setdefault(session_key, [])
        queue.append(record)
        while len(queue) > MAX_PER_SESSION:
            dropped = queue.pop(0)
            logger.warning(
                "session-directive queue full for %s: dropped unclaimed %s "
                "(cap %d). Nothing claimed these — the session may hold no "
                "consumer.",
                session_key,
                dropped.get("kind"),
                MAX_PER_SESSION,
            )
        # Reclaim on the write path, because for many sessions there is no read
        # path: a claim only ever runs for a session whose turn reaches the
        # dashboard consumer, so a bucket belonging to any other surface is never
        # visited again and would live for the gateway's whole lifetime.
        _sweep_locked(now)
    return rec_id


def _sweep_locked(now: float) -> None:
    """Drop expired records, and the buckets they leave empty. Caller holds ``_lock``.

    Bounds the store in BOTH dimensions. Records inside a bucket are bounded by
    age; the number of BUCKETS is bounded because a bucket whose records have all
    expired is DELETED rather than left as an empty list — so the map holds only
    sessions that published within :data:`MAX_AGE_SECS`. Total work is bounded by
    ``MAX_SESSIONS * MAX_PER_SESSION``.

    The publisher that triggers a sweep is safe from it without needing to be
    named: it has just appended a record stamped *now*, so its bucket always has a
    fresh member and cannot be deleted, and that record is the newest in the store,
    so its bucket sorts last for eviction. Both fall out of the ordering, which is
    why there is no exemption argument to get wrong.
    """
    for key in list(_pending):
        fresh = [r for r in _pending[key] if now - float(r.get("at", 0.0)) <= MAX_AGE_SECS]
        if fresh:
            _pending[key] = fresh
        else:
            del _pending[key]
    if len(_pending) <= MAX_SESSIONS:
        return
    # Backstop: more distinct sessions published inside one expiry window than the
    # cap allows. Evict whole buckets, least-recently-published first, so the
    # sessions most likely to still have a live turn are the ones retained.
    victims = sorted(
        _pending,
        key=lambda k: max((float(r.get("at", 0.0)) for r in _pending[k]), default=0.0),
    )
    for key in victims[: len(_pending) - MAX_SESSIONS]:
        dropped = _pending.pop(key, [])
        logger.warning(
            "session-directive store full: evicted %s unclaimed record(s) for %s "
            "(cap %d sessions). Nothing claimed them — that surface holds no "
            "out-of-band consumer.",
            len(dropped),
            key,
            MAX_SESSIONS,
        )


def _canonical(args: dict[str, Any] | None) -> str:
    """Order-independent comparable form of a directive's ``args``.

    The two channels serialize independently — the marker through
    ``session_directive.encode``, the record through the internal API's JSON body
    — so the dicts are equal in value but not necessarily in key order. Sorting
    keys makes the comparison about the payload rather than about how either side
    happened to emit it, and ``default=str`` mirrors ``encode`` so a value only
    one side could serialize compares equal instead of raising.
    """
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


def claim(
    session_key: str,
    kind: str,
    args: dict[str, Any] | None,
    *,
    not_before: float | None = None,
) -> dict[str, Any] | None:
    """Remove and return the ONE record for *session_key* matching *kind*/*args*.

    CORRELATED by construction, which is what makes the out-of-band path safe to
    act on (see the module docstring): the caller passes the ``(kind, args)`` its
    frame's marker named, and only a record parked with that same payload is
    returned. An uncorrelated drain would apply whatever happened to be queued —
    including a record another session's caller parked here, or one left by a turn
    that was cancelled before it could consume it.

    *not_before* bounds the record to the claiming TURN (pass the turn's start
    from ``time.monotonic()``). A directive belongs to the turn that asked for it:
    without this bound, a record whose turn was abandoned stays claimable by any
    later frame naming the same payload, so a cancelled intent could land minutes
    later.

    Single-consume: the match is removed under the lock, so two consumers racing
    the same session cannot both apply it. Returns ``None`` when nothing matches.
    """
    if not session_key or kind not in DIRECTIVE_TOOLS:
        logger.warning(
            "session-directive CLAIM REFUSED before lookup: session_key=%r kind=%r "
            "(empty session key, or kind is not a known directive tool). Nothing "
            "was claimed.",
            session_key,
            kind,
        )
        return None
    now = time.monotonic()
    want = _canonical(args)
    with _lock:
        queue = _pending.get(session_key)
        if not queue:
            return None
        hit: dict[str, Any] | None = None
        keep: list[dict[str, Any]] = []
        misses: list[str] = []
        for record in queue:
            at = float(record.get("at", 0.0))
            age = now - at
            if age > MAX_AGE_SECS:
                misses.append(
                    "stale(kind=%r age=%.1fs > %.1fs)" % (record.get("kind"), age, MAX_AGE_SECS)
                )
                logger.info(
                    "session-directive dropped as stale for %s: %s (age %.0fs > %.0fs)",
                    session_key,
                    record.get("kind"),
                    age,
                    MAX_AGE_SECS,
                )
                continue
            # FIFO among equals: a repeated identical directive in one turn (the
            # model calling monitor_start twice with the same message) parks two
            # equal records, and each frame must consume its OWN. The queue is
            # oldest-first, so taking the first match pairs frame N with record N
            # while `keep` retains the rest for the sibling frames.
            _rec_kind = record.get("kind")
            _rec_args = _canonical(record.get("args"))
            if (
                hit is None
                and _rec_kind == kind
                and _rec_args == want
                and (not_before is None or at >= not_before)
            ):
                hit = record
                continue
            if hit is None:
                # Name the ONE reason this candidate was passed over. Ordered so
                # the first true predicate is the decisive one, because a record
                # failing on kind is a different bug from one failing only on the
                # turn bound -- and the old silent None could not tell them apart.
                if _rec_kind != kind:
                    misses.append("kind-differs(parked=%r wanted=%r)" % (_rec_kind, kind))
                elif _rec_args != want:
                    # Digests, not the args themselves: the parked record carries
                    # the payload the tool validated and `want` came off
                    # model-visible marker text, so printing either publishes
                    # directive content to the dashboard log. A digest still
                    # answers the question this line exists for -- are these two
                    # the same payload -- and pairs with the other side's log.
                    misses.append(
                        "args-differ(parked_sha=%s/%dB wanted_sha=%s/%dB)"
                        % (
                            content_free_digest(_rec_args),
                            len(_rec_args),
                            content_free_digest(want),
                            len(want),
                        )
                    )
                elif not_before is not None and at < not_before:
                    misses.append(
                        "parked-before-this-turn(at=%.1f < turn_start=%.1f, %.1fs earlier)"
                        % (at, not_before, not_before - at)
                    )
                else:
                    misses.append("already-matched-a-sibling-frame(kind=%r)" % (_rec_kind,))
            keep.append(record)
        if hit is None:
            if misses:
                logger.warning(
                    "session-directive CLAIM MISS for session_key=%r kind=%r: %d "
                    "parked record(s) were examined and none matched -> %s. The "
                    "record stays parked; the directive is NOT applied.",
                    session_key,
                    kind,
                    len(misses),
                    "; ".join(misses),
                )
            # Nothing matched. Keep what survived the staleness sweep so a
            # sibling frame in this same turn can still find its own record.
            if keep:
                _pending[session_key] = keep
            else:
                _pending.pop(session_key, None)
            return None
        if keep:
            _pending[session_key] = keep
        else:
            _pending.pop(session_key, None)
    return hit


def discard(session_key: str) -> int:
    """Drop any parked directives for *session_key*; return how many.

    The kiro-cli path applies the directive from the marker under a verified
    ``_meta.kiro`` identity. The out-of-band record for that same call is then a
    DUPLICATE, and applying both would arm two loops or render two cards — so the
    marker path calls this to retire its twin.
    """
    if not session_key:
        return 0
    with _lock:
        return len(_pending.pop(session_key, []))


def depth(session_key: str) -> int:
    """Parked record count for *session_key* — diagnostics only, no claim."""
    with _lock:
        return len(_pending.get(session_key, []))


def reset() -> None:
    """Drop every parked record. For tests and gateway shutdown."""
    with _lock:
        _pending.clear()
