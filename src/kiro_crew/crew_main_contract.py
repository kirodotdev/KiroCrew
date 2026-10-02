"""The root session's automatic card: its numbers, as one type and one provider.

The chain is ``crew log -> fold -> build_crew_main -> the card's data``, and a number never
passes through a model. The card itself is the automatic session card the dashboard
already shows (``dashboard/card_lifecycle.py``); its layout stays the model's. What this
module takes away from the model is every number: :func:`build_crew_main` turns four fold
renders into :class:`CrewMainDerived`, and the model's own data is exactly
:class:`CrewMainJudgment` -- three sentences.

WHOSE CARD THIS IS. A ROOT session's, and no other slot's. The gate is
:func:`~kiro_crew.dashboard.card_lifecycle.is_root_session`: no ``_created_by`` and no
session-tree parent, the same root notion the sidebar tree draws with. A worker gets no
card; its numbers are read by opening the worker.

TWO WRITERS, KEPT APART BY TYPE.

* :class:`CrewMainDerived` -- every field from a fold render. No model can reach them.
* :class:`CrewMainJudgment` -- the three sentences no fold can produce.

:func:`merge_crew_main` is the only place the two meet, and it copies every field by NAME
rather than updating a dict, so a model field can never land on a derived one. The publish
seam in ``card_lifecycle`` refuses a card whose model-written part carries a digit at all.

EVERY VALUE IS A STRING. The data island is bound by ``dashboardDocument.ts`` with
``element.textContent`` and ``normalize_card`` accepts only a flat map of strings, so
absence is carried in WORDS:

* :data:`NOT_RECORDED` -- the fold was read and does not carry this key.
* :data:`UNREADABLE` -- the fold could not be read at all.

Those two are never collapsed: a zero standing in for an unknown is the one failure a
reader cannot recover from.

NO PERCENTAGES, and every count carries its denominator in words where one exists. A bare
``3`` invites the reader to supply the total, and the total they supply is wrong.

WHAT IS DELIBERATELY NOT A FIELD. The ask_question inventory is a host read no fold
carries, so there is no honest derived count of it; the two question-shaped numbers that
ARE folds appear instead (``approvals_open`` from ``approvals``, ``items_question`` from
``work``), and the question cards stay native controls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, TypedDict, cast

from kiro_crew.work_vocab import WorkBoardView

__all__ = [
    "CARD_FIELDS",
    "DERIVED_FIELDS",
    "JUDGMENT_FIELDS",
    "JUDGMENT_TEXT_LIMIT",
    "NOT_RECORDED",
    "UNREADABLE",
    "CrewMainData",
    "CrewMainDerived",
    "CrewMainJudgment",
    "CrewMainReads",
    "FoldUnreadable",
    "build_crew_main",
    "card_data_payload",
    "merge_crew_main",
]


# --------------------------------------------------------------------------
# the words absence is spelt with
# --------------------------------------------------------------------------

NOT_RECORDED: Final[str] = "not recorded"
"""The fold was read and carries no such key. Shown to the reader, in words."""

UNREADABLE: Final[str] = "could not be read"
"""The fold could not be read. NOT the same fact as nothing being recorded."""


FoldUnreadable = Literal["__unreadable__"]
"""The type a caller passes INSTEAD of a fold render when the read failed."""

FOLD_UNREADABLE: Final[FoldUnreadable] = "__unreadable__"
"""Pass this for a fold whose read raised. It cannot arrive from a lookup."""


# --------------------------------------------------------------------------
# INPUT -- the fold renders, each readable or not
# --------------------------------------------------------------------------


class CrewMainReads(TypedDict):
    """The four fold renders the card is built from, or :data:`FOLD_UNREADABLE`.

    Required keys, all four. A caller that could not read one says so with the sentinel
    rather than omitting the key, because an omitted key and a failed read are the same
    absence to a ``dict`` and different facts to a reader.

    The reads are passed IN rather than fetched here. This module then has no store to
    reach, so it cannot decide to read one more thing on a whim, and the provider is a
    pure function a test can drive with a hand-built board.
    """

    #: ``status`` fold, SESSION-keyed: lifecycle, turn, turn counts, last time.
    status: Mapping[str, Any] | FoldUnreadable
    #: ``usage`` fold, SESSION-keyed: credits with the per-source split, and tokens.
    usage: Mapping[str, Any] | FoldUnreadable
    #: ``approvals`` fold, SESSION-keyed: tool approvals raised, decided, still open.
    approvals: Mapping[str, Any] | FoldUnreadable
    #: ``work`` fold, SLOT-keyed and eager: the board's items and their states.
    work: WorkBoardView | FoldUnreadable


# --------------------------------------------------------------------------
# OUTPUT -- the root card's data
# --------------------------------------------------------------------------


class CrewMainDerived(TypedDict):
    """Everything derived from folds. Neither the host nor a model can write here."""

    #: The session's lifecycle as the crew log stated it -- open or closed -- or, when it
    #: did not, why not, in words: retention took the opening entry, the key is absent,
    #: or the fold could not be read. Never empty.
    state: str
    #: What it is doing right now: a turn running, or how the last one ended.
    phase: str
    #: Turns the log saw complete, and how many were refused.
    turns: str
    #: Entries in the crew log, which is what every count here was folded from.
    entries: str
    #: The agent the log recorded for this session.
    agent: str
    #: The model the log recorded for it.
    model: str
    #: Work items still open, over the board's total.
    items_open: str
    #: Open items whose worker last reported progress, over the open ones. Labelled
    #: "Reporting progress" rather than "Running", because it counts work ITEMS and not
    #: sessions that are awake: a session count would have to include workers, which this
    #: panel does not show, so the old label named a question this number does not answer.
    items_progress: str
    #: Open items whose worker last reported blocked, over the open ones.
    items_blocked: str
    #: Items the conductor accepted, over the board's total.
    items_done: str
    #: Open items whose worker is waiting on the conductor's own decision.
    items_question: str
    #: Entries the work fold dropped. A fact about the LOG, not about the board, which
    #: is why it is its own count and is never added into the totals above.
    board_omitted: str
    #: Credits the log billed to this session, all sources together.
    credits: str
    #: The sub-agent share of that total, which is otherwise invisible.
    credits_subagents: str
    #: Tokens across every dimension the log measured.
    tokens: str
    #: Tool approvals raised and not yet decided, over those raised.
    approvals_open: str
    #: Approvals already decided, and how they went.
    approvals_decided: str


class CrewMainJudgment(TypedDict):
    """The model's whole surface: three sentences, and not one number among them."""

    #: One sentence: what this crew is doing.
    lede: str
    #: One sentence: what, if anything, the reader must do. Empty when nothing.
    you: str
    #: One sentence of caveat, or empty.
    notes: str


