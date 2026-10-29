"""The conductor work ledger's vocabulary, limits, refusal codes and record types.

The states, verdicts, statuses and event kinds a record may carry; the caps every stored
field and every goal board is held to; the ``CODE_*`` refusal codes the routes map to HTTP
statuses; :class:`WorkLedgerError`; the three record dataclasses (:class:`ConductorRecord`,
:class:`WorkItem`, :class:`WorkEvent`) with the coercions that read a stored record back;
and the store's clock. :mod:`kiro_crew.work_ledger` binds every name defined here, so each
one reads, imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kiro_crew.work_vocab import (
    WORK_BLOCKED_REASONS,
    WORK_ITEM_STATES,
    WORK_MAX_DEPTH_DEFAULT,
    WORK_STORED_ITEM_LIMIT,
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
)

# Named for the module callers import, so every record the store's owners log carries
# the logger name ``kiro_crew.work_ledger``.
logger = logging.getLogger("kiro_crew.work_ledger")

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

#: A work item's disposition, written by the conductor. A DIFFERENT question from
#: ``verdict``: an item may hold ``verdict: fail`` and stay ``open`` while the
#: worker retries, which is the state ``ledger_entry.py`` encodes today as "fails
#: incremented but still running".
ITEM_STATES: frozenset[str] = frozenset(WORK_ITEM_STATES)

#: States from which no further write is accepted.
TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"accepted", "rejected", "abandoned"})

#: ``accept_eval.py``'s own five values, reused rather than paralleled, so a verdict
#: crosses from that script into this store with no translation.
VERDICTS: frozenset[str] = frozenset(WORK_VERDICTS)

#: What a worker may say about itself. ``blocked`` and ``question`` are separate
#: because they differ in WHO must act: an external dependency versus the conductor.
WORKER_STATUSES: frozenset[str] = frozenset(WORK_WORKER_STATUSES)

#: Why a ``blocked`` / ``question`` report is stuck; see :data:`WORK_BLOCKED_REASONS`.
BLOCKED_REASONS: frozenset[str] = frozenset(WORK_BLOCKED_REASONS)

#: The statuses from which a report gap still means "the WORKER went quiet", which is
#: the only thing :func:`is_stale` exists to surface. Only ``progress`` leaves the next
#: move with the worker. ``done``, ``blocked`` and ``question`` each woke the conductor
#: through the report itself and hand the next move to the conductor or to a person,
#: so a worker that stays silent after one of them is waiting, not stalled. Flagging
#: it would point the flag at the reader instead of the worker.
STALE_ELIGIBLE_STATUSES: frozenset[str] = frozenset({"progress"})

#: The statuses that wait on the conductor or a person. A conductor ``decide`` newer
#: than such a report hands the move BACK to the worker, and from then on
#: :func:`is_stale` measures the worker's silence from the decision.
WAITING_STATUSES: frozenset[str] = frozenset({"blocked", "question"})

#: Field values that mean "not filled in yet". A conductor legitimately opens an item
#: whose bar is not knowable at dispatch time -- the worker is what learns the pull
#: request number -- and writes ``"TBD"`` in the field meanwhile, so the placeholder is
#: part of the shape rather than a typo. See :func:`is_acceptance_concrete`.
ACCEPTANCE_PLACEHOLDERS: frozenset[str] = frozenset({"tbd", ""})

#: Per ``kind``, the fields ``accept_eval.py`` actually READS. This is the whole
#: vocabulary :func:`is_acceptance_concrete` judges a bar by: a placeholder in a field
#: that script consumes is what makes a bar unevaluable, and a placeholder anywhere
#: else is in a field it never looks at, so it is none of this predicate's business.
#: ``cmd`` reads nothing because that script always REFUSES it, and ``refused`` is a
#: message the conductor must receive -- re-express the condition, never route around
#: it -- so dropping such an item from the batch would delete its only delivery.
#: A test derives this map's keys from the script's own dispatch and fails on drift.
ACCEPTANCE_READ_FIELDS: dict[str, tuple[str, ...]] = {
    "pr_checks": ("pr", "repo"),
    "pr_merged": ("pr", "repo"),
    "file": ("path", "exists"),
    "human_approval": (),
    "cmd": (),
}

#: ``accept_eval.py``'s own ``pr_merged`` ``repo`` pattern, mirrored so a bar it can
#: only answer ``error`` to is not called concrete. Applied with ``fullmatch``, as
#: there. A test pins the two equal.
ACCEPTANCE_GH_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")

#: The ``kind`` values that script dispatches on at all. Anything else is an error-only
#: spec (its closing ``return ("error", f"unknown accept kind ...")``), so it is not
#: concrete. Derived from the map above rather than spelled twice.
ACCEPTANCE_KINDS: frozenset[str] = frozenset(ACCEPTANCE_READ_FIELDS)

#: Every ITEM write appends exactly one event, so there is no way to move an item
#: field without a line explaining it (the conductor's own ``goal``/``round``
#: header is the one eventless write, because it belongs to no item). ``report``
#: is the only kind a worker can produce; ``steer`` is the only kind a person's
#: steer from a Crew Mode side panel produces, and it moves no field.
EVENT_KINDS: frozenset[str] = frozenset(
    {"create", "bind", "report", "decision", "verdict", "close", "steer"}
)

#: The conductor's disjoint operations. One action per call, because the field sets
#: do not overlap and a single flat schema would accept nonsense combinations.
CONDUCTOR_ACTIONS: frozenset[str] = frozenset(
    {"create", "bind", "decide", "verdict", "close", "goal"}
)

# --------------------------------------------------------------------------- #
# Caps. Each one refuses; none truncates.
# --------------------------------------------------------------------------- #

#: How many OPEN items one conductor may hold at once -- the bound on live fan-out,
#: which is the only thing a per-conductor item cap is for. Items in a
#: :data:`TERMINAL_ITEM_STATES` state do not count: a queue conductor that mints one
#: item per ticket and closes each as it lands would otherwise exhaust the cap on its
#: own history within a day, with nothing live behind the refusal. Enforced by
#: :func:`_create_item`. There is no bound on how many items a board creates over its
#: life: the crew log's ``work`` fold forgets a closed item once
#: ``work_vocab.WORK_CLOSED_WINDOW`` newer closed items sit behind it, so the fold is
#: bounded by what is open, not by history. This cache keeps every record; nothing
#: here removes one.
MAX_ITEMS_PER_CONDUCTOR = 32
#: How many OPEN items one GOAL may hold at once when the user set no bound of
#: their own -- the goal's own fan-out bound, tighter than
#: :data:`MAX_ITEMS_PER_CONDUCTOR`, which bounds the store. A worker per open item
#: is a worker running, so this is the bound on parallel work, the one thing the
#: user asked to be capped in code. Stored per board as
#: :attr:`ConductorRecord.concurrency_cap`, set with
#: ``work_ledger_record action=goal concurrency_cap=N`` and enforced by
#: :func:`_create_item` on a board that has recorded a goal -- a queue board that
#: never writes one (the pipeline and security conductors) keeps only the open
#: open bound. Closed items do not count: a create refused here is refused for
#: what is running, never for what has run.
DEFAULT_GOAL_CONCURRENCY_CAP = 6
#: The highest ``concurrency_cap`` a goal write accepts: a cap past the store's
#: open bound could never be reached, so it would only hide that bound behind a
#: different refusal.
MAX_GOAL_CONCURRENCY_CAP = MAX_ITEMS_PER_CONDUCTOR
#: How many items one GOAL is expected to create, every round and re-plan together,
#: when the user set no budget of their own. An ADVISORY line, not a bound: a
#: create past it is admitted, and :attr:`ConductorRecord.cap_exceeded` turns true
#: so the conductor's round report can say so and the user can decide whether the
#: goal is spending more than they meant. Nothing refuses on it: a refusal here
#: would stall a goal whose items land and close quickly on its own finished
#: history while nothing was running, and the bound on spend is the concurrency
#: cap above. Stored per board as :attr:`ConductorRecord.item_cap`, set with
#: ``work_ledger_record action=goal item_cap=N`` and measured against
#: :attr:`ConductorRecord.goal_items_used`.
DEFAULT_GOAL_ITEM_CAP = 20
#: The highest ``item_cap`` a goal write accepts.
MAX_GOAL_ITEM_CAP = WORK_STORED_ITEM_LIMIT
MAX_EVENTS_PER_ITEM = 200
#: How many levels may CONDUCT: the root at depth 0 and the conductors below it, up
#: to but not including depth :data:`MAX_DEPTH`. Depth ``MAX_DEPTH`` itself is the
#: first level that may only work -- it may neither open a ledger nor create an item
#: -- so a conductor dispatched by the last conducting level is refused at its first
#: ledger call (:func:`child_depth` denies the open), before any ledger exists for it.
#: ``MAX_DEPTH`` is the DEFAULT of the cap, two levels: root at depth 0 and one
#: conductor at depth 1. Two rather than three because each level multiplies
#: sessions -- three levels of three items is twenty-seven -- and because a summary
#: of summaries of summaries is not evidence any more; that number is a guess
#: informed by session multiplication, not a measurement (RFC Q5).
#:
#: The cap in force is :func:`work_ledger_rules.max_depth`: this default, or the
#: ``work_ledger.max_depth`` config key, kept inside
#: ``[WORK_MAX_DEPTH_MIN, WORK_MAX_DEPTH_MAX]`` (the bounds live in ``work_vocab``). Read ONLY through :func:`may_conduct`,
#: which every depth guard asks, so the level a dispatch admits, the level a ledger
#: may open at and the level that may create an item are one level and cannot drift
#: apart.
MAX_DEPTH = WORK_MAX_DEPTH_DEFAULT

MAX_GOAL_CHARS = 2000
MAX_TITLE_CHARS = 200
MAX_DECISION_CHARS = 2000
MAX_SUMMARY_CHARS = 500
MAX_EVENT_TEXT_CHARS = 500

MAX_ARTIFACT_KEYS = 16
MAX_ARTIFACT_KEY_CHARS = 64
MAX_ARTIFACT_VALUE_CHARS = 512

MIN_PR = 1
MAX_PR = 1_000_000_000

#: A record over this size reads as absent. The writers cannot approach it — every
#: field is capped far below — so crossing it means the file was torn, hand-edited,
#: or written by something that is not this module.
MAX_RECORD_BYTES = 1_000_000

#: How long an item may go unreported before :func:`is_stale` will consider it, once
#: its worker is also confirmed not running. A default, not a policy: the caller
#: passes its own window.
DEFAULT_STALE_WINDOW_SECS = 900.0

#: How long one item's acceptance may read ``pending`` on end before
#: :func:`is_pending_overdue` flags it. Per item, and wall-clock rather than patrol
#: cycles, because the conductor picks its own interval. A day is far past any CI run
#: that is only slow.
PENDING_BUDGET_SECS = 24 * 60 * 60.0

# --------------------------------------------------------------------------- #
# Error codes. Named for the HTTP-facing vocabulary Phase 2 maps them onto, so the
# mapping is a lookup rather than a re-classification.
# --------------------------------------------------------------------------- #

CODE_NO_LEDGER = "no_ledger"
CODE_UNKNOWN_ITEM = "unknown_item"
CODE_ALREADY_BOUND = "already_bound"
#: A caller-chosen item id the board already holds (``create_bound_item``).
CODE_DUPLICATE_ITEM = "duplicate_item"
CODE_ITEM_CLOSED = "item_closed"
CODE_ITEM_CAP_EXCEEDED = "item_cap_exceeded"
CODE_GOAL_CONCURRENCY_CAP_REACHED = "goal_concurrency_cap_reached"
CODE_CREW_LOG_INCOMPLETE = "crew_log_incomplete"
CODE_CACHE_DIRTY = "cache_dirty"
CODE_DEPTH_EXCEEDED = "depth_exceeded"
CODE_FIELD_TOO_LONG = "field_too_long"
CODE_INVALID_ACTION = "invalid_action"
CODE_INVALID_STATUS = "invalid_status"
CODE_INVALID_VALUE = "invalid_value"
CODE_LEDGER_NOT_FINISHED = "ledger_not_finished"


class WorkLedgerError(Exception):
    """A store invariant was violated, or a cap refused a write.

    Carries ``code`` — one of the ``CODE_*`` constants — and, for a bound that
    refused, the ``field`` whose cap it was. Phase 2 maps ``code`` onto a status and
    quotes ``field`` back, so the caller learns WHICH bound it crossed rather than
    that something was too long.
    """

    def __init__(self, message: str, *, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass
class ConductorRecord:
    """One conductor session's ledger header. The conductor is the sole writer.

    There is deliberately NO item roster field. The item list is derived by listing
    the items directory (:func:`list_work_items`), which removes a writer and with
    it a class of clobber — the same choice Issue Radar's ``list_work_items`` makes.
    """

    slot_key: str = ""
    goal: str = ""
    round: int = 0
    depth: int = 0
    parent_item: str | None = None
    created_at: str = ""
    schema: int = SCHEMA_VERSION
    #: An opaque id minted when the record is created. A slot reused after its board
    #: was purged mints a new one, which is how the crew-log fold tells the two
    #: boards apart without comparing timestamps. Empty on records from before it.
    generation: str = ""
    #: Counts the header's goal writes. A goal entry carries it and the fold keeps
    #: the highest it saw, so a rebuild can tell a header the record holds whole
    #: from one the cache wrote after the record's last goal entry. Zero on
    #: records from before it.
    goal_version: int = 0
    #: Stamped once a goal entry has landed in the crew log (the header's
    #: counterpart of an item's ``recorded_at``): from then on the record is
    #: expected to hold every goal write, and a rebuild checks that it does.
    recorded_at: str = ""
    #: How many items this board has created over its life, open and closed
    #: together; the current goal's spend (:attr:`goal_items_used`) is measured off
    #: it. Monotonic under the store's own writes: :func:`_create_item` bumps it under
    #: the conductor lock in the same hold that writes the item, and nothing here
    #: lowers it -- a record removed from ``items/`` keeps the create it counted,
    #: as the crew log keeps that create's entry. :func:`rebuild_from_projection`
    #: sets it to the count the log holds. Zero on records from before it; the
    #: first create on such a board seeds it from the records on the board.
    created_total: int = 0
    #: How many items the current goal is expected to create --
    #: :data:`DEFAULT_GOAL_ITEM_CAP` unless a ``goal`` write set it. Measured
    #: against :attr:`goal_items_used` by :attr:`cap_exceeded`; advisory only.
    item_cap: int = DEFAULT_GOAL_ITEM_CAP
    #: How many items the current goal may hold OPEN at once --
    #: :data:`DEFAULT_GOAL_CONCURRENCY_CAP` unless a ``goal`` write set it. The
    #: bound :func:`_create_item` refuses on, once the board has recorded a goal.
    concurrency_cap: int = DEFAULT_GOAL_CONCURRENCY_CAP
    #: :attr:`created_total` when the current goal started, so the goal's own
    #: creates are the difference. Only a goal write with ``new_goal=True`` moves
    #: it: a reworded goal text, a round bump or a cap raise keeps the count.
    #: :data:`LEGACY_GOAL_BASE` on a header from before this field: the
    #: current goal then counts from the upgrade, and the next header write
    #: settles it to the board's create total (:func:`_settle_goal_base`).
    goal_items_base: int = 0

    @property
    def goal_items_used(self) -> int:
        """How many items the current goal has created, re-plans included."""
        if self.goal_items_base == LEGACY_GOAL_BASE:
            return 0
        return max(0, self.created_total - self.goal_items_base)

    @property
    def cap_exceeded(self) -> bool:
        """Whether the current goal has created more items than :attr:`item_cap`.

        True from the first create past the cap, not at the cap: a goal at twenty
        of twenty has spent what it was allowed. Advisory -- the read reports it beside
        ``items used: N of M`` and the conductor tells the user; nothing blocks on
        it. False on a board that never recorded a goal, which has no goal to
        measure: a queue board's count is its whole history.
        """
        return self.goal_version > 0 and self.goal_items_used > self.item_cap

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "slot_key": self.slot_key,
            "goal": self.goal,
            "round": self.round,
            "depth": self.depth,
            "parent_item": self.parent_item,
            "created_at": self.created_at,
            "generation": self.generation,
            "goal_version": self.goal_version,
            "recorded_at": self.recorded_at,
            "created_total": self.created_total,
            "item_cap": self.item_cap,
            "concurrency_cap": self.concurrency_cap,
            "goal_items_base": self.goal_items_base,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ConductorRecord:
        """Coerce stored JSON into a record. NEVER raises.

        A wrong-typed field resets to its default rather than failing the read: this
        file is written by one party and read by a probe, a page and the conductor
        itself, and a single bad field must not take all three down.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            slot_key=_as_str(raw.get("slot_key")),
            goal=_as_str(raw.get("goal")),
            round=_as_int(raw.get("round"), 0),
            depth=_as_int(raw.get("depth"), 0),
            parent_item=_as_opt_str(raw.get("parent_item")),
            created_at=_as_str(raw.get("created_at")),
            schema=_as_int(raw.get("schema"), SCHEMA_VERSION),
            generation=_as_str(raw.get("generation")),
            goal_version=_as_int(raw.get("goal_version"), 0),
            recorded_at=_as_str(raw.get("recorded_at")),
            created_total=max(0, _as_int(raw.get("created_total"), 0)),
            item_cap=_stored_item_cap(raw.get("item_cap")),
            concurrency_cap=_stored_concurrency_cap(raw.get("concurrency_cap")),
            goal_items_base=(
                max(0, _as_int(raw.get("goal_items_base"), 0))
                if "goal_items_base" in raw
                else LEGACY_GOAL_BASE
            ),
        )


