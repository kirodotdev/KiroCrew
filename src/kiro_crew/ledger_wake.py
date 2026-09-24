"""Decides whether a conductor's work ledger changed in a way that needs a turn.

The pure half of the ``work-ledger`` watch. It answers three questions about a
conductor's items -- is this revision new, does anything in it need the
conductor, and is the whole goal finished -- and it answers them from values the
caller already holds. Nothing here reads the dashboard, starts a turn or touches
:mod:`kiro_crew.irq` state, so the gate's policy can be tested without a gateway
and a probe defect can never be mistaken for a policy defect.

The generic half is already built and needs no change: :mod:`kiro_crew.irq` owns
state persistence, per-epoch reset, time-bounded dedupe, the coalescing window
and the consecutive-failure backstop, and ``autonudge._monitor_tick_is_quiet``
turns a quiet verdict into a re-arm that spends no model turn.

What this module deliberately does NOT reimplement: the liveness conjunction.
``work_ledger.is_stale`` already requires the staleness window AND a
non-running worker AND a last report that leaves the next move with the worker,
including the case of a ``done`` item its conductor ruled ``verdict: fail`` on
and left open. Restating any of that here would be a second copy of a rule that
already has one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import time
from typing import Any

from kiro_crew import irq, work_ledger

logger = logging.getLogger(__name__)

#: Worker statuses whose ARRIVAL needs the conductor. ``done`` is a claim it must
#: verify, ``blocked`` an external dependency it must clear, ``question`` a
#: decision only it can make. ``progress`` is deliberately absent: it advances the
#: revision so it reaches the conductor on the next real wake, and charging a turn
#: for it would rebuild the polling this gate exists to remove.
#:
#: Phase 5's ``request`` belongs here too and is NOT listed, because it does not
#: exist on this base: ``work_ledger.WORKER_STATUSES`` is exactly
#: ``{progress, done, blocked, question}``. Add it in the change that adds the
#: status, so the set and the vocabulary never disagree.
WAKE_STATUSES = frozenset({"done", "blocked", "question"})

#: Event kinds a worker can produce. Only ``report`` is one -- every other kind in
#: ``work_ledger.EVENT_KINDS`` (``create``, ``bind``, ``decision``, ``verdict``,
#: ``close``) is written by the CONDUCTOR itself, and a gate that wakes on its
#: owner's own writes never sleeps. This is the same rule the RFC states for
#: Phase 5's ``channel_open`` / ``channel_close``, applied to the kinds that are
#: actually on this base.
WORKER_EVENT_KINDS = frozenset({"report"})

#: Wakes one item may cause per hour. Past it the revision still advances and the
#: board still updates; the wake folds into the next delivered one, so the
#: conductor sees the newest state rather than a queue of superseded ones. This is
#: additive to ``irq``'s coalescing window, which bounds a burst rather than a
#: rate.
MAX_WAKES_PER_ITEM_PER_HOUR = 12

_RATE_WINDOW_SECS = 3600.0

#: Items one conductor tracks in the rate file. ``work_ledger`` caps a conductor
#: at ``MAX_ITEMS_PER_CONDUCTOR`` items, so this is derived from that rather than
#: guessed, with room for closed items still in the window.
_MAX_TRACKED_ITEMS = 4 * work_ledger.MAX_ITEMS_PER_CONDUCTOR

#: Tokens remembered per item, so a tick that produces more than one wake for the
#: same item does not evict its own exemptions. Derived, not guessed: it has to cover
#: everything one item can be charged for inside a window, and that is the window's
#: own budget -- which also dominates the probe's eight-event tail. A smaller bound
#: silently reintroduces the bug it was added for: the tick re-reports its live
#: tokens, the evicted ones are charged again, and the budget is spent on unchanged
#: facts until a genuinely new report is refused.
_MAX_REMEMBERED_TOKENS = MAX_WAKES_PER_ITEM_PER_HOUR

#: Charge moments stored per item. One more than the budget: the budget's own count
#: is what the window needs, and the extra slot keeps a moment that arrives while the
#: item is already at its cap from displacing one that is still inside the window.
_MAX_STORED_WAKES = MAX_WAKES_PER_ITEM_PER_HOUR + 1


def _recent_tokens(entry: Any) -> tuple[str, ...]:
    """The tokens already charged for one item, newest first."""
    if not isinstance(entry, dict):
        return ()
    raw = entry.get("recent_tokens")
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if isinstance(item, str))
    return ()


def _wake_times(entry: Any, *, since: float) -> tuple[float, ...]:
    """One item's charge moments at or after *since*, newest first.

    The window is counted from these rather than from a start-of-window marker plus
    a tally. A marker-and-tally resets the tally when the marker expires, which caps
    each hour-long block independently and so permits twice the budget across the
    boundary between two of them: a full allowance late in one block and another full
    allowance early in the next both satisfy it, while an hour containing both does
    not. Counting the moments answers for whichever hour is being asked about.

    Entries that are not usable numbers are dropped by :func:`_as_number`, which reads
    them as older than every real moment. A file written before this shape existed
    therefore has no moments to count and reads as an unspent budget, which costs at
    most one window's wakes once.

    The comparison is strict, so a moment exactly one window old has aged out. That is
    the boundary the marker-and-tally form had, and keeping it means an item charged to
    its cap in one instant may spend again exactly one window later rather than one
    tick after that.
    """
    if not isinstance(entry, dict):
        return ()
    raw = entry.get("wakes")
    if not isinstance(raw, list):
        return ()
    moments = [_as_number(value) for value in raw]
    kept = sorted((moment for moment in moments if moment > since), reverse=True)
    return tuple(kept[:_MAX_STORED_WAKES])


#: Refuse to parse the rate file past this size: it is a bounded map of short ids to
#: a bounded list of moments and a bounded list of tokens, so anything larger is
#: damage rather than a record to trust.
_MAX_RATE_BYTES = 256_000


def worker_running(slot_table: Any, session_key: str) -> bool:
    """Whether *session_key*'s slot has a TURN IN FLIGHT, per *slot_table*.

    The gate's liveness question, as a function of a value rather than as a closure
    inside a gateway constructor. It lives here so it can be tested without a live
    gateway: the driver binds it to the dashboard's slot table and hands the result
    to the probe, and nothing in this module imports the dashboard to do it.

    RUNNING, not merely open. A worker whose tab is still there but which stopped
    without reporting is exactly the case a stall wake exists to surface, so testing
    existence would never flag it.

    Both slot spellings are tried because a dashboard slot is registered under a
    prefixed key as well as its bare one -- the same lookup the work ledger's own
    HTTP reader performs. Anything unreadable answers False, which is the direction
    that cannot suppress a stall wake.
    """
    if slot_table is None or not session_key:
        return False
    getter = getattr(slot_table, "get_slot", None)
    if not callable(getter):
        return False
    for candidate in (session_key, f"dashboard_{session_key}"):
        try:
            slot = getter(candidate)
        except Exception:
            logger.debug("wake gate: slot lookup failed for %s", candidate, exc_info=True)
            continue
        if slot is not None:
            # ``turn_running``, NOT ``running``. The slot's own docstring says so:
            # ``running`` is an admission predicate, true when a turn is executing OR
            # when a stage boundary is merely armed, so a PAUSED stage would read as a
            # live worker. Here that is the dangerous direction -- a worker read as
            # live has its stall wake SUPPRESSED, which is what this module's
            # docstring forbids an uncertain liveness from doing. A slot that does not
            # answer this attribute at all is treated as idle for the same reason.
            return bool(getattr(slot, "turn_running", False))
    return False


def revision(newest_event_ids: dict[str, str]) -> str:
    """The epoch token for one tick: a digest over the newest event id per item.

    ``irq`` wipes its dedupe memory when the epoch changes, so this has to change
    exactly when something happened and not otherwise. Event ids are
    content-addressed (``work_ledger.event_id``), which is what makes this stable
    across a re-read of an unchanged ledger -- a wall clock in the token would
    make every tick look like a new revision and defeat the dedupe entirely.

    Sorted by item id so the digest does not depend on directory order, and empty
    for a conductor with no items, which disables epoch resets rather than
    asserting a revision that describes nothing.
    """
    if not newest_event_ids:
        return ""
    packed = json.dumps(sorted(newest_event_ids.items()), separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()[:16]


def is_actionable_event(kind: str, status: str | None) -> bool:
    """Whether one work-ledger event needs the conductor.

    A worker's ``report`` carrying a waking status, and nothing else. The kind is
    checked as well as the status because a conductor's own ``verdict`` event also
    carries a status-shaped field, and a gate woken by its owner's writes would
    wake on every ruling it made.
    """
    if (kind or "").strip() not in WORKER_EVENT_KINDS:
        return False
    return (status or "").strip() in WAKE_STATUSES


def _rate_path(conductor_key: str):
    """Where this conductor's wake budget lives: under the WATCH state root.

    Deliberately not ``session_ledger.control_dir``. That directory holds
    fence-inherited control state which every purge and sweep skips BY DESIGN, so a
    counter placed there would be actively protected from the cleanup it wants, and
    would sit among files an operator reads as governing a slot's fold. The budget is
    the opposite kind of state: worthless once the watch ends, and safe to lose at any
    moment because losing it costs at most one extra wake.

    ``irq.state_path`` puts it beside the watch state, whose retention it now shares:
    ``irq`` does NOT delete its state files, and nothing sweeps that directory today,
    so this file is retained rather than collected. What the move buys is that it is
    retained somewhere an operator or a future sweeper may freely delete, instead of
    somewhere deletion is deliberately prevented. The fold/digest naming is reused
    rather than reinvented. The job id is fixed because the budget is per ITEM per
    hour across that conductor's watches -- two watches on one ledger share the
    allowance, since the item they would wake for is the same item.
    """
    return irq.state_path("work-ledger-rate", conductor_key, "rate")


def _read_rate(conductor_key: str) -> dict[str, Any]:
    """The rate map, or empty when there is nothing to trust.

    Best-effort by contract: a missing map means nothing has been rate-limited
    yet, and a damaged one is discarded rather than half-read, which costs at most
    one extra wake. Never creates the directory -- a read that answers "nothing
    recorded" needs none, and a reader that made one would leave residue for every
    conductor anything ever asked about.
    """
    try:
        path = _rate_path(conductor_key)
        if not path.is_file():
            return {}
        if path.stat().st_size > _MAX_RATE_BYTES:
            logger.warning("wake gate: the rate file is past its own bound; ignoring it")
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("wake gate: reading the rate file failed", exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


def _as_number(value: Any) -> float:
    """*value* as a FINITE float, or ``-1`` when it is not one.

    The file is on disk and its contents are not this module's to promise, so an
    unusable entry reads as older than every real one instead of raising inside a
    comparison.

    Three rejections, not one. ``float()`` raises ``TypeError``/``ValueError`` on the
    obvious junk, ``OverflowError`` on an integer too large to be a float -- which is
    a plain ``int`` in JSON and so entirely reachable from a stored file -- and it
    happily RETURNS ``nan`` for ``"nan"``. A NaN is the worst of the three because it
    does not raise: every comparison against it is false, so the window would neither
    open nor expire and each actionable tick would fall back to the timer it exists to
    replace.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return -1.0
    if not math.isfinite(number):
        return -1.0
    return number