class CrewMainData(CrewMainDerived, CrewMainJudgment):
    """THE contract: every field a root card's data carries, and nothing else.

    The two halves are inherited rather than restated, so a field added to either reaches
    this type without a second edit. Its key set is also the only set of names a model's
    layout may bind (:data:`CARD_FIELDS`).
    """


DERIVED_FIELDS: Final[frozenset[str]] = frozenset(CrewMainDerived.__annotations__)
"""Fold-derived. Neither the host nor a model may write one."""

JUDGMENT_FIELDS: Final[frozenset[str]] = frozenset(CrewMainJudgment.__annotations__)
"""The fields a model may write, and the only ones."""

JUDGMENT_TEXT_LIMIT: Final[int] = 160
"""Per judgment field. One sentence, and a bound the data cap can always afford."""

if DERIVED_FIELDS & JUDGMENT_FIELDS:  # pragma: no cover - import-time consistency
    raise RuntimeError(
        f"a crew main field has exactly one writer: {sorted(DERIVED_FIELDS & JUDGMENT_FIELDS)}"
    )

_ALL_FIELDS: Final[frozenset[str]] = DERIVED_FIELDS | JUDGMENT_FIELDS
"""Every field the root card's data carries, and the only names its layout may bind."""

if set(CrewMainData.__annotations__) != _ALL_FIELDS:  # pragma: no cover - import-time
    raise RuntimeError(
        "CrewMainData must be exactly its two halves: "
        f"{sorted(set(CrewMainData.__annotations__) ^ _ALL_FIELDS)}"
    )

CARD_FIELDS: Final[frozenset[str]] = _ALL_FIELDS
"""Public name for :data:`_ALL_FIELDS`: the binding names a root card's layout may use."""


