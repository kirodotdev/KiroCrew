"""Provider-neutral delivery for session directives.

The directive marker (:mod:`kiro_crew.session_directive`) is model-visible text,
so the consumer may only honour it when the tool CALL it arrived under was
recorded as an MCP call served by Kiro Crew's own core server. That recording
comes from the provider's out-of-band ``_meta.kiro`` channel, which is a
kiro-cli engine feature: an ACP backend that does not emit it leaves the gate
with no trusted source, and a gate with no trusted source correctly refuses
every directive. The whole control plane (loops, project changes, cards) then
fails closed on that backend, which the gate reports as a diagnostic.

This module is the second delivery path, and it carries the payload OUT OF BAND
rather than through the model's tool result. The MCP tool, having validated its
arguments, POSTs them to the gateway over Kiro Crew's own internal API declaring
its ``X-Session-Key``; the gateway parks the record here; the turn's consumer
claims it. The marker is still emitted (the kiro-cli path is unchanged and
remains authoritative there), but on a backend without ``_meta.kiro`` the marker
is DISPLAY ONLY: neither its presence nor its content takes part in the claim.

How the consumer names the record: by the tool CALL's input, not the result
------------------------------------------------------------------------------
The record is keyed by :func:`session_directive.call_input_digest` of the raw
arguments the tool was called with. The tool computes it from the ``arguments``
of the ``tools/call`` it served; the consumer computes it from the ``rawInput``
of the ACP ``tool_call`` frame it saw the model make. Neither reading passes
through the tool RESULT, which is the one thing every backend reshapes at will:
KAS re-serialises the envelope, copies the text into two fields, swaps one for
an offload reference above a threshold, and caps every string with the
tail-anchored marker falling off the end. Reading the selector out of that body
would make each of those shapes one more repair branch in the shared ACP parser.
None of them touch ``rawInput``.

Why this is not weaker than the marker gate it backs up
------------------------------------------------------
TWO channels must agree, and neither is trusted alone. That is the whole design:

* The RECORD carries the payload and is unforgeable in CONTENT — the gateway
  DERIVES it by re-running the directive tool on the raw call arguments the stub
  reported (``mcp_core.derive_directive``), so a caller cannot pair a payload of
  its choosing with the key of some other call; it can only park what that call
  itself would produce. Never lifted from model-visible text.
  Its weak point is its TARGET: the session is named by an ``X-Session-Key``
  header, and the header is only kernel-attested on an AF_UNIX peer whose /proc
  ancestry resolves. Over TCP loopback (Windows has no AF_UNIX at all), or from a
  pooled backend whose ancestry does not resolve, a same-uid caller holding the
  internal secret could name somebody else's session.
* The CALL INPUT is bound to the right session by construction — it arrives in
  the ``tool_call`` frame of a call made in THAT turn, on that session's own
  event stream. Its weak point is CONTENT: it is model-authored, so a model can
  type any arguments it likes.
* So a directive applies only where BOTH hold: :func:`claim` requires a parked
  record whose input digest equals the one the frame's call carried, parked
  during the CLAIMING turn. A record aimed at another session waits for a call
  that session's model never makes; a call with arguments no tool ever validated
  looks up a record that was never parked. The applied payload is always the
  RECORD's, so the call input never contributes a value — only the choice of
  which record to look up. Exactly the shape the marker selector had.

A model can still drive its OWN session by calling the tool honestly, which is
exactly what calling the tool is. No privilege is gained.

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

import logging
import threading
import time
import uuid
from typing import Any

from kiro_crew.session_directive import DIRECTIVE_TOOLS

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


def publish(session_key: str, kind: str, args: dict[str, Any], input_digest: str) -> str:
    """Park a validated directive for *session_key*; return its record id.

    *input_digest* is :func:`session_directive.call_input_digest` of the raw
    arguments the tool was CALLED with (before validation added defaults), and
    it is the only key :func:`claim` matches on. Required, not optional: a
    record with no digest could never be claimed, so accepting one would park a
    directive the model was told was requested and that nothing can apply.

    Raises :class:`ValueError` for an unknown *kind*, an empty *session_key* or
    an empty *input_digest* — the caller is the gateway handler, and a request
    missing any of them did not come from one of Kiro Crew's own directive tools.
    """
    if kind not in DIRECTIVE_TOOLS:
        raise ValueError(f"unknown directive kind: {kind!r}")
    if not session_key:
        raise ValueError("session_key required")
    if not input_digest or not isinstance(input_digest, str):
        raise ValueError("input_digest required")
    rec_id = uuid.uuid4().hex
    now = time.monotonic()
    record: dict[str, Any] = {
        "id": rec_id,
        "kind": kind,
        "args": dict(args or {}),
        "input_digest": input_digest,
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


def claim(
    session_key: str,
    input_digest: str,
    *,
    not_before: float | None = None,
) -> dict[str, Any] | None:
    """Remove and return the ONE record for *session_key* parked under *input_digest*.

    CORRELATED by construction, which is what makes the out-of-band path safe to
    act on (see the module docstring): the caller passes the digest of the raw
    arguments it saw the model make the tool call with, and only a record the tool
    parked under that same digest is returned. An uncorrelated drain would apply
    whatever happened to be queued — including a record another session's caller
    parked here, or one left by a turn that was cancelled before it could consume
    it.

    *not_before* bounds the record to the claiming TURN (pass the turn's start
    from ``time.monotonic()``). A directive belongs to the turn that asked for it:
    without this bound, a record whose turn was abandoned stays claimable by any
    later frame carrying the same input, so a cancelled intent could land minutes
    later.

    Single-consume: the match is removed under the lock, so two consumers racing
    the same session cannot both apply it. FIFO among equals: the model calling
    the same tool twice with identical arguments parks two records under one
    digest, and each frame consumes its OWN — the queue is oldest-first, so the
    first match pairs frame N with record N while the rest stay for the sibling
    frames. Returns ``None`` when nothing matches.
    """
    if not session_key or not input_digest:
        logger.warning(
            "session-directive CLAIM REFUSED before lookup: session_key=%r "
            "input_digest=%s (empty session key or empty digest). Nothing was claimed.",
            session_key,
            (input_digest or "")[:12] or "empty",
        )
        return None
    now = time.monotonic()
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
            _rec_digest = str(record.get("input_digest") or "")
            if (
                hit is None
                and _rec_digest == input_digest
                and (not_before is None or at >= not_before)
            ):
                hit = record
                continue
            if hit is None:
                # Name the ONE reason this candidate was passed over. Ordered so
                # the first true predicate is the decisive one, because a record
                # failing on digest is a different bug from one failing only on
                # the turn bound -- and a silent None could not tell them apart.
                # Digest prefixes only: the digest is a one-way handle over the
                # call input and reveals nothing, but a full 64-char pair makes
                # the line unreadable.
                if _rec_digest != input_digest:
                    misses.append(
                        "input-differs(kind=%r parked=%s wanted=%s)"
                        % (record.get("kind"), _rec_digest[:12] or "empty", input_digest[:12])
                    )
                elif not_before is not None and at < not_before:
                    misses.append(
                        "parked-before-this-turn(at=%.1f < turn_start=%.1f, %.1fs earlier)"
                        % (at, not_before, not_before - at)
                    )
                else:
                    misses.append(
                        "already-matched-a-sibling-frame(kind=%r)" % (record.get("kind"),)
                    )
            keep.append(record)
        if hit is None:
            if misses:
                logger.warning(
                    "session-directive CLAIM MISS for session_key=%r input_digest=%s: %d "
                    "parked record(s) were examined and none matched -> %s. The "
                    "record stays parked; the directive is NOT applied.",
                    session_key,
                    input_digest[:12],
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