def within_rate_limit(
    conductor_key: str, item_id: str, *, token: str = "", now: float | None = None
) -> bool:
    """Whether a wake for *item_id* may fire now.

    Read-only: it answers without spending the budget, so an item refused for some
    other reason has not used a wake. :func:`note_wake` is what spends it.

    The window is sliding rather than fixed, so an item that was noisy for an hour
    is not muted for the rest of the run, and no hour anywhere permits more than the
    budget. A start-of-window marker with a tally would give both: it resets on
    expiry, so a full allowance just before a reset and another just after it both
    pass while the hour spanning them holds twice the budget.

    *token* identifies WHICH wake is being asked about -- an event id, or an
    item's silence. A probe re-reports a condition on every tick until the
    kernel's own mask expires, and charging each of those re-reads would spend the
    hour's allowance on one unchanged fact and then swallow the genuinely new
    report that follows it. So a token that was charged recently is always allowed
    again: it is the same wake, already accounted, and the kernel decides whether
    it is delivered.

    Several tokens are remembered, not one. A single item can produce TWO wakes in
    the same tick -- a worker reported ``blocked`` and then stopped, so the report's
    event id and the silence are both live -- and with one remembered token each
    tick would evict the other's exemption and re-charge it, emptying the budget in
    half the ticks it is meant to cover.
    """
    moment = time.time() if now is None else now
    entry = _read_rate(conductor_key).get(item_id)
    if not isinstance(entry, dict):
        return True
    if token and token in _recent_tokens(entry):
        return True
    charged = _wake_times(entry, since=moment - _RATE_WINDOW_SECS)
    return len(charged) < MAX_WAKES_PER_ITEM_PER_HOUR