def merge_crew_main(derived: CrewMainDerived, judgment: CrewMainJudgment) -> CrewMainData:
    """The one place the two writers meet.

    Built by NAMING every field, not by ``{**derived, **judgment}``.
    A dict update writes whatever keys the right-hand side happens to hold, so a judgment
    carrying ``credits`` would overwrite the folded value and nothing would notice;
    naming the fields means only a source edit could do that, and mypy would reject the
    derived key in a :class:`CrewMainJudgment` literal.
    """
    return {
        "state": derived["state"],
        "phase": derived["phase"],
        "turns": derived["turns"],
        "entries": derived["entries"],
        "agent": derived["agent"],
        "model": derived["model"],
        "items_open": derived["items_open"],
        "items_progress": derived["items_progress"],
        "items_blocked": derived["items_blocked"],
        "items_done": derived["items_done"],
        "items_question": derived["items_question"],
        "board_omitted": derived["board_omitted"],
        "credits": derived["credits"],
        "credits_subagents": derived["credits_subagents"],
        "tokens": derived["tokens"],
        "approvals_open": derived["approvals_open"],
        "approvals_decided": derived["approvals_decided"],
        "lede": judgment["lede"],
        "you": judgment["you"],
        "notes": judgment["notes"],
    }


def card_data_payload(data: CrewMainData) -> dict[str, str]:
    """*data* as the flat string map ``normalize_card`` accepts. The single exit."""
    return {key: str(value) for key, value in data.items()}


# --------------------------------------------------------------------------
# the provider -- the one place a fold render becomes a card field
# --------------------------------------------------------------------------


class _StatusFields(TypedDict):
    """What the ``status`` fold answers for. A TypedDict so mypy checks the branches.

    The four section types below exist for one reason: a helper with an early return
    for an unreadable fold has as many branches as it has outcomes, and a plain
    ``dict[str, str]`` return lets one of those branches forget a key. The forgotten key
    then reaches the card as an absent binding, which shows an EMPTY cell -- the
    one outcome this contract is built to make impossible, because an empty cell and a
    recorded zero are indistinguishable. Typed, a branch that forgets a key fails the
    blocking mypy run instead.
    """

    state: str
    phase: str
    turns: str
    entries: str
    agent: str
    model: str


class _WorkFields(TypedDict):
    """What the ``work`` fold answers for."""

    items_open: str
    items_progress: str
    items_blocked: str
    items_done: str
    items_question: str
    board_omitted: str


class _UsageFields(TypedDict):
    """What the ``usage`` fold answers for."""

    credits: str
    credits_subagents: str
    tokens: str


class _ApprovalFields(TypedDict):
    """What the ``approvals`` fold answers for."""

    approvals_open: str
    approvals_decided: str


def build_crew_main(reads: CrewMainReads) -> CrewMainDerived:
    """Map four fold renders onto the card's derived half.

    The signature is the contract: fold renders in, a checked field set out, and mypy
    checks both ends. Every branch answers in words -- no key here is ever left to a
    caller to fill in, and no absence is ever answered with a zero.

    Every field is NAMED rather than gathered by ``**`` expansion. Expansion
    reads shorter and checks nothing: mypy cannot verify a ``dict`` spread against a
    TypedDict's required keys, so a section helper that dropped a field would produce a
    value missing that key with no error anywhere. Named, the assignment is checked.
    """
    status = _status_fields(reads["status"])
    work = _work_fields(reads["work"])
    usage = _usage_fields(reads["usage"], _turns_completed(reads["status"]))
    approvals = _approval_fields(reads["approvals"])
    return {
        "state": status["state"],
        "phase": status["phase"],
        "turns": status["turns"],
        "entries": status["entries"],
        "agent": status["agent"],
        "model": status["model"],
        "items_open": work["items_open"],
        "items_progress": work["items_progress"],
        "items_blocked": work["items_blocked"],
        "items_done": work["items_done"],
        "items_question": work["items_question"],
        "board_omitted": work["board_omitted"],
        "credits": usage["credits"],
        "credits_subagents": usage["credits_subagents"],
        "tokens": usage["tokens"],
        "approvals_open": approvals["approvals_open"],
        "approvals_decided": approvals["approvals_decided"],
    }


