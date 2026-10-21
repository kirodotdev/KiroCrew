"""What the reader drops or truncates, counted instead of logged per frame.

The backend subagent roster and its overflow episodes, and the unroutable-frame drop summaries.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dropped_frames_flushed_at``, ``_roster_overflow_peak``,
``_roster_overflow_repeats``, ``_roster_overflow_summary_at``, ``_subagent_owner``,
``_subagent_roster_overflow``, ``_subagent_sessions``.

Instance fields read: ``_dropped_frames``, ``_session_queues``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.acp._dispatch import redact_backend_text
from kiro_crew.acp.client import _loggable_request_id
from kiro_crew.acp.session_handle import NATIVE_CHILD_ROSTER_CAP
from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION
from kiro_crew.metrics.events import DROPPED_FRAMES, emit_counter
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


#
# The reader drops any frame it cannot route (see _reader_loop). That is
# CORRECT behaviour, but logging it per frame is not: kiro-cli is multiplexed,
# so every frame for a torn-down or not-yet-registered sessionId takes the drop
# branch, and a backend that keeps streaming after teardown makes that an
# unbounded STEADY STATE, not a burst. Measured on an operator host: ~60
# lines/second for 6+ hours from one gateway PID, taking 33–59% of every
# gateway.log rotation — which, at RotatingFileHandler(maxBytes=2MB,
# backupCount=3) (see cli_logging.py), rolls the genuine diagnostics needed for an
# incident out of the retained 8MB window before anyone can read them.
#
# So the per-frame line is collapsed into a periodic count keyed by
# (sessionId, method). The key must stay PER SESSION: the incident's decisive
# signal was that two DIFFERENT session UUIDs were flooding at once, which a
# single global tally would hide.
_DROP_SUMMARY_INTERVAL_SECS = 60.0
# Hard cap on distinct (sessionId, method) keys held between flushes. Both
# halves of the key are backend-controlled, so an unbounded map would be a
# memory sink; reaching the cap forces an early flush instead of growing.
_DROP_SUMMARY_MAX_KEYS = 64
# Backend-controlled key text is redacted whole and then capped before it is
# stored, so a pathological sessionId/method (a stdout line may be up to
# _STDOUT_BUFFER_LIMIT) cannot be retained at full length by the map either.
_DROP_SUMMARY_KEY_MAX_CHARS = 80
# Stands in for the sessionId half of the key on the no-sessionId broadcast
# path, which has no session to name.
_DROP_NO_SESSION = "-"
# Stands in for EITHER half of the key when the backend supplied no usable
# string: an absent `method`, or a value of the wrong JSON type (see
# _drop_key_part).
_DROP_KEY_PLACEHOLDER = "?"

# SEL/metric reason for a permission request auto-rejected while the last
# subagent roster named more children than NATIVE_CHILD_ROSTER_CAP. Distinct
# from `unregistered_session_auto_reject` because the two say different things:
# that reason claims the backend never announced the session, and past the cap
# the backend may well have — the id simply is not remembered, so it cannot be
# verified as this owner's child. Auditing an announced child's denial as
# "unregistered" would make the one signal that a cap truncation cost a real
# approval indistinguishable from an ordinary unknown-session frame.
_ROSTER_OVERFLOW_REJECT_REASON = "roster_overflow_auto_reject"


# Minimum seconds between throttled DEBUG summaries of repeated roster
# truncation. A `subagent/list_update` is a backend-controlled notification
# re-broadcast on every child status change, so above the cap the WARNING it
# earns is a steady state at the frame rate, not a burst — the same retention
# hazard `_note_dropped_frame` exists to avoid, one level louder. Bound to the
# unroutable-frame interval rather than given a second value: both answer the
# one question "how often may a suppressed high-frequency condition restate
# itself in gateway.log", and a second knob is a second throttle to reason
# about in the same module.
_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS = _DROP_SUMMARY_INTERVAL_SECS


def _drop_key_part(value: object) -> str:
    """Bounded, hashable string for one half of a (sessionId, method) drop key.

    BOTH halves arrive verbatim from backend JSON: `JsonRpcMessage.from_dict`
    copies `method` and `params` with no validation, so the `str | None`
    annotation is documentation, not enforcement — `{"method": 123}` yields an
    int, and `params.sessionId` is `Any`. Slicing such a value raises TypeError
    *inside* `_reader_loop`, the single owner of this process's stdout, which
    marks the runtime dead and tears down EVERY multiplexed session on it. One
    malformed frame must not cost every session, so anything that is not a
    `str` (including the legitimate absent-`method` `None`) becomes the
    placeholder before it is sliced or used as a dict key.
    """
    if not isinstance(value, str):
        return _DROP_KEY_PLACEHOLDER
    # Redact BEFORE the retention cap: the key text is backend-authored and
    # the flush logs it, so a slice taken first could sever a credential at
    # the cut into a fragment that matches none of the redactor's patterns.
    return redact_backend_text(value)[:_DROP_SUMMARY_KEY_MAX_CHARS]


def _snapshot_subagent_sessions(self: AcpRuntime, params: dict) -> None:
    """Replace the known backend-subagent session-id set from a list_update.

    The frame carries the backend's FULL current subagent list (kiro-cli
    rebuilds it from `orchestrated_sessions` on every change), so replacing
    the set keeps it bounded and self-cleaning: terminated children vanish
    from the next update. Ids are backend-controlled — length-capped and
    type-checked so a hostile payload cannot grow memory unboundedly.

    Membership here is what the routing branch decides a child's approvals
    on, so the entry bound is the SHARED native-child cap
    `NATIVE_CHILD_ROSTER_CAP` — the same number `AcpSessionHandle` remembers
    child ids under — and never a tighter per-frame slice. A tighter bound
    would deny mode parity to children the very same payload made the
    handle count: an id the set does not hold is unrecognisable, its
    `session/update` is a counted drop and its permission request is
    auto-rejected, so a cap below the handle's turns one announced roster
    into two governance classes by list position alone.

    Ids past the cap are counted in `_subagent_roster_overflow`, and the
    truncation is reported through `_note_roster_overflow`: a truncated tail
    otherwise reads exactly like a roster that never named those children.
    The count is per SNAPSHOT, not cumulative — the frame is the full list,
    so replacement makes it idempotent under repeated identical rosters even
    above the cap (the handle's counter accumulates instead, because ids
    reach it one frame at a time and only a per-turn total can describe that
    stream). The LOG is episode-scoped rather than per-snapshot, which is a
    different lifetime and deliberately so: see `_note_roster_overflow`.

    The scan visits every entry even above the cap, on the SHARED demux
    loop. That is a constant factor on work already done: `json.loads` has
    walked the whole list before this method is called, and the frame is
    already bounded by `_STDOUT_BUFFER_LIMIT`. Slicing to save the pass is
    what cost the tail its governance.
    """
    raw = params.get("subagents")
    if not isinstance(raw, list):
        return
    ids: set[str] = set()
    overflow = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sessionId") or entry.get("session_id")
        if not isinstance(sid, str) or not sid or len(sid) > MAX_ACP_SESSION_ID_LEN:
            continue
        if sid in ids:
            continue
        if len(ids) >= NATIVE_CHILD_ROSTER_CAP:
            # Past the cap an id is counted, never stored: recognising a
            # repeat would mean remembering it, which is the one thing the
            # cap refuses.
            overflow += 1
            continue
        ids.add(sid)
    self._subagent_sessions = ids
    self._subagent_roster_overflow = overflow
    if overflow:
        self._note_roster_overflow(overflow)
    else:
        self._end_roster_overflow_episode()
    # Ownership is provable only when exactly one session is registered:
    # the announce demonstrably belongs to it. Otherwise no owner, and
    # routing stays fail-closed.
    self._subagent_owner = (
        next(iter(self._session_queues)) if len(self._session_queues) == 1 else None
    )


def _note_roster_overflow(self: AcpRuntime, overflow: int) -> None:
    """Report a truncated roster snapshot: loud ONCE per episode, then counted.

    The first truncation an episode sees is a `WARNING`, because a truncated
    tail is otherwise invisible — nothing else in the log distinguishes it
    from a roster that never named those children, and an operator has to
    see it to decide whether the cap needs raising. What must not recur is
    the per-FRAME repeat: `subagent/list_update` is a backend-controlled
    notification re-broadcast on every child status change, so above the cap
    each rebroadcast re-earns the same warning at the backend's frame rate.
    Measured on this handler: 10 over-cap snapshots → 10 identical WARNING
    records at ~245 message bytes each, whether the frames are byte-identical
    or differ only in a child's `status`. That is the retention hazard
    `_note_dropped_frame` documents above (~60 lines/second rolling incident
    evidence out of `gateway.log`'s 8MB window), one level louder.

    So later truncations inside the episode are tallied and flushed as one
    throttled `DEBUG` summary, at most every
    `_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS`, carrying how many snapshots
    repeated and the LARGEST tail they named. The peak, not the latest
    value: sizing the cap reads the worst case, and which value happens to
    be current at an arbitrary flush instant is noise.

    **The reset boundary is the truncation EPISODE** — it re-arms when the
    overflow count returns to 0, which is exactly the two events that
    already retire the snapshot attribution: a later roster that truncated
    nothing, and the owning session unregistering. One lifetime governs both
    halves of the same signal, so the loud line and the auto-reject reason
    can never disagree about whether the cap is under pressure. A plain
    per-interval re-arm is the alternative, and it is what the drop counter
    does, but the drop summary is `DEBUG`: re-arming a WARNING on a timer
    restates it every interval for as long as the steady state lasts, which
    is the volume this throttle exists to remove.

    The residual, stated rather than implied: a tail that GROWS inside one
    episode (40 ids, later 40000) is loud only at its first value, and the
    growth is visible in the summary's peak at `DEBUG`. Re-warning on growth
    would need a second threshold — a second throttle shape in a module that
    already has one.

    Synchronous and awaitless, like the drop counter, because the caller is
    on the shared demux loop.
    """
    now = time.monotonic()
    if self._roster_overflow_summary_at == 0.0:
        # No episode open: this snapshot is the loud one, and it opens the
        # window the repeats below are measured against. __init__ cannot
        # supply that baseline — a runtime may be constructed long before
        # spawn(), and a stale timestamp would make the first repeat flush
        # immediately instead of aggregating.
        self._roster_overflow_summary_at = now
        logger.warning(
            "subagent roster announced %d children past the %d-id recognition "
            "cap; their session/update frames are counted drops and their "
            "permission requests are auto-rejected (reason %s) rather than "
            "reaching the approval pipeline. Further truncated snapshots are "
            "summarized at DEBUG until a roster inside the cap arrives",
            overflow,
            NATIVE_CHILD_ROSTER_CAP,
            _ROSTER_OVERFLOW_REJECT_REASON,
        )
        return
    self._roster_overflow_repeats += 1
    self._roster_overflow_peak = max(self._roster_overflow_peak, overflow)
    if now - self._roster_overflow_summary_at >= _ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS:
        self._flush_roster_overflow(now)


def _flush_roster_overflow(self: AcpRuntime, now: float | None = None) -> None:
    """Emit the repeated-truncation summary and reopen the throttle window."""
    self._roster_overflow_summary_at = time.monotonic() if now is None else now
    if not self._roster_overflow_repeats:
        return
    logger.debug(
        "subagent roster truncated on %d further snapshot(s); largest tail %d "
        "id(s) past the %d-id recognition cap",
        self._roster_overflow_repeats,
        self._roster_overflow_peak,
        NATIVE_CHILD_ROSTER_CAP,
    )
    self._roster_overflow_repeats = 0
    self._roster_overflow_peak = 0


def _end_roster_overflow_episode(self: AcpRuntime) -> None:
    """Close a truncation episode: flush the residual count, re-arm the warning.

    Called wherever `_subagent_roster_overflow` returns to 0 — a roster
    inside the cap, or the owning session unregistering. Flushing first is
    what keeps a SHORT episode honest: repeats accrued in under one interval
    would otherwise be dropped on the floor, and then a truncation storm
    that ends quickly would report only its first frame. Re-arming is what
    keeps the NEXT truncation loud; without it the throttle would swallow a
    genuinely new one for the rest of the runtime's life.
    """
    self._flush_roster_overflow()
    self._roster_overflow_summary_at = 0.0


def _note_dropped_frame(self: AcpRuntime, session_id: object, method: object) -> None:
    """Count one unroutable frame, flushing a summary at most once per interval.

    Replaces a per-frame log line (see the drop-accounting constants above).
    Cheap and synchronous by design: it is called from the hot demux path
    and must not await, so there is no timer task to leak and no blocking
    I/O beyond the throttled ``logger.debug`` the flush itself emits.

    Both arguments are backend-controlled and deliberately typed `object`:
    they are normalized through `_drop_key_part`, which is the only thing
    that keeps a wrong-typed value from raising in the shared reader.
    """
    key = (_drop_key_part(session_id), _drop_key_part(method))
    # Hang-resilience series: classify the drop by method so dashboards
    # can alert on the pre-fix hang signature. ``method_class`` is a
    # closed 3-value enum — the raw method (backend-controlled) never
    # becomes an attribute value.
    _m = method if isinstance(method, str) else ""
    if _m == METHOD_REQUEST_PERMISSION:
        _mclass = "permission"
    elif _m in self._notification_aliases().session_update:
        # EVERY session-update spelling this host uses classifies as
        # "update": a dashboard alerting on the pre-fix hang signature must
        # see a dropped extension-method child update the same way it sees
        # the plain spelling, and a spelling the host's aliases omit would be
        # counted as "other" and lost.
        _mclass = "update"
    else:
        _mclass = "other"
    emit_counter(DROPPED_FRAMES, {"method_class": _mclass})
    now = time.monotonic()
    if self._dropped_frames_flushed_at == 0.0:
        # First drop of this runtime's life opens the window. __init__ cannot
        # supply the baseline (a runtime may be constructed long before
        # spawn()), and a stale 0.0 would make every first drop flush
        # immediately instead of aggregating.
        self._dropped_frames_flushed_at = now
    counts = self._dropped_frames
    if key not in counts and len(counts) >= _DROP_SUMMARY_MAX_KEYS:
        # A wide fan-out of distinct keys inside one interval must not grow
        # the map; report what we have and start a fresh window.
        self._flush_dropped_frames(now)
    counts[key] = counts.get(key, 0) + 1
    if now - self._dropped_frames_flushed_at >= _DROP_SUMMARY_INTERVAL_SECS:
        self._flush_dropped_frames(now)


def _flush_dropped_frames(self: AcpRuntime, now: float | None = None) -> None:
    """Emit one summary record per (sessionId, method) and reset the window.

    Called on the interval from _note_dropped_frame and unconditionally when
    the reader loop exits, so a low-rate trickle is reported late rather
    than swallowed. A key seen once in an otherwise idle hour is therefore
    reported at the next drop or at loop exit — deliberately traded for
    having no wakeup timer on the event loop.
    """
    self._dropped_frames_flushed_at = time.monotonic() if now is None else now
    counts = self._dropped_frames
    if not counts:
        return
    for (session_id, method), count in counts.items():
        logger.debug(
            "Dropped %d unroutable frame(s) for session %s (method=%s)",
            count,
            _loggable_request_id(session_id),
            _loggable_request_id(method),
        )
    counts.clear()