def note_wake(
    conductor_key: str, item_id: str, *, token: str = "", now: float | None = None
) -> bool:
    """Spend one wake from *item_id*'s budget; whether the write landed.

    Returns False rather than raising: the caller is a probe tick, and a
    maintenance file it could not persist must not turn a real wake into an error.
    A caller that treats False as authorization has no bound at all, though, because
    an unwritable file leaves every later tick reading an unspent budget -- so the
    probe refuses the wake it could not charge and folds it forward instead.

    Re-charging a REMEMBERED token is a no-op on the count, for the reason
    :func:`within_rate_limit` gives, but the write still happens so the pruned
    moment list is recorded.
    """
    moment = time.time() if now is None else now
    current = _read_rate(conductor_key)
    entry = current.get(item_id)
    remembered = _recent_tokens(entry) if isinstance(entry, dict) else ()
    repeat = bool(token) and token in remembered
    charged = list(_wake_times(entry, since=moment - _RATE_WINDOW_SECS))
    if not isinstance(entry, dict):
        entry = {}
        remembered = ()
        repeat = False
    if not repeat:
        charged = [moment] + charged
    entry["wakes"] = charged[:_MAX_STORED_WAKES]
    entry.pop("window_start", None)
    entry.pop("count", None)
    if token:
        # Newest first, bounded: one item's live wakes are its newest report and its
        # silence, so a handful covers every real case while keeping the file small.
        entry["recent_tokens"] = [token] + [t for t in remembered if t != token][
            : _MAX_REMEMBERED_TOKENS - 1
        ]
    current[item_id] = entry
    if len(current) > _MAX_TRACKED_ITEMS:
        ordered = sorted(
            current.items(),
            key=lambda kv: max(_wake_times(kv[1], since=-1.0), default=-1.0),
            reverse=True,
        )
        current = dict(ordered[:_MAX_TRACKED_ITEMS])
    try:
        path = _rate_path(conductor_key)
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink():
            # The redirect an unpredictable temporary name does not stop, because it
            # is one level up. ``mkdir(exist_ok=True)`` succeeds against a symlink to
            # a directory, and ``mkstemp(dir=...)`` then creates inside whatever it
            # points at, so the rename lands the rate file in a directory of someone
            # else's choosing. The destination rename itself does not follow a link
            # -- ``os.replace`` replaces a symlink at *path* rather than its target --
            # so the directory is the component that has to be checked. An ancestor
            # above the watch root belongs to the module that owns that root.
            logger.warning(
                "wake gate: the rate file's directory is a symlink; refusing to write through it"
            )
            return False
        # UNPREDICTABLE temporary name, not "<name>.tmp". A guessable temporary is
        # pre-plantable: an actor who can create entries in this directory puts a
        # symlink there first, and the write below follows it and overwrites the
        # target. ``mkstemp`` picks a name that cannot be guessed AND creates with
        # O_EXCL at 0600, so the create fails rather than following anything that
        # already exists. This is the rule the directory's owner states for its own
        # state files, and the rate file lives beside them.
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f"{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, sort_keys=True)
            os.replace(tmp_name, path)
        except BaseException:
            # Nothing sweeps this directory, so a temporary left behind by a failed
            # write is permanent: the cleanup is load-bearing, not boilerplate.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except (OSError, ValueError, TypeError):
        logger.debug("wake gate: writing the rate file failed", exc_info=True)
        return False
    return True