def _status_fields(status: Mapping[str, Any] | FoldUnreadable) -> _StatusFields:
    """The ``status`` fold's six fields, or six honest refusals."""
    if status == FOLD_UNREADABLE:
        return {
            "state": UNREADABLE,
            "phase": UNREADABLE,
            "turns": UNREADABLE,
            "entries": UNREADABLE,
            "agent": UNREADABLE,
            "model": UNREADABLE,
        }
    fold = cast("Mapping[str, Any]", status)
    lifecycle = fold.get("lifecycle")
    if lifecycle == "open":
        state = "session open"
    elif lifecycle == "closed":
        state = "session closed"
    elif lifecycle == "unknown":
        # The fold's own third state: retention removed the entry that opened the
        # session, so the log cannot say. Not the same as the key being absent.
        state = "the log no longer says"
    else:
        state = NOT_RECORDED
    completed = _count(fold.get("turns_completed"))
    refused = _count(fold.get("turns_refused"))
    return {
        "state": state,
        "phase": _phase(fold),
        "turns": (
            NOT_RECORDED
            if completed is None
            # "refused" alone named nothing a reader could place. These count
            # ``turn/refused`` entries, so what refused is the backend -- and saying
            # "approval" here would point at the approvals tile, a different thing on
            # this same card.
            else _noun(completed, "turn", "turns")
            + " finished"
            + ("" if not refused else f", {refused} refused by the backend")
        ),
        "entries": _plain(fold.get("entries"), "entry in the log", "entries in the log"),
        "agent": _text(fold.get("agent")),
        "model": _text(fold.get("model")),
    }


_STOP_WORDS: Final[dict[str, str]] = {
    "end_turn": "the last turn finished",
    "max_tokens": "the last turn hit its length limit",
    "max_turn_requests": "the last turn hit its step limit",
    "refusal": "the last turn was refused",
    "cancelled": "the last turn was stopped",
    "failed": "the last turn failed",
    "error": "the last turn failed",
    "timeout": "the last turn timed out",
    "interrupted": "the last turn was cut off",
}
"""The ACP stop reasons, in words. ``_phase`` shows these, never the enum."""


def _phase(fold: Mapping[str, Any]) -> str:
    """What the crew is doing, from the fold's open turn or its last stop.

    ``turn_open`` is read rather than ``turn``: the fold REPORTS an open turn and never
    closes one, so the flag is the fold's own answer and the dict beside it is the
    detail. A session with a stop reason reads as that reason, which is the nearest
    thing the log has to a phase.
    """
    if fold.get("turn_open") is True:
        return "a turn is running now"
    reason = fold.get("last_stop_reason")
    if isinstance(reason, str) and reason.strip():
        # The fold keeps the provider's enum; a reader is shown words. An enum this
        # table does not know is still a real stop, so it is named as one, not echoed.
        return _STOP_WORDS.get(reason.strip(), "the last turn stopped")
    if "turn_open" not in fold and "last_stop_reason" not in fold:
        return NOT_RECORDED
    return "no turn running"


def _work_fields(work: WorkBoardView | FoldUnreadable) -> _WorkFields:
    """The board's six counts, each with its denominator in words.

    ``state`` and ``status`` are two different vocabularies and both are used here,
    because they answer two different questions. ``state`` is the CONDUCTOR's
    disposition (open / accepted / rejected / abandoned); ``status`` is the worker's own
    last report (progress / done / blocked / question). A worker's ``done`` is a claim
    its conductor has not ruled on, so ``items_done`` counts ACCEPTED items -- the
    ruling -- and says the word, rather than promoting a claim to a result.
    """

    def same(text: str, omitted: str) -> _WorkFields:
        """All five item counts reading *text*, with ``board_omitted`` its own answer.

        ``board_omitted`` is never folded into *text*: it counts log entries the fold
        DROPPED, which stays answerable when the item list does not, so a board whose
        items cannot be read can still say how many entries went missing.
        """
        return {
            "items_open": text,
            "items_progress": text,
            "items_blocked": text,
            "items_done": text,
            "items_question": text,
            "board_omitted": omitted,
        }

    if work == FOLD_UNREADABLE:
        return same(UNREADABLE, UNREADABLE)
    board = cast("WorkBoardView", work)
    items = board.get("items")
    omitted = _dropped_entries(board.get("omitted"))
    if not isinstance(items, list):
        return same(NOT_RECORDED, omitted)
    total = len(items)
    if not total:
        # A real answer, not an absence: this crew folded a board and it has no items.
        # Saying "not recorded" here would hide a fact the fold does carry.
        return same("no work items on this board", omitted)
    open_items = [item for item in items if item.get("state") == "open"]
    accepted = sum(1 for item in items if item.get("state") == "accepted")
    open_total = len(open_items)

    def of_open(count: int, tail: str, empty: str) -> str:
        return f"{count} of {open_total} open items {tail}" if open_total else empty

    return {
        "items_open": f"{open_total} of {total} items not yet done",
        "items_progress": of_open(
            sum(1 for item in open_items if item.get("status") == "progress"),
            "reporting progress",
            "no open items in progress",
        ),
        "items_blocked": of_open(
            sum(1 for item in open_items if item.get("status") == "blocked"),
            "blocked",
            "no open items to block",
        ),
        "items_done": f"{accepted} of {total} items accepted",
        "items_question": of_open(
            sum(1 for item in open_items if item.get("status") == "question"),
            "waiting on you",
            "no items waiting on you",
        ),
        "board_omitted": omitted,
    }


