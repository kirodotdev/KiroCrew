"""The recovery-replay ledger of a dashboard chat slot.

A recovery replay is a queue entry the runner queues for itself to run a turn
again after a recoverable failure. Each family has its own trigger, but one
hazard: the entry waits in the queue, and a Stop, a session rebind or a newer
user message can land before it runs. A replay must never run work the user has
since stopped, moved or replaced, so it is checked twice: by the queue drain
before it dispatches the entry, and by the turn at its consume seam, because
scheduling the task opens a second window.

The ledger is ``slot.replays``. A family is armed with the identity of the entry
it queued and the Stop counters and session binding it was queued under
(:meth:`RecoveryReplays.arm`). The drain names the families a dispatch carries
(:meth:`RecoveryReplays.claim`), forgets records whose entry left the queue
(:meth:`RecoveryReplays.sweep`), and either seam asks whether a record is revoked
(:meth:`RecoveryReplays.revalidate`). The rule is the same for every family; what
differs per family -- whether one queue entry identifies it, where the consume seam
checks it, the cancellation notice, which one-shot allowance a cancellation
refunds, whether an accepted replay keeps its record until the turn settles its
episode -- is data in :data:`POLICIES`, the one registry of families: its order is
the order the drain re-checks them in. The runner's recovery owner applies the
policy at the two seams.

The ledger holds no reference to its slot and reads nothing itself: a re-check
takes a :class:`LiveSlot`, the slot's Stop state, binding and pending user input
as the caller reads them, so the rule is a function of its arguments.

A sub-agent completion the parent is still owed is never a replay here: a verbatim
requeue of it is queued as the completion it is, with no record, so no Stop,
rebind or newer message cancels it.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple


class ReplayFamily(enum.Enum):
    """The runner's self-queued replays, one per recovery that re-runs a turn."""

    #: The reactive model-access swap re-queues the user's message on the first
    #: advertised model the account can run.
    MODEL_ACCESS = "model_access"
    #: A live backend answering "Session not found" gets one fresh process and one
    #: replay of the turn.
    SESSION_NOT_FOUND = "session_not_found"
    #: The backend rejected an image retained in native history; the conversation
    #: is discarded and the turn replayed without it.
    IMAGE_HISTORY = "image_history"
    #: The content filter declined the turn; it is retried once on the fallback model.
    CONTENT_FILTER = "content_filter"
    #: The runner's own continuation prompts: a promise-only ending, a stall after
    #: a mid-turn compaction, a false tool blocker. Identified by entry shape rather
    #: than by one entry id, so every continuation-shaped entry shares the record.
    CONTINUATION = "continuation"


@dataclass(frozen=True)
class LiveSlot:
    """The slot as a re-check sees it: read once by the caller, at the seam.

    ``session_stop_generation`` is the session manager's Stop count for a key. A
    family's record decides which key it asks about, so it is a reader rather
    than a value; each key is read at most once per view.
    """

    session_key: str
    stop_generation: int
    stopping: bool
    user_input: bool
    session_stop_generation: Callable[[str], int]
    _read: dict[str, int] = field(default_factory=dict, init=False, compare=False, repr=False)

    def stop_count_for(self, key: str) -> int:
        if key not in self._read:
            self._read[key] = self.session_stop_generation(key)
        return self._read[key]


class ReplayRevocation(NamedTuple):
    """Why a queued replay is revoked. All False: it may run."""

    #: The slot is bound to another session than the one the replay was queued for.
    rebound: bool = False
    #: A Stop moved the slot's or the session's counter since the replay was queued.
    stop_moved: bool = False
    #: A Stop is in flight now.
    stopping: bool = False
    #: User input -- a pending steer or a user-authored queue entry -- is waiting.
    superseded: bool = False

    @property
    def stopped(self) -> bool:
        return self.stop_moved or self.stopping

    @property
    def revoked(self) -> bool:
        return self.rebound or self.stopped or self.superseded

    def __repr__(self) -> str:
        return (
            f"ReplayRevocation(rebound={self.rebound}, stopped={self.stopped}, "
            f"superseded={self.superseded})"
        )