def wake_brief(*, item_id: str, status: str) -> str:
    """One line of operator-facing text for an item that needs the conductor.

    STRUCTURAL ONLY: an item id and a status, never ledger prose. ``irq`` persists
    an observation's brief into its own watch-state file so a coalescing window
    survives a restart, and that file is not the work ledger -- it is not under the
    store's identity-gated read path, and anything able to read the watch directory
    can read it. A worker's reported summary copied in here would therefore leave
    the boundary the store's importer allowlist exists to draw, without the
    server-resolved identity check that path requires.

    Nothing is lost by it. The conductor reads the record with ``work_ledger_read``,
    which is exactly what the footer tells it to do, so the wake's job is to name
    WHICH item moved and how, not to restate what the tool returns whole.
    """
    return f"[work-ledger wake] item={item_id} status={status or '(unset)'}"


def stall_brief(*, item_id: str, status: str | None) -> str:
    """Text for a SILENCE wake, marked as such.

    A different shape from :func:`wake_brief` because it says the opposite thing:
    not "this worker reported" but "this worker has reported nothing and is no
    longer running". A conductor that could not tell them apart would go looking
    for a report that does not exist.

    Structural only, for the reason :func:`wake_brief` gives.
    """
    return f"[work-ledger wake] item={item_id} reason=stall last_status={status or '(none yet)'}"