def _dropped_entries(value: object) -> str:
    """How many entries the fold dropped, and what that does to every count beside it.

    The qualifier rides HERE rather than in the model's ``notes``, because a reader deciding
    from a tally needs to know the tally is a floor whether or not the sentences are switched
    on. A warning that disappears with the opt-in while the number it qualifies stays is the
    same defect as a number written by a model: the page keeps its confidence and loses its
    caveat.

    A count of zero is stated too. "No entries dropped" is a real assurance, and leaving the
    field silent would make a complete board and an unreadable one look alike.
    """
    count = _count(value)
    if count is None:
        return NOT_RECORDED
    if not count:
        return "no entries dropped, so these counts cover every log entry"
    return _noun(count, "entry", "entries") + " dropped, so these counts are a floor"


def _usage_fields(
    usage: Mapping[str, Any] | FoldUnreadable, turns_completed: int | None
) -> _UsageFields:
    """Credits, the sub-agent share of them, and tokens.

    The sub-agent share gets its own field because it is otherwise invisible: it is
    folded into the same total as the crew's own turns, so a reader looking at one number
    cannot tell a crew that spent it all itself from one that fanned out. ``reported``
    beside each bucket is what says how much of the bucket the total covers, so a bucket
    nothing reported reads as unmetered rather than as free.
    """
    if usage == FOLD_UNREADABLE:
        return {"credits": UNREADABLE, "credits_subagents": UNREADABLE, "tokens": UNREADABLE}
    fold = cast("Mapping[str, Any]", usage)
    charge = fold.get("credits")
    by_source = fold.get("credits_by_source")
    subagent = by_source.get("subagent") if isinstance(by_source, Mapping) else None
    tokens = fold.get("tokens")
    total_tokens = tokens.get("total") if isinstance(tokens, Mapping) else None
    return {
        "credits": _crew_credits(charge),
        "credits_subagents": _subagent_credits(subagent),
        "tokens": _tokens(total_tokens, turns_completed),
    }


def _turns_completed(status: Mapping[str, Any] | FoldUnreadable) -> int | None:
    """The status fold's completed-turn count, or ``None`` when it cannot be read."""
    if status == FOLD_UNREADABLE:
        return None
    return _count(cast("Mapping[str, Any]", status).get("turns_completed"))


def _tokens(total: object, turns_completed: int | None) -> str:
    """The token total, or that no turn reported one.

    A completed turn cannot cost zero tokens, so a total of zero after one is an
    absence: the backend sent no counts and the log recorded zeros. Beside a real bill
    it would read as a session that billed without counting. With no completed turn a
    zero is a true zero and is stated as one.
    """
    if _count(total) == 0 and turns_completed:
        return "no tokens reported"
    return _plain(total, "token measured", "tokens measured")


def _crew_credits(charge: object) -> str:
    """The crew's whole charge, or why there is no number for it."""
    if not isinstance(charge, (int, float)) or isinstance(charge, bool):
        return NOT_RECORDED
    if _credits(charge) is None:
        return UNREADABLE
    return f"{_credits(charge)} credits billed to this crew"