@dataclass(frozen=True)
class _Record:
    entry_id: str
    session_key: str
    stop_gen: int
    session_stop_gen: int


#: Where the turn's consume seam re-checks a family: before or after the
#: turn-start refresh of the per-turn allowances, which a lost-session or
#: image-history veto must not reach and a model-access replay must.
ConsumePhase = Literal["before_allowances", "after_allowances"]


@dataclass(frozen=True)
class ReplayPolicy:
    """What differs between families when a replay is cancelled or accepted."""

    #: The record names one queue entry by id, which the drain claims for its
    #: dispatch and the consume seam re-checks. Otherwise the record covers every
    #: entry of the family's shape and only the drain's purge reads it.
    keyed_by_entry: bool = True
    #: Where the consume seam re-checks a claimed replay of the family.
    consume_phase: ConsumePhase | None = None
    #: The notice a cancellation appends: fixed wording, or ``"{label} retry
    #: cancelled -- <reason>"`` when ``label`` is set.
    notice: str = ""
    label: str = ""
    #: The one-shot allowances a cancellation re-arms, as ``(slot attribute,
    #: re-armed value)``, at the drain and at the consume seam.
    drain_refunds: tuple[tuple[str, Any], ...] = ()
    consume_refunds: tuple[tuple[str, Any], ...] = ()
    #: The allowances re-armed when the drain finds the record's entry gone.
    sweep_refunds: tuple[tuple[str, Any], ...] = ()
    #: Where the drain forgets a record whose entry left the queue: once, at its
    #: head (``"head"``, :meth:`RecoveryReplays.sweep`), or in the family's own step.
    swept_at: Literal["head", "step"] = "step"
    #: An accepted replay keeps its record past the consume seam; the turn's
    #: episode settlement forgets it.
    consume_keeps_record: bool = False
    #: Whose Stop count the session-scoped check reads: the session the record was
    #: queued for (``"recorded"``), or the slot's live binding (``"live"``).
    session_count_of: Literal["recorded", "live"] = "recorded"
    #: Log lines for a cancellation, and for a record the drain head swept.
    #: ``log_flags`` formats a cancellation's reasons as ``(stop_moved, superseded,
    #: rebound)``; otherwise the revocation itself.
    drain_log: str = ""
    consume_log: str = ""
    sweep_log: str = ""
    log_flags: bool = False


SESSION_NOT_FOUND_CANCELLED_TEXT = "ℹ️ Session reconnect cancelled — nothing was run."
IMAGE_RECOVERY_CANCELLED_TEXT = "ℹ️ Image-history recovery cancelled — nothing was run."

POLICIES: dict[ReplayFamily, ReplayPolicy] = {
    ReplayFamily.MODEL_ACCESS: ReplayPolicy(
        consume_phase="after_allowances",
        label="Model-fallback",
        # The swap itself stays: the next genuine turn's restore probe unwinds it.
        # Only a drain-side cancellation refunds the one-shot.
        drain_refunds=(("_model_access_fallback_used", False),),
        sweep_refunds=(("_model_access_fallback_used", False),),
        swept_at="head",
        sweep_log=(
            "Cleared model-access recovery record for slot %s: the replay "
            "entry (qid=%s) was swept from the queue before dispatch"
        ),
        drain_log="Dropped model-access recovery replay before dispatch for slot %s (%s)",
        consume_log="Model-access recovery replay aborted at consume for slot %s (%s)",
    ),
    ReplayFamily.SESSION_NOT_FOUND: ReplayPolicy(
        consume_phase="before_allowances",
        notice=SESSION_NOT_FOUND_CANCELLED_TEXT,
        drain_refunds=(("_session_not_found_retry_used", False),),
        consume_refunds=(("_session_not_found_retry_used", False),),
        drain_log=(
            "Dropped lost-session replay before dispatch for slot %s "
            "(stop_since_enqueue=%s superseded=%s rebound=%s)"
        ),
        consume_log=(
            "Lost-session replay aborted at consume for slot %s "
            "(stopped=%s superseded=%s rebound=%s)"
        ),
        log_flags=True,
    ),
    ReplayFamily.IMAGE_HISTORY: ReplayPolicy(
        consume_phase="before_allowances",
        notice=IMAGE_RECOVERY_CANCELLED_TEXT,
        # The conversation discard is not unwound -- a cold start on the next turn
        # is the safe direction -- but its one-shot, shared with the poisoned-
        # conversation canary, is refunded.
        drain_refunds=(("_poisoned_reset_used", False),),
        consume_refunds=(("_poisoned_reset_used", False),),
        drain_log="Dropped unsupported-image recovery before dispatch for slot %s (%s)",
        consume_log="Unsupported-image recovery aborted at consume for slot %s (%s)",
    ),
    ReplayFamily.CONTENT_FILTER: ReplayPolicy(
        consume_phase="after_allowances",
        label="Content-filter",
        # The record dies with a cancelled retry, so an identical later message is
        # a genuine turn. The allowance stays spent: the episode's one retry was
        # used. The model swap is unwound by the next genuine turn's restore probe.
        consume_keeps_record=True,
        drain_log="Purged superseded refusal replay before dispatch for slot %s (%s)",
        consume_log="Refusal replay aborted at consume for slot %s (%s)",
    ),
    ReplayFamily.CONTINUATION: ReplayPolicy(
        keyed_by_entry=False,
        drain_refunds=(("_promise_only_retries", 0), ("_compaction_continue_retries", 0)),
        session_count_of="live",
    ),
}