#: The ``goal_items_base`` a header from before the field reads with. Its goal was
#: set before the store counted per goal, so its earlier creates are not charged
#: to it: it counts from the upgrade, never from the board's first create.
LEGACY_GOAL_BASE = -1


def _settle_goal_base(record: ConductorRecord) -> None:
    """Turn a legacy count start into the board's create total, in place.

    Called under the conductor lock after the create counter's one-time backfill,
    so the goal starts counting at the upgrade and the next write stores the key.
    """
    if record.goal_items_base == LEGACY_GOAL_BASE:
        record.goal_items_base = record.created_total


def _stored_item_cap(value: Any) -> int:
    """A stored ``item_cap``, or the default when it is absent or out of range."""
    number = _finite_int(value)
    if number is None or not 1 <= number <= MAX_GOAL_ITEM_CAP:
        return DEFAULT_GOAL_ITEM_CAP
    return number


def _stored_concurrency_cap(value: Any) -> int:
    """A stored ``concurrency_cap``, or the default when absent or out of range.

    A header from before the field reads as the default, so an upgraded board's
    fan-out is bounded the same way a new one's is.
    """
    number = _finite_int(value)
    if number is None or not 1 <= number <= MAX_GOAL_CONCURRENCY_CAP:
        return DEFAULT_GOAL_CONCURRENCY_CAP
    return number