def _subagent_credits(bucket: object) -> str:
    """The sub-agent bucket, or why there is no number for it."""
    if not isinstance(bucket, Mapping):
        return NOT_RECORDED
    charge = bucket.get("credits")
    reported = bucket.get("reported")
    if not isinstance(charge, (int, float)) or isinstance(charge, bool):
        return NOT_RECORDED
    if _credits(charge) is None:
        return UNREADABLE
    if not isinstance(reported, int) or isinstance(reported, bool) or reported <= 0:
        # Zero charges reported is not a charge of zero: an unmetered provider writes no
        # credits key at all, and folding that in as 0.0 would state a measurement
        # nobody made.
        return "no sub-agent charge reported"
    return f"{_credits(charge)} of that from {reported} sub-agent charges"


def _approval_fields(approvals: Mapping[str, Any] | FoldUnreadable) -> _ApprovalFields:
    """Tool approvals still open, and how the decided ones went.

    TOOL approvals, and the field says so on the page. The panel's other
    question-shaped number is the ask_question inventory, which is a host read with no
    fold behind it and therefore not a field in this contract at all.
    """
    if approvals == FOLD_UNREADABLE:
        return {"approvals_open": UNREADABLE, "approvals_decided": UNREADABLE}
    fold = cast("Mapping[str, Any]", approvals)
    pending = _count(fold.get("pending"))
    requested = _count(fold.get("requested"))
    decided = _count(fold.get("decided"))
    if pending is None:
        open_text = NOT_RECORDED
    elif requested is None:
        open_text = f"{pending} tool approvals still waiting"
    else:
        open_text = f"{pending} of {requested} tool approvals still waiting"
    return {
        "approvals_open": open_text,
        "approvals_decided": (
            NOT_RECORDED if decided is None else f"{decided} answered{_decisions(fold)}"
        ),
    }


def _decisions(fold: Mapping[str, Any]) -> str:
    """The decided count's own breakdown, in words, or nothing.

    Sorted by DECISION NAME rather than by count, so two reads of the same board put
    the same words in the same order: ordering by count would reshuffle the sentence
    every time one of them moved, which reads as a change that did not happen.
    """
    by_decision = fold.get("by_decision")
    if not isinstance(by_decision, Mapping):
        return ""
    rows = [
        f"{count} {name}"
        for name, count in sorted(by_decision.items())
        if isinstance(name, str) and name and _count(count)
    ]
    return f" ({', '.join(rows)})" if rows else ""


# --------------------------------------------------------------------------
# the small conversions, each with one job
# --------------------------------------------------------------------------


def _count(value: object) -> int | None:
    """*value* as a non-negative count, or ``None`` when it is not one.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so ``True`` would
    otherwise fold in as the count ``1``.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _plain(value: object, one: str, many: str) -> str:
    """``"<n> <noun>"``, or :data:`NOT_RECORDED`. No bare number ever reaches a field."""
    count = _count(value)
    return NOT_RECORDED if count is None else _noun(count, one, many)


def _noun(count: int, one: str, many: str) -> str:
    """*count* with the noun in its English number: ``1 turn``, ``2 turns``, ``0 turns``."""
    return f"{count} {one if count == 1 else many}"


def _text(value: object) -> str:
    """A recorded string, or :data:`NOT_RECORDED`. Bounded, because it is displayed."""
    if not isinstance(value, str) or not value.strip():
        return NOT_RECORDED
    return value.strip()[:80]


def _credits(charge: float) -> str | None:
    """A credit charge as a reader budgets in it, and never in scientific notation.

    ``repr`` of a small float is exponential (``1e-05``), which on a panel reads as a
    different order of magnitude than it is.

    TWO places, because this is a display field and a reader budgeting from it does not
    think in millionths. The fold keeps its six, so nothing downstream loses precision by
    this. Trailing zeros stay: "3.50" reads as money where "3.5" reads as a measurement.
    """
    if charge != charge or charge in (float("inf"), float("-inf")):
        # A non-finite total is a broken fold, not a charge. The caller states the whole
        # field as unreadable rather than print "nan" where a reader budgets from it.
        return None
    if 0 < charge < 0.005:
        # Two places would round this to "0.00", and a charge shown as zero reads as no
        # charge at all -- the one thing a spend field must never say while spending. So
        # the SMALLEST charge is described instead of rounded, still to two places.
        return "less than 0.01"
    return f"{charge:.2f}"
