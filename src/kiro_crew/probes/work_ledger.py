"""Observe one conductor's work ledger: the domain half of the wake gate.

A conductor today re-reads its ledger on a timer and spends a model turn to find
out that nothing moved. This probe answers *what does the ledger look like right
now* so :mod:`kiro_crew.irq` can decide, and a tick where no worker said anything
the conductor must act on costs no turn at all.

The split is deliberate and matches :mod:`kiro_crew.probes.gh_pr`. Everything
generic -- state persistence, epoch reset, time-bounded dedupe, the coalescing
window, the consecutive-failure backstop -- is the kernel's. Every POLICY
question -- which statuses need the conductor, which event kinds a worker can
even write, how many wakes one item may cause -- lives in
:mod:`kiro_crew.ledger_wake` as pure functions. What is left here is the reading
itself, which is the only part that needs a disk.

Three facts this probe cannot read for itself and takes as values instead:

* ``worker_running`` -- whether a worker's slot has a turn in flight. That lives
  in the dashboard's in-process slot table, which a probe running on a worker
  thread has no handle on, so the driver injects a resolver.
* ``worker_closed`` -- whether that slot is GONE. Same table, same reason, and a
  separate question: "not running" is true of a worker thinking between turns as
  well as of one that ended, which is why ``is_stale`` makes the first wait out a
  window and lets the second skip it.
* ``worker_looping`` -- whether the worker's own session holds an active loop that
  will wake it. That lives in the AutoNudge service's loop table, not in the
  ledger, so the driver injects a resolver for it too.

All three default to ``False`` rather than to nothing. For ``worker_running`` and
``worker_looping`` that is the safe direction: "not running" and "not looping" can
only make the gate LOUDER (a stall wake the conductor did not need costs one turn),
never quieter about a worker that stopped, and the opposite default would turn a
missing handle into a watch that never reports a stalled worker, which is the
failure this gate exists to remove. ``worker_closed`` is the other way round:
``True`` is the answer that flags, so "not closed" is the default that cannot invent
a stall, and a worker that is gone is judged as a quiet one (see
:func:`_always_open`).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from kiro_crew import irq, ledger_wake, work_ledger, work_preview
from kiro_crew.work_vocab import (
    WORK_BLOCKED_REASONS,
    WORK_REASON_STATUSES,
    WORK_REMOTE_DISPATCH_STATUSES,
)

logger = logging.getLogger(__name__)

#: This probe's subject kind. ``probes/__init__`` imports this module only inside
#: ``build``, which keeps the ledger store off the gateway boot path, so it spells the
#: kind a second time as its own literal (``WORK_LEDGER``) rather than importing this
#: one; the two must stay equal.
WORK_LEDGER_KIND = "work-ledger"

#: Events read per item per tick. The kernel masks a repeat by key, so the tail
#: only has to be long enough that a BURST between two ticks is not missed: a
#: worker that reports ``blocked`` and then ``progress`` before the next tick must
#: still wake its conductor on the ``blocked``. Reading only the newest event
#: would drop exactly that transition. Matches the kernel's own list bound.
_EVENT_TAIL = 8

#: Prefix for a report wake's dedupe key. The rest of the key is the event's
#: content-addressed id, so the key is unique to one line in one item's log.
_REPORT_KEY = "report"

#: Prefix for a silence wake's dedupe key.
_STALL_KEY = "stall"

#: Prefix for an undelivered-report wake's dedupe key
#: (:mod:`kiro_crew.work_ledger_undelivered`).
_UNDELIVERED_KEY = "undelivered"

#: The one terminal item state that means the item's bar was MET. The other two
#: (``rejected``, ``abandoned``) close an item without meeting it, so a ledger is
#: only a success when every item ended in THIS state. Pinned against
#: :data:`~kiro_crew.work_ledger.TERMINAL_ITEM_STATES` by a test, because a rename
#: there would otherwise make every finished ledger read as a failure in silence.
ACCEPTED_STATE = "accepted"

#: Dedupe key for "the whole goal is finished, and every item's bar was met". One
#: per subject, since a watch that ends has nothing further to say. PUBLIC because a
#: driver records success or failure from a probe's own terminal keys, so this kind's
#: key has to be nameable from outside: read through the pull-request vocabulary
#: instead, a conductor whose every item is closed reads as not-merged and is
#: persisted as blocked.
ACCEPTED_KEY = "all-accepted"

#: Dedupe key for "the whole goal is finished, and at least one item was NOT
#: accepted". Equally terminal -- a rejected goal leaves nothing to watch either --
#: and deliberately NOT a success key, so a ledger whose items were all rejected or
#: abandoned is not persisted as an outcome its conductor never reached.
CLOSED_KEY = "all-closed"

#: :attr:`WorkLedgerProbe.revision` for a ledger that was read but holds no worker
#: report yet. Not empty, because empty means "not known" to the gate.
_NO_REPORTS = "no-reports"


def _always_idle(_session_key: str) -> bool:
    """Default liveness resolver: nothing is running.

    Not ``True``: an unknown liveness must not be able to SUPPRESS a stall wake.
    See the module docstring for the full argument.
    """
    return False


def _always_open(_session_key: str) -> bool:
    """Default close resolver: nothing is closed.

    Not ``True``, and the reason is the opposite side of :func:`_always_idle`'s. This
    input only ever makes the gate flag MORE or SOONER -- it skips the staleness window
    for an item that has reported, flags a reported ``blocked`` / ``question`` item at
    once, and outranks an active loop -- so answering ``True`` without a handle on the
    slot table would flag an item the moment its worker reported ``progress``: a wake
    per item for a worker that is simply still working. ``False`` falls back to
    measuring the window.
    """
    return False


def _never_looping(_session_key: str) -> bool:
    """Default loop resolver: no worker holds an active loop.

    Not ``True``, for :func:`_always_idle`'s reason: this input only ever SUPPRESSES a
    stall wake, so an unknown answer must not be able to.
    """
    return False


class WorkLedgerProbe(irq.Probe):
    """Watch one conductor's work ledger.

    Args:
        worker_running: ``session_key -> has a turn in flight``. Defaults to
            :func:`_always_idle`.
        worker_closed: ``session_key -> its slot is gone``. Defaults to
            :func:`_always_open`, so a build with no resolver never skips the
            staleness window.
        worker_looping: ``session_key -> its session holds an active loop``.
            Defaults to :func:`_never_looping`, which never suppresses a flag.
    """

    def __init__(
        self,
        *,
        worker_running: Callable[[str], bool] | None = None,
        worker_closed: Callable[[str], bool] | None = None,
        worker_looping: Callable[[str], bool] | None = None,
    ) -> None:
        self._worker_running = worker_running or _always_idle
        self._worker_closed = worker_closed or _always_open
        self._worker_looping = worker_looping or _never_looping
        self._conductor = ""
        #: The polling driver's identity, read off the ctx each tick. The seen set
        #: is kept per driver; see ``ledger_wake._MAX_WATCHERS_PER_ITEM``.
        self._watcher = ""
        #: A digest of each item's worker-owned report fields (``last_report_at``,
        #: ``status``, ``summary``), read on the last successful observation, or
        #: ``None`` when nothing was read. The AutoNudge
        #: gate lengthens a work-ledger watch's quiet floor while it matches the one
        #: its last turn was delivered at. Only worker reports count: the
        #: conductor's own writes (a verdict, a dispatch) are things it already
        #: knows, and counting them would spend the next floor turn reading them
        #: back. A ledger read with no worker report yet still gets a token, so an
        #: early, quiet board is recognised as unchanged too.
        self.revision: str | None = None

    # -- Probe contract ---------------------------------------------------

    def tuning(self) -> dict[str, float]:
        """No coalescing window: every fresh wake in a tick is delivered at once.

        A report is complete the moment it is written, so the kernel's default
        floor would only hold it back. The observations are WAKE rather than
        IMMEDIATE because the kernel delivers an IMMEDIATE on its own and masks
        only that one key: a tick that finds two workers' reports would deliver
        the first and leave the second to wake the conductor again later for news
        the first turn already read off the board. With the window at zero the
        kernel folds every fresh WAKE of the tick into one delivery and masks them
        all together.
        """
        return {"coalesce_secs": 0}

    def identity(self, ctx: object) -> tuple[str, str]:
        """``("work-ledger", <conductor session key>)``, parsed from the message.

        Raises :class:`ValueError` for a message that can never become valid. The
        kernel converts that to ``Done``: a watch whose subject is unnameable
        cannot self-heal, and retrying it every interval is a crash loop wearing a
        schedule.
        """
        self._conductor = _conductor_key(getattr(ctx, "message", ""))
        return (WORK_LEDGER_KIND, self._conductor)

    def observe(self, ctx: object) -> irq.Tick:
        """One bounded read of the conductor's items, classified.

        Never raises for an expected failure. An unreadable ledger returns
        ``fetch_ok=False``, which the kernel turns into a BLIND skip and
        :func:`kiro_crew.irq.poll` into ``FALLBACK`` -- the driver then keeps the
        timer it already had. That is the one answer a missing read is entitled
        to: ``QUIET`` would assert "nothing changed" about a ledger this tick
        never saw, which is how a broken read becomes an indefinitely silent
        watch.
        """
        key = self._conductor or _conductor_key(getattr(ctx, "message", ""))
        self._watcher = str(getattr(getattr(ctx, "job", None), "id", "") or "")
        record = work_ledger.read_conductor(key)
        if record is None:
            # No ledger, or one that could not be read. Deliberately NOT the same
            # answer as "a ledger with no items": a conductor that has not opened
            # one yet is indistinguishable from a torn read at this layer, and the
            # cheap failure is to keep firing on the existing timer.
            return irq.Tick(fetch_ok=False, detail="work ledger unreadable")

        items = work_ledger.list_work_items(key)
        # How many item files EXIST, against how many were readable. ``list_work_items``
        # skips a torn file so one bad item cannot hide the rest, which is right for a
        # board and wrong for a terminal decision: if the only OPEN item is the
        # unreadable one, every item this tick can see is closed and the watch would
        # deactivate while its work is still live. A watch that ends early is silent
        # about the thing it was armed for, so doubt here must keep the watch alive.
        try:
            stored = sum(1 for _ in work_ledger.items_dir(key).glob("it_*.json"))
        except OSError:
            stored = len(items)
        all_readable = stored <= len(items)
        newest: dict[str, str] = {}
        tails: dict[str, list[work_ledger.WorkEvent]] = {}
        for item in items:
            events = work_ledger.read_events(key, item.item_id, limit=_EVENT_TAIL)
            tails[item.item_id] = events
            if events:
                newest[item.item_id] = events[-1].id
        epoch = ledger_wake.revision(newest)
        self.revision = worker_report_revision(items)

        if items and all_readable and all(item.is_terminal for item in items):
            # Every item closed means the goal this ledger serves is finished, so
            # the watch has nothing left to observe. Requires at least one item on
            # purpose: "all of nothing" is vacuously true, and a watch armed
            # BEFORE the conductor mints its first item would otherwise deactivate
            # itself on its very first tick.
            #
            # WHICH terminal key is not cosmetic. A closed item ended accepted,
            # rejected or abandoned, and only the first means its bar was met, so a
            # ledger is a success only when every item ended accepted. Reporting one
            # key for all three would persist a goal whose every item was REJECTED
            # as a succeeded watch -- the same conflation, in the other direction, as
            # reading this kind through the pull-request vocabulary.
            accepted = sum(1 for item in items if item.state == ACCEPTED_STATE)
            finished_well = accepted == len(items)
            detail = (
                f"[work-ledger wake] every item is closed and accepted ({len(items)} total)"
                if finished_well
                else (
                    f"[work-ledger wake] every item is closed, {accepted} accepted "
                    f"of {len(items)}"
                )
            )
            return irq.Tick(
                epoch=epoch,
                observations=[
                    irq.Observation(
                        ACCEPTED_KEY if finished_well else CLOSED_KEY,
                        irq.Severity.TERMINAL,
                        detail,
                        irq.ResetsOn.NEVER,
                    )
                ],
            )

        # A closed item never wakes again, so its seen set has nothing left to
        # suppress. Dropping it here is what keeps that set bounded by the item's
        # life rather than by the ledger's.
        closed = [item.item_id for item in items if item.is_terminal]
        if closed:
            ledger_wake.forget_items(key, closed)

        observations: list[irq.Observation] = []
        for item in items:
            if item.is_terminal:
                continue
            observations.extend(self._item_observations(key, item, tails.get(item.item_id, [])))
        # ``pending`` stays 0. It is what the coalescing window waits to drain, and
        # an item still being worked on never drains -- modelling work-in-progress
        # as pending would hold a ``question`` wake for the window's hard cap while
        # the worker it blocks waits for the answer.
        return irq.Tick(epoch=epoch, observations=observations)

    def wake_suffix(self) -> str:
        """Said once per wake, not once per item."""
        return (
            "The work-ledger watch stays armed. Read the items with work_ledger_read "
            "before acting: this text names what changed, not the whole board."
        )

    # -- internals --------------------------------------------------------

    def _item_observations(
        self,
        conductor_key: str,
        item: work_ledger.WorkItem,
        events: list[work_ledger.WorkEvent],
    ) -> list[irq.Observation]:
        """Everything about one open item that needs the conductor.

        Every observation here is ``WAKE``, and :meth:`tuning` sets the coalescing
        window to zero, so no ``WAKE`` waits out the kernel's default floor
        (``irq.DEFAULT_COALESCE_SECS``): every fresh one in a tick is delivered at
        once, in one delivery, and masked together. A report is complete the moment
        it is written, and a stall is a decision already made, so waiting would
        observe nothing further. ``WAKE`` rather than ``IMMEDIATE`` is what lets two
        reports found in one tick share that delivery; see :meth:`tuning`. These
        keys never recur anyway.
        """
        found: list[irq.Observation] = []
        remote_dispatch: bool | None = None
        for event in events:
            if remote_dispatch is None and (event.status or "").strip() in (
                WORK_REMOTE_DISPATCH_STATUSES
            ):
                # Only a preview status pays for the switch's config read.
                remote_dispatch = work_preview.remote_dispatch()
            if not ledger_wake.is_actionable_event(
                event.kind, event.status, remote_dispatch=bool(remote_dispatch)
            ):
                continue
            # Keyed on the event's content-addressed id, which is why these never
            # reset on a revision: the key already cannot recur, and clearing it
            # when some OTHER item reports would re-fire a wake this one already
            # delivered.
            if not self._spend(conductor_key, item.item_id, event.id):
                continue
            found.append(
                irq.Observation(
                    f"{_REPORT_KEY}:{event.id}",
                    irq.Severity.WAKE,
                    ledger_wake.wake_brief(
                        item_id=item.item_id,
                        status=event.status or "",
                    ),
                    irq.ResetsOn.NEVER,
                )
            )

        undelivered = work_ledger.read_undelivered(conductor_key, item)
        if undelivered is not None:
            # The worker tried to report and the harness never delivered the call, so
            # there is no report event to wake on and the silence would otherwise read
            # as a stall. Keyed on the failed call's own stamp: a second failure later
            # is news, a re-read of the same one is not.
            token = f"{_UNDELIVERED_KEY}:{undelivered['at']}"
            if self._spend(conductor_key, item.item_id, token, replaces=f"{_UNDELIVERED_KEY}:"):
                found.append(
                    irq.Observation(
                        f"{_UNDELIVERED_KEY}:{item.item_id}:{undelivered['at']}",
                        irq.Severity.WAKE,
                        ledger_wake.undelivered_wake_brief(
                            item_id=item.item_id, tool=undelivered["tool"]
                        ),
                        irq.ResetsOn.NEVER,
                    )
                )

        running, closed, looping = work_ledger.item_liveness(
            conductor_key,
            item,
            running=self._worker_running,
            closed=self._worker_closed,
            looping=self._worker_looping,
        )
        if work_ledger.is_stale(
            item, worker_running=running, worker_closed=closed, worker_looping=looping
        ):
            # The whole liveness conjunction is ``is_stale``'s, not this module's:
            # quiet past the window -- or the worker's session gone, which needs no
            # window -- AND the worker not running AND its last word still leaving the
            # move with it. Restating any part of it here would be a second copy of a
            # shipped rule.
            token = f"{_STALL_KEY}:{item.last_report_at or 'none'}"
            if self._spend(conductor_key, item.item_id, token, replaces=f"{_STALL_KEY}:"):
                found.append(
                    irq.Observation(
                        f"{_STALL_KEY}:{item.item_id}:{item.last_report_at or 'none'}",
                        irq.Severity.WAKE,
                        ledger_wake.stall_brief(
                            item_id=item.item_id,
                            status=item.status,
                        ),
                        irq.ResetsOn.NEVER,
                    )
                )
        return found

    def _spend(self, conductor_key: str, item_id: str, token: str, *, replaces: str = "") -> bool:
        """Whether this item may wake now, charging the budget if it does.

        A *token* that already woke the conductor never wakes it again while the item
        is open. The kernel masks its key for ``realert_secs`` only,
        and this probe re-reads the same events and the same silence on every tick, so
        the kernel cannot be the dedupe here.

        The same check is what makes the budget count DISTINCT wakes: a re-read never
        reaches it, so an unchanged fact cannot burn the hour's allowance and swallow
        a genuinely new report on the same item.

        A charge that does not persist refuses the wake. The alternative reads as the
        safer one and is not: an unwritable rate file leaves every later tick reading
        an unspent budget, so waking anyway removes the ceiling entirely rather than
        costing one extra wake. Refusing costs the conductor its event-driven wakes
        while the file cannot be written, and its plain timer still runs the goal.
        """
        if ledger_wake.already_woke(conductor_key, item_id, token, watcher=self._watcher):
            return False
        if not ledger_wake.within_rate_limit(conductor_key, item_id):
            logger.debug(
                "work-ledger probe: item %s is over its wake budget; folding forward",
                item_id,
            )
            return False
        if not ledger_wake.note_wake(
            conductor_key, item_id, token=token, replaces=replaces, watcher=self._watcher
        ):
            logger.warning(
                "work-ledger probe: item %s's wake could not be charged; folding "
                "forward rather than waking uncounted",
                item_id,
            )
            return False
        return True


def has_open_items(conductor_key: str) -> bool:
    """Whether *conductor_key*'s ledger holds at least one non-terminal item.

    What the AutoNudge timer asks before it lets a spent cycle cap or runtime
    budget end a work-ledger watch. Positive evidence only: an unreadable ledger,
    or a torn item file ``list_work_items`` skips, answers False, so doubt leaves
    the loop's own bound in force rather than lifting it -- the opposite of the
    tick's terminal rule above, because there doubt must keep a watch ALIVE and
    here it must not keep one running past its bound. Read-only; blocking file
    reads, so call it off the event loop.
    """
    try:
        return any(not item.is_terminal for item in work_ledger.list_work_items(conductor_key))
    except Exception:  # noqa: BLE001 - a read fault must leave the bound in force
        logger.debug("work-ledger probe: open-items read failed for %s", conductor_key)
        return False


def worker_report_revision(items: list[work_ledger.WorkItem]) -> str:
    """A digest of each item's newest WORKER report: the one rule both readers share.

    The quiet floor (:attr:`WorkLedgerProbe.revision`) and the person-wait hold
    (:func:`person_wait_fingerprint`) both ask "has a worker said anything since the
    conductor last looked?", so both read this. Worker-owned fields only, not the
    event tail: the conductor's own writes are things it already knows, and eight of
    them would push the newest report out of a tail. Every report moves
    ``last_report_at``, so a report that repeats the last one still counts. A ledger
    with no report yet answers :data:`_NO_REPORTS`, never empty.
    """
    newest_report: dict[str, str] = {}
    for item in items:
        if item.last_report_at:
            newest_report[item.item_id] = json.dumps(
                [item.last_report_at, item.status, item.summary], default=str
            )
    return ledger_wake.revision(newest_report) or _NO_REPORTS


def person_wait_fingerprint(
    conductor_key: str, *, worker_closed: Callable[[str], bool] | None = None
) -> str | None:
    """The ledger's fingerprint when every open item waits on a person, else ``None``.

    What the AutoNudge timer asks before a work-ledger watch spends a turn: an open
    item that is ``blocked`` or ``question`` with a ``reason`` of ``approval`` or
    ``needs_human`` cannot move until someone acts, and when every open item is one,
    the patrol has nothing to read that its last turn did not. The fingerprint IS
    :func:`worker_report_revision`, the digest the quiet floor uses, so a worker's
    report -- even one that stays blocked, because ``last_report_at`` moves -- changes
    it and the conductor's own writes do not. The timer holds only while it equals the
    copy taken at the loop's last delivered turn (``ledger_seen_fp``), which the timer
    keeps apart from the floor's ``ledger_delivered_revision`` because the two are
    forgotten at different times: the hold's copy is read at the top of the tick and
    cleared when a cycle never starts, fails, or a person presses Nudge.

    An item whose worker session is gone (*worker_closed*) is not a wait on a person:
    nobody is left to answer its approval, so the patrol must run and the probe's
    stall wake must reach the conductor. A worker's ``work_report`` / ``work_brief``
    that never reached the ledger (:func:`kiro_crew.work_ledger.read_undelivered`)
    moves no report field, so its record is folded into the fingerprint: each new
    failure releases the hold once, for the probe's undelivered wake.

    Positive evidence only, like :func:`has_open_items`: no open item, a torn item
    file, an unreadable header or a read that raises all answer ``None``, so doubt
    leaves the loop firing as it always has. Blocking file reads; call it off-loop.
    """
    try:
        record = work_ledger.read_conductor(conductor_key)
        if record is None:
            return None
        items = work_ledger.list_work_items(conductor_key)
        stored = sum(1 for _ in work_ledger.items_dir(conductor_key).glob("it_*.json"))
        if stored > len(items):
            return None
        open_items = [item for item in items if not item.is_terminal]
        if not open_items:
            return None
        if not all(
            item.status in WORK_REASON_STATUSES and item.reason in WORK_BLOCKED_REASONS
            for item in open_items
        ):
            return None
        if any(item.worker_instance_id for item in open_items):
            # A remote worker's reports reach this board only when the conductor
            # reads it (the hub pulls on read), so its fields here can be older than
            # the peer's. Holding the patrol on them could hold it past the report
            # that moves the item; the patrol runs instead.
            return None
        closed = worker_closed or _always_open
        if any(closed(item.worker_session_key or "") for item in open_items):
            return None
        # A worker whose report the harness never delivered has news the report
        # fields do not carry, so its record joins the fingerprint: a new failure
        # releases the hold for the tick that brings it, and the patrol holds again
        # once that turn has seen it.
        undelivered: dict[str, str] = {}
        for item in open_items:
            lost = work_ledger.read_undelivered(conductor_key, item)
            if lost is not None:
                undelivered[item.item_id] = lost["at"]
        revision = worker_report_revision(items)
        if not undelivered:
            return revision
        return f"{revision}|undelivered:{ledger_wake.revision(undelivered)}"
    except Exception:  # noqa: BLE001 - a read fault must never hold a patrol
        logger.debug("work-ledger probe: person-wait read failed for %s", conductor_key)
        return None


def _conductor_key(raw: object) -> str:
    """The conductor session key named by a watch's config message."""
    text = raw if isinstance(raw, str) else ""
    try:
        config = json.loads(text) if text.strip() else None
    except ValueError as exc:
        raise ValueError(f"work-ledger watch config is not JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("work-ledger watch config must be a JSON object")
    key = str(config.get("conductor") or "").strip()
    if not key:
        raise ValueError("work-ledger watch config needs a 'conductor' session key")
    return key