#: The families whose record names one queue entry, in the drain's re-check order.
ENTRY_FAMILIES = tuple(family for family, policy in POLICIES.items() if policy.keyed_by_entry)


def consumed_in(phase: ConsumePhase) -> tuple[ReplayFamily, ...]:
    """The families the consume seam re-checks in *phase*, in registry order."""
    return tuple(family for family, policy in POLICIES.items() if policy.consume_phase == phase)


def _cancel_reason(revocation: ReplayRevocation) -> str:
    # A rebind reads as a move only when the replay was neither superseded nor
    # stopped; a newer message outranks a Stop.
    if revocation.rebound and not (revocation.superseded or revocation.stopped):
        return "this chat moved to another session."
    if revocation.superseded:
        return "your newer message runs instead."
    return "the turn was stopped."


def cancel_notice(family: ReplayFamily, revocation: ReplayRevocation) -> str:
    """The transcript notice for a cancelled replay of *family*."""
    if family is ReplayFamily.CONTINUATION:
        # Only a real user follow-up takes over; a Stop with nothing queued ran
        # nothing, so the notice never promises a takeover.
        if revocation.superseded:
            return "ℹ️ Auto-continue cancelled — your message takes over."
        if revocation.rebound:
            return (
                "ℹ️ Auto-continue cancelled — this chat moved to another session, nothing was run."
            )
        return "ℹ️ Auto-continue cancelled — the turn was stopped, nothing was run."
    policy = POLICIES[family]
    if policy.label:
        return f"ℹ️ {policy.label} retry cancelled — " + _cancel_reason(revocation)
    return policy.notice


def log_args(family: ReplayFamily, revocation: ReplayRevocation) -> tuple[Any, ...]:
    """The reason arguments a cancellation log line of *family* formats."""
    if POLICIES[family].log_flags:
        return (revocation.stop_moved, revocation.superseded, revocation.rebound)
    return (revocation,)


#: The record a family not keyed by entry starts with: queued under no binding,
#: before any Stop.
_UNKEYED_AT_START = _Record(entry_id="", session_key="", stop_gen=0, session_stop_gen=0)


