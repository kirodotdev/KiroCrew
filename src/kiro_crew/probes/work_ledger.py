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

One fact this probe cannot read for itself and takes as a value instead:

* ``worker_running`` -- whether a worker's slot has a turn in flight. That lives
  in the dashboard's in-process slot table, which a probe running on a worker
  thread has no handle on, so the driver injects a resolver.

It defaults to the safe direction rather than to nothing: an absent resolver
reads as "not running", which can only make the gate LOUDER (a stall wake the
conductor did not need costs one turn), never quieter. The opposite default would
turn a missing handle into a watch that never reports a stalled worker, which is
the failure this gate exists to remove.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from kiro_crew import irq, ledger_wake, work_ledger

logger = logging.getLogger(__name__)

#: This probe's subject kind. Defined HERE rather than in ``probes/__init__``, which
#: imports this module: the constant has to be readable without reaching back into
#: the package that is still mid-import. ``probes/__init__`` re-exports it.
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

_ACCEPTED_KEY = ACCEPTED_KEY
_CLOSED_KEY = CLOSED_KEY


def _always_idle(_session_key: str) -> bool:
    """Default liveness resolver: nothing is running.

    Not ``True``: an unknown liveness must not be able to SUPPRESS a stall wake.
    See the module docstring for the full argument.
    """
    return False


class WorkLedgerProbe(irq.Probe):
    """Watch one conductor's work ledger.

    Args:
        worker_running: ``session_key -> has a turn in flight``. Defaults to
            :func:`_always_idle`.
    """

    def __init__(
        self,
        *,
        worker_running: Callable[[str], bool] | None = None,
    ) -> None:
        self._worker_running = worker_running or _always_idle
        self._conductor = ""

    # -- Probe contract ---------------------------------------------------

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
                        _ACCEPTED_KEY if finished_well else _CLOSED_KEY,
                        irq.Severity.TERMINAL,
                        detail,
                        irq.ResetsOn.NEVER,
                    )
                ],
            )

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
        """Everything about one open item that needs the conductor."""
        found: list[irq.Observation] = []
        for event in events:
            if not ledger_wake.is_actionable_event(event.kind, event.status):
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

        if work_ledger.is_stale(
            item, worker_running=self._worker_running(item.worker_session_key or "")
        ):
            # The whole liveness conjunction is ``is_stale``'s, not this module's:
            # quiet past the window AND the worker not running AND its last word
            # still leaving the move with it. Restating any part of it here would be
            # a second copy of a shipped rule.
            token = f"{_STALL_KEY}:{item.last_report_at or 'none'}"
            if self._spend(conductor_key, item.item_id, token):
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

    def _spend(self, conductor_key: str, item_id: str, token: str) -> bool:
        """Whether this item may wake now, charging the budget if it does.

        The budget counts DISTINCT wakes, keyed on *token*: a condition the probe
        re-reports on every tick until the kernel's mask expires must not burn the
        hour's allowance, or a genuinely new report on the same item would be
        swallowed by a rate limit that its own predecessor had already spent.

        A charge that does not persist refuses the wake. The alternative reads as the
        safer one and is not: an unwritable rate file leaves every later tick reading
        an unspent budget, so waking anyway removes the ceiling entirely rather than
        costing one extra wake. Refusing costs the conductor its event-driven wakes
        while the file cannot be written, and its plain timer still runs the goal.
        """
        if not ledger_wake.within_rate_limit(conductor_key, item_id, token=token):
            logger.debug(
                "work-ledger probe: item %s is over its wake budget; folding forward",
                item_id,
            )
            return False
        if not ledger_wake.note_wake(conductor_key, item_id, token=token):
            logger.warning(
                "work-ledger probe: item %s's wake could not be charged; folding "
                "forward rather than waking uncounted",
                item_id,
            )
            return False
        return True


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