@dataclass
class WorkItem:
    """One dispatched work item. Two writers, one per-item lock, disjoint fields.

    Conductor-owned: ``title``, ``acceptance``, ``state``, ``verdict``, ``decision``,
    ``worker_session_key``, ``round``, ``fails``.
    Worker-owned: ``status``, ``reason``, ``summary``, ``artifacts``, ``pr``,
    ``last_report_at``.
    Server-owned: ``item_id``, ``created_at``, ``closed_at``, ``decided_at``,
    ``pending_since``.

    ``orphaned`` and ``stale`` are NOT fields. They are derived at read time by
    :func:`is_orphaned` and :func:`is_stale`, because a stamped flag would need
    something running at close time to stamp it, a missed stamp would stay wrong
    forever, and a derived flag self-heals when a session is reopened.
    """

    item_id: str = ""
    title: str = ""
    acceptance: dict[str, Any] = field(default_factory=dict)
    state: str = "open"
    verdict: str | None = None
    decision: str = ""
    worker_session_key: str | None = None
    #: The remote crew the worker runs on (rfc-conductor-remote-dispatch §3), set by
    #: a remote bind beside ``worker_session_key`` (then the PEER's slot key), else
    #: ``None``. Serialized only when set, so a board with no remote item is stored
    #: and read exactly as before the field existed.
    worker_instance_id: str | None = None
    round: int = 0
    fails: int = 0
    status: str | None = None
    #: Why a ``blocked`` / ``question`` report is stuck (:data:`BLOCKED_REASONS`), or
    #: ``None``. Every report replaces it, so one without a reason clears it.
    reason: str | None = None
    summary: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    pr: int | None = None
    last_report_at: str | None = None
    created_at: str = ""
    closed_at: str | None = None
    #: When the conductor last wrote ``decide`` on this item, or ``None``. A ``decide``
    #: newer than the last report hands a ``blocked`` / ``question`` item back to the
    #: worker; see :func:`is_stale`.
    decided_at: str | None = None
    #: When the item's current run of ``pending`` verdicts began, or ``None``. The
    #: first ``pending`` verdict stamps it; any other verdict, or a new bar from
    #: ``accept``, clears it. See :func:`is_pending_overdue`.
    pending_since: str | None = None
    schema: int = SCHEMA_VERSION
    #: When the crew log first held this item whole (its recorded create, or the
    #: baseline its first recorded mutation carried). Empty until then: the write
    #: routes carry the whole item on the next entry, so a lost file rebuilds.
    recorded_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ITEM_STATES

    def to_dict(self) -> dict[str, Any]:
        out = self._base_dict()
        if self.worker_instance_id:
            out["worker_instance_id"] = self.worker_instance_id
        return out

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "item_id": self.item_id,
            "title": self.title,
            "acceptance": self.acceptance,
            "state": self.state,
            "verdict": self.verdict,
            "decision": self.decision,
            "worker_session_key": self.worker_session_key,
            "round": self.round,
            "fails": self.fails,
            "status": self.status,
            "reason": self.reason,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "pr": self.pr,
            "last_report_at": self.last_report_at,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "decided_at": self.decided_at,
            "pending_since": self.pending_since,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WorkItem:
        """Coerce stored JSON into an item. NEVER raises. See
        :meth:`ConductorRecord.from_dict` for why a bad field resets rather than
        failing the read."""
        if not isinstance(raw, dict):
            return cls()
        state = _as_str(raw.get("state"))
        status = _as_opt_str(raw.get("status"))
        reason = _as_opt_str(raw.get("reason"))
        verdict = _as_opt_str(raw.get("verdict"))
        artifacts_raw = raw.get("artifacts")
        artifacts: dict[str, str] = {}
        if isinstance(artifacts_raw, dict):
            artifacts = {str(k): v for k, v in artifacts_raw.items() if isinstance(v, str)}
        return cls(
            item_id=_as_str(raw.get("item_id")),
            title=_as_str(raw.get("title")),
            acceptance=raw["acceptance"] if isinstance(raw.get("acceptance"), dict) else {},
            state=state if state in ITEM_STATES else "open",
            verdict=verdict if verdict in VERDICTS else None,
            decision=_as_str(raw.get("decision")),
            worker_session_key=_as_opt_str(raw.get("worker_session_key")),
            worker_instance_id=_as_opt_str(raw.get("worker_instance_id")) or None,
            round=_as_int(raw.get("round"), 0),
            fails=_as_int(raw.get("fails"), 0),
            status=status if status in WORKER_STATUSES else None,
            reason=reason if reason in BLOCKED_REASONS else None,
            summary=_as_str(raw.get("summary")),
            artifacts=artifacts,
            pr=_finite_int(raw.get("pr")),
            last_report_at=_as_opt_str(raw.get("last_report_at")),
            created_at=_as_str(raw.get("created_at")),
            closed_at=_as_opt_str(raw.get("closed_at")),
            decided_at=_as_opt_str(raw.get("decided_at")),
            pending_since=_as_opt_str(raw.get("pending_since")),
            recorded_at=_as_str(raw.get("recorded_at")),
            schema=_as_int(raw.get("schema"), SCHEMA_VERSION),
        )