class RecoveryReplays:
    """The armed replays of one slot, keyed by family.

    An entry family is armed while its record names a queue entry; arming one
    with an empty entry id disarms it. A family not keyed by entry (the
    continuations) always has a record: it starts at zero counters and no
    binding, and is re-armed whenever one is queued or purged.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[ReplayFamily, _Record] = {
            family: _UNKEYED_AT_START
            for family, policy in POLICIES.items()
            if not policy.keyed_by_entry
        }

    def arm(
        self,
        family: ReplayFamily,
        *,
        entry_id: str,
        session_key: str,
        stop_gen: int,
        session_stop_gen: int,
    ) -> None:
        """Record the replay *family* just queued, replacing any earlier record.

        ``stop_gen`` and ``session_stop_gen`` are the slot's and the session's Stop
        counts at enqueue; ``session_key`` is the binding the replay belongs to.
        """
        if POLICIES[family].keyed_by_entry and not entry_id:
            self._records.pop(family, None)
            return
        self._records[family] = _Record(
            entry_id=entry_id,
            session_key=session_key,
            stop_gen=stop_gen,
            session_stop_gen=session_stop_gen,
        )

    def disarm(self, family: ReplayFamily) -> None:
        """Forget *family*'s record: its replay ran, was cancelled, or is gone."""
        if POLICIES[family].keyed_by_entry:
            self._records.pop(family, None)
        else:
            self._records[family] = _UNKEYED_AT_START

    def armed(self, family: ReplayFamily) -> bool:
        """Whether *family* has a queued replay on record."""
        record = self._records.get(family)
        return record is not None and (not POLICIES[family].keyed_by_entry or bool(record.entry_id))

    def entry_id(self, family: ReplayFamily) -> str:
        """The queue entry id *family*'s replay was queued under, or ``""``."""
        record = self._records.get(family)
        return record.entry_id if record is not None else ""

    def claim(self, consumed: Sequence[dict]) -> frozenset[ReplayFamily]:
        """The families whose replay this dispatch IS.

        A replay counts only when its entry drained alone: a merge that folds user
        input into the same dispatch is a correction, not the retry. One entry can
        carry several families -- a content-filter retry re-queued verbatim by
        another recovery keeps the retry identity -- so every match is returned.
        """
        if len(consumed) != 1:
            return frozenset()
        drained = consumed[0].get("id")
        return frozenset(
            family
            for family in ENTRY_FAMILIES
            if (record := self._records.get(family)) is not None
            and record.entry_id
            and record.entry_id == drained
        )

    def sweep(
        self, live_ids: Iterable[object], families: Iterable[ReplayFamily] = ENTRY_FAMILIES
    ) -> list[tuple[ReplayFamily, str]]:
        """Forget the records among *families* whose entry is not in *live_ids*.

        Another removal -- the admission sweep, a merge, a hard Stop's queue clear --
        can take a replay's entry without touching the ledger, and a later entry
        must not inherit its gate. Returns each forgotten family with the entry id
        it named, so the caller can re-arm the family's ``sweep_refunds``.
        """
        live = set(live_ids)
        swept: list[tuple[ReplayFamily, str]] = []
        for family in families:
            entry = self.entry_id(family)
            if entry and entry not in live:
                self.disarm(family)
                swept.append((family, entry))
        return swept

    def revalidate(self, family: ReplayFamily, live: LiveSlot) -> ReplayRevocation:
        """Whether *family*'s replay is revoked by what happened since it was queued.

        Revoked when the slot is bound away from the session the replay was queued
        for, when a Stop moved the slot or session counter past the counts taken at
        enqueue or one is in flight, or when user input is waiting behind it. An
        empty recorded binding means none was taken and is never a rebind. An
        unarmed family reads as not revoked.
        """
        if not self.armed(family):
            return ReplayRevocation()
        record = self._records[family]
        if POLICIES[family].session_count_of == "live":
            count_key = live.session_key
        else:
            count_key = record.session_key or live.session_key
        session_count = live.stop_count_for(count_key)
        return ReplayRevocation(
            rebound=bool(record.session_key) and live.session_key != record.session_key,
            stop_moved=(
                live.stop_generation != record.stop_gen or session_count != record.session_stop_gen
            ),
            stopping=live.stopping,
            superseded=live.user_input,
        )


def replays_of(slot: Any) -> RecoveryReplays:
    """*slot*'s ledger, attaching an empty one to a slot that has none yet.

    Every :class:`~kiro_crew.dashboard.state._ChatSlot` carries one from
    construction; a partially built slot or a test double gets one on first use.
    A slot that cannot hold one raises ``AttributeError``: a ledger it would not
    keep would drop every record armed on it, and with them the Stop and rebind
    checks.
    """
    replays = getattr(slot, "replays", None)
    if not isinstance(replays, RecoveryReplays):
        replays = RecoveryReplays()
        slot.replays = replays
    return replays