@dataclass
class WorkEvent:
    """One line in an item's event log.

    ``id`` is content-addressed, so a line written twice — a retry, a restored
    backup, a rollback that replayed — collapses on read instead of double-counting.
    """

    id: str = ""
    ts: str = ""
    item_id: str = ""
    kind: str = ""
    status: str | None = None
    text: str = ""

    @property
    def is_progress_report(self) -> bool:
        """Whether this is the one event kind that coalesces. See
        :func:`_coalesce_progress`."""
        return self.kind == "report" and self.status == "progress"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "item_id": self.item_id,
            "kind": self.kind,
            "status": self.status,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WorkEvent | None:
        """Parse one stored line, or ``None`` when it is not one.

        ``None`` rather than a defaulted record: an event log is append-only and a
        torn tail must be SKIPPED, not folded in as an event with empty fields that
        a reader would then have to distinguish from a real one.
        """
        if not isinstance(raw, dict):
            return None
        kind = _as_str(raw.get("kind"))
        if kind not in EVENT_KINDS:
            return None
        status = _as_opt_str(raw.get("status"))
        return cls(
            id=_as_str(raw.get("id")),
            ts=_as_str(raw.get("ts")),
            item_id=_as_str(raw.get("item_id")),
            kind=kind,
            status=status if status in WORKER_STATUSES else None,
            text=_as_str(raw.get("text")),
        )


# --------------------------------------------------------------------------- #
# Coercion helpers. None of these raise.
# --------------------------------------------------------------------------- #


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any, default: int) -> int:
    parsed = _finite_int(value)
    return default if parsed is None else parsed


def _finite_int(value: Any) -> int | None:
    """*value* as an int, or ``None`` when it is not one.

    ``bool`` is rejected before ``int`` because ``True`` is an ``int`` in Python, so
    a cap written as ``value <= MAX`` is silently defeated by a boolean. A FRACTIONAL
    float is rejected too rather than truncated: ``int()`` would store a number the
    caller never asked for while reporting success.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if not value.is_integer():
            return None
    return int(value)


def _now_iso() -> str:
    """Local time with offset, seconds precision — the same stamp
    :mod:`kiro_crew.session_ledger` writes, so two ledgers read side by side sort
    against each other."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Named by the path callers import it from, so a traceback or an error chain renders
# ``kiro_crew.work_ledger.WorkLedgerError``.
WorkLedgerError.__module__ = "kiro_crew.work_ledger"
