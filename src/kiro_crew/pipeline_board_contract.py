"""The pipeline board panel's contract: one input type, one output type, one provider.

A dashboard needs a data FORMAT, so it gets a type -- not a lookup table saying where
each field could come from. The provider's return type IS the template's input type,
and ``build_pipeline_board`` is the only place the two meet. ``mypy src/kiro_crew/`` is
blocking in CI and ``check_untyped_defs`` is on, so a missing required key is a build
failure rather than a convention.

ONE template, ONE contract type, ONE provider, ONE :data:`CONTRACT_VERSION`. A second
dashboard that needs different data brings its own three. There is deliberately no
general "panel dict" for templates to dig through, because that is the current defect:
:func:`kiro_crew.agent_panel.publish` accepts any object under a size cap, so a
conductor typed owner names into the cell that must say what a person should DO, and
words into the column headed CHECKS. Nothing could refuse it.

So the writable surface is split. :class:`PipelineBoardNumbers` is derived from the
folded work board and a publisher cannot reach it. :class:`PipelineBoardJudgment` holds
the three things no log can produce, each with its own type. The provider merges them,
and the numbers stop being hand-typed.

:data:`UNSAID` is why "nobody can supply this" cannot be forgotten. A key that may be
omitted is omitted silently; a REQUIRED key whose type is ``str | Unsaid`` forces the
provider to write the sentinel out, and mypy rejects both the missing key and a
stray ``None`` from a failed lookup. :func:`panel_payload` is the single exit that
turns the sentinel into the ``null`` the template already renders as "not said" --
and if that step is ever skipped the sentinel shows up as visible text, which is
loud rather than a zero standing in for an unknown.

Unsaid marks a value a writer could supply but did not; a field no writer can ever fill
is removed, not marked. The sentinel exists so a gap is never silently skipped, which
is a different problem from a field that is simply dead: a field with no possible writer
gives every reader a branch that can never be taken, and the parity gate then has to
carry it forever.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Literal, TypedDict, cast

from kiro_crew.work_vocab import (
    WORK_ITEM_STATES,
    WORK_VERDICTS,
    WorkBoardItem,
    WorkBoardView,
)

# --------------------------------------------------------------------------
# the sentinel
# --------------------------------------------------------------------------

Unsaid = Literal["__unsaid__"]
"""The type of "nobody can supply this". A required field, never an absent key."""

UNSAID: Final[Unsaid] = "__unsaid__"
"""Write this, explicitly. It cannot arrive from a lookup that returned nothing."""


# --------------------------------------------------------------------------
# INPUT -- the shape the ``work`` fold renders
# --------------------------------------------------------------------------
#
# :class:`~kiro_crew.work_vocab.WorkBoardView` and its parts live in ``work_vocab``,
# the leaf every reader of a work board already shares, so the fold can narrow its own
# return to them without importing anything about a panel. Re-exported here because
# this module's signature names them and a reader of the contract should not have to
# chase two files to see both ends of the map.

__all__ = [
    "BOARD_CREW_NAME",
    "BOARD_TEMPLATE_ID",
    "CONTRACT_VERSION",
    "EMPTY_JUDGMENT",
    "UNSAID",
    "JudgmentError",
    "PipelineBoardCard",
    "PipelineBoardColumn",
    "PipelineBoardJudgment",
    "PipelineBoardMeta",
    "PipelineBoardNumbers",
    "PipelineBoardPanel",
    "PipelineBoardProgress",
    "PipelineBoardSegment",
    "PipelineBoardStat",
    "Unsaid",
    "WorkBoardItem",
    "WorkBoardView",
    "build_pipeline_board",
    "panel_payload",
    "validate_judgment",
]


# --------------------------------------------------------------------------
# which template this contract is of
# --------------------------------------------------------------------------

BOARD_TEMPLATE_ID: Final[str] = "kirocrew-pipeline-conductor"
"""The one template this contract describes -- and an id ``template_for_crew`` RETURNS.

Asserted reachable through crew selection by ``test_pipeline_board_contract_parity``,
not assumed: an id no crew's name slugifies to names a file nobody renders, so the
conductor would get the generic template while every gate here stayed green over a
file that is never selected.

``default`` is deliberately NOT bound to this contract. Any crew may publish anything
to the generic template, so binding it would rebuild the free-form panel dict this
contract replaces -- in the opposite direction, and for every crew at once.
"""

BOARD_CREW_NAME: Final[str] = "KiroCrew Pipeline Conductor"  # brand-ok: slugified, not prose
"""The crew whose name selects that template, as a DISPLAY name.

Not its slug: ``template_for_crew`` slugifies what it is given, and a multi-word name
is exactly where that step fails, so the assertion has to walk through that step.

The JOINED spelling of the first word is load-bearing, which is why the line carries a
``brand-ok`` marker rather than the two-word product name. Slugification lowercases and
turns each space into a hyphen, so the joined form yields
``kirocrew-pipeline-conductor`` and reaches :data:`BOARD_TEMPLATE_ID`, while splitting
that word yields a slug with one hyphen more and reaches ``default`` instead. This value
is an identifier on its way through a transform, not prose about the product.
"""


# --------------------------------------------------------------------------
# OUTPUT -- the shape ``kirocrew-pipeline-conductor.html`` reads
# --------------------------------------------------------------------------


class PipelineBoardCard(TypedDict):
    """One row under a column."""

    #: The item's pull request if it has one, else its item id.
    id: str
    #: The worker session holding it.
    sub: str | Unsaid
    #: A CI check tally, and NOT derivable: an item carries a verdict (an acceptance
    #: ruling) and a fail count, neither of which is a check count. Only a publisher
    #: that genuinely read a forge can fill it, and only fraction-shaped.
    of: str | Unsaid
    #: What a person must DO. A judgment, so a publisher's -- gated, because an owner
    #: name here is the defect this contract exists to stop.
    you: str | Unsaid


class PipelineBoardColumn(TypedDict):
    """One column. Named from the closed state vocabulary, never invented."""

    name: str
    cards: list[PipelineBoardCard]


class PipelineBoardSegment(TypedDict):
    """One band of the progress track."""

    name: str
    n: int


class PipelineBoardProgress(TypedDict):
    """Counts, never a percentage."""

    total: int
    added_since: int
    segments: list[PipelineBoardSegment]


class PipelineBoardStat(TypedDict):
    """One metric tile. A generic slot: its source is that of whatever is put in it."""

    k: str
    v: str
    note: str | Unsaid


class PipelineBoardMeta(TypedDict):
    """The header bar."""

    name: str
    captured_at: str
    #: Age of the NEWEST work entry, not of the read. A live read's own clock is
    #: always about zero, which would delete the idea of a stale board entirely.
    age_seconds: int | Unsaid
    stale_after_seconds: int
    revision: int


class PipelineBoardPanel(TypedDict):
    """THE contract. Every field ``kirocrew-pipeline-conductor.html`` reads, and nothing else.

    Asserted equal to the template's own reads by
    ``test_pipeline_board_contract_parity``, because mypy cannot see inside HTML.
    """

    contract_version: int
    lede: str | Unsaid
    since: str | Unsaid
    meta: PipelineBoardMeta
    columns: list[PipelineBoardColumn]
    progress: PipelineBoardProgress
    stats: list[PipelineBoardStat]
    #: Entries the fold dropped, carried through from :class:`WorkBoardView`.
    omitted: int


CONTRACT_VERSION: Final[int] = 1
"""Bumped when :class:`PipelineBoardPanel` changes shape. One per contract type."""


# --------------------------------------------------------------------------
# the two writable surfaces
# --------------------------------------------------------------------------


class PipelineBoardJudgment(TypedDict):
    """The publisher's whole surface: the things no log can produce.

    Every number is absent from here on purpose. A conductor cannot reach a count, a
    column name, an age or a revision, so the board's arithmetic cannot disagree with
    the log it claims to summarise.

    ``checks`` has its own field rather than riding in ``notes`` under a key prefix.
    A prefix convention would make ``notes`` a general bag with a naming rule holding
    it together, which is the defect this contract replaces -- and mypy cannot check a
    naming rule.
    """

    #: The sentence.
    lede: str | Unsaid
    #: What a person must do about ONE item, keyed by item id. Gated by
    #: :func:`_gate_action`.
    #:
    #: By item, not by column: the template's field is per card, so a column key
    #: stamps one item's sentence onto every card beside it -- two open items both
    #: reading one item's action, when only one of them is the item it names. That is
    #: the same misattribution as an owner name in this cell, which is what the gate
    #: below exists to stop, so keying it any other way reintroduces by shape what
    #: the gate removes by value. Same key as ``checks``, so the publisher has one
    #: rule for both rather than a rule per field.
    you: dict[str, str]
    #: The gloss on a metric, keyed by metric key.
    notes: dict[str, str]
    #: A CI check tally, keyed by item id. Gated by :func:`_gate_fraction`, so only a
    #: fraction reaches the cell headed CHECKS.
    checks: dict[str, str]


class PipelineBoardNumbers(TypedDict):
    """Everything the provider derives from the fold. A publisher cannot write here."""

    revision: int
    age_seconds: int | Unsaid
    since: str | Unsaid
    columns: list[PipelineBoardColumn]
    progress: PipelineBoardProgress
    omitted: int


EMPTY_JUDGMENT: Final[PipelineBoardJudgment] = {
    "lede": UNSAID,
    "you": {},
    "notes": {},
    "checks": {},
}
"""A publisher that said nothing. Renders a board of facts with no judgments."""


# --------------------------------------------------------------------------
# the publisher's half, checked at RUN time
# --------------------------------------------------------------------------
#
# mypy checks the provider because the provider is Python in this tree. The publisher
# is an agent handing JSON to an MCP tool, so no type checker is anywhere near it --
# which is why ``you: "Raymond"`` reached a rendered board. The contract therefore has
# to be enforced twice, in the two different ways the two ends admit of.


class JudgmentError(Exception):
    """A published payload is not a :class:`PipelineBoardJudgment`.

    Carries the offending KEY, because "your data is wrong" is unactionable to a
    caller holding a dict of four fields. Raised from this module rather than as the
    store's own error type so the contract does not have to import the store it is
    validated by.
    """

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(f"{key}: {detail}" if key else detail)
        self.key = key
        self.detail = detail


def _require_str(key: str, value: Any) -> None:
    if not isinstance(value, str):
        raise JudgmentError(key, f"must be a string, got {type(value).__name__}")


def _require_str_map(key: str, value: Any) -> None:
    """A flat ``dict[str, str]``, or a :class:`JudgmentError` naming the inner key.

    The live defect is exactly this shape being absent: ``you`` arrived as the bare
    string ``"Raymond"``, and a check that only asked "is it a dict" would have let a
    nested object through to be rendered as ``[object Object]``.
    """
    if not isinstance(value, dict):
        raise JudgmentError(key, f"must be an object of strings, got {type(value).__name__}")
    for inner, text in value.items():
        if not isinstance(inner, str):
            raise JudgmentError(key, f"has a non-string key {inner!r}")
        if not isinstance(text, str):
            raise JudgmentError(f"{key}.{inner}", f"must be a string, got {type(text).__name__}")


#: One row per :class:`PipelineBoardJudgment` field. Asserted below to be exactly that
#: type's key set, which is what stops the two from drifting: a field added to the type
#: with no checker here, or a checker for a field the type dropped, fails at import.
_JUDGMENT_CHECKS: Final[dict[str, Any]] = {
    "lede": _require_str,
    "you": _require_str_map,
    "notes": _require_str_map,
    "checks": _require_str_map,
}

assert set(_JUDGMENT_CHECKS) == set(PipelineBoardJudgment.__annotations__), (
    "every PipelineBoardJudgment field needs a runtime check: "
    f"{sorted(set(PipelineBoardJudgment.__annotations__) ^ set(_JUDGMENT_CHECKS))}"
)


def validate_judgment(data: Any) -> PipelineBoardJudgment:
    """*data* as a :class:`PipelineBoardJudgment`, or :class:`JudgmentError`.

    An UNKNOWN key is refused rather than ignored. Ignoring it is how a conductor
    learns nothing from publishing ``fleet`` and ``issues``: the panel would render
    without them and look merely incomplete, when the real answer is that those
    numbers now come from the log and the publisher has no say in them.

    An ABSENT key is filled from :data:`EMPTY_JUDGMENT`, which is not the same
    leniency. A publisher omitting ``checks`` is saying it read no forge, and that is
    a true statement with a named value under this contract -- unlike a PROVIDER
    omitting an output field, where the template is left with a blank cell and nobody
    accountable for it. The asymmetry is the point: an offer may be silent, a
    contractual answer may not.
    """
    if not isinstance(data, dict):
        raise JudgmentError("", f"a panel judgment must be an object, got {type(data).__name__}")
    for key in data:
        if key not in _JUDGMENT_CHECKS:
            raise JudgmentError(
                str(key),
                "is not part of this contract; the publisher writes judgments "
                f"({', '.join(sorted(_JUDGMENT_CHECKS))}) and the provider derives every "
                "number from the work log",
            )
    # A FRESH empty per call, not ``dict(EMPTY_JUDGMENT)``: that copies the mapping
    # but shares its three inner dicts, so one caller adding an entry to ``you`` would
    # put it in the module constant and hand it to every later publisher.
    out: dict[str, Any] = {
        "lede": EMPTY_JUDGMENT["lede"],
        "you": {},
        "notes": {},
        "checks": {},
    }
    for key, check in _JUDGMENT_CHECKS.items():
        if key not in data:
            continue
        check(key, data[key])
        out[key] = data[key]
    return cast("PipelineBoardJudgment", out)


# --------------------------------------------------------------------------
# gates on the judgment side
# --------------------------------------------------------------------------

#: The column names a board may have, in render order. The states and verdicts are
#: closed vocabularies, so a publisher inventing "next" or "ready" is refused rather
#: than rendered -- ``ready`` in a column headed CHECKS is how this started.
BOARD_COLUMN_NAMES: Final[tuple[str, ...]] = WORK_ITEM_STATES
BOARD_VERDICT_NAMES: Final[tuple[str, ...]] = WORK_VERDICTS


def _gate_action(text: str) -> str | Unsaid:
    """An action sentence, or :data:`UNSAID`.

    A bare token is refused because the live board rendered ``Raymond`` and
    ``chat-2176`` on the line that must say what to DO. A name is not an action, and
    the cheapest thing that separates them is whether the value reads as a phrase at
    all: an action has a space in it and a verb's worth of length.
    """
    value = text.strip()
    if len(value) < 8 or " " not in value:
        return UNSAID
    return value


def _gate_fraction(text: str) -> str | Unsaid:
    """``N/M``, or :data:`UNSAID`. The CHECKS cell takes nothing else.

    Each side must round-trip through ``int()`` to its canonical non-negative decimal
    spelling. That admits only ASCII digits with no sign, whitespace, underscore, or
    leading zero; Unicode digit forms either fail to parse or normalize to ASCII. The
    CHECKS cell represents a trusted forge tally, so ambiguous spellings are refused.
    """
    left, sep, right = text.partition("/")
    if not sep:
        return UNSAID
    try:
        left_number = int(left)
        right_number = int(right)
    except ValueError:
        return UNSAID
    if left_number < 0 or right_number < 0:
        return UNSAID
    if left != str(left_number) or right != str(right_number):
        return UNSAID
    return text


# --------------------------------------------------------------------------
# the provider -- the one place the two types meet
# --------------------------------------------------------------------------


def build_pipeline_board(
    view: WorkBoardView,
    judgment: PipelineBoardJudgment,
    *,
    name: str,
    captured_at: str,
    stale_after_seconds: int,
    now_epoch: float,
) -> PipelineBoardPanel:
    """Map one folded work board plus one publisher judgment onto the panel contract.

    The signature is the contract: a ``WorkBoardView`` in, a ``PipelineBoardPanel``
    out, and mypy checks both ends. The host values are keyword arguments rather than
    fields of either input, because they belong to neither -- the fold does not know
    the crew's display name and the publisher must not decide when its own board
    counts as stale.
    """
    numbers = _derive_numbers(view, judgment, now_epoch=now_epoch)
    return {
        "contract_version": CONTRACT_VERSION,
        "lede": judgment["lede"],
        "since": numbers["since"],
        "meta": {
            "name": name,
            "captured_at": captured_at,
            "age_seconds": numbers["age_seconds"],
            "stale_after_seconds": stale_after_seconds,
            "revision": numbers["revision"],
        },
        "columns": numbers["columns"],
        "progress": numbers["progress"],
        "stats": _stats(view, judgment),
        "omitted": numbers["omitted"],
    }


def _derive_numbers(
    view: WorkBoardView,
    judgment: PipelineBoardJudgment,
    *,
    now_epoch: float,
) -> PipelineBoardNumbers:
    """Everything the fold can answer for, in O(1) over the already-folded object."""
    conductor = view["conductor"]
    items = view["items"]
    by_state: dict[str, list[WorkBoardItem]] = {n: [] for n in BOARD_COLUMN_NAMES}
    # The board's own newest entry, not the newest ITEM stamp. An item carries only
    # ``created_at``, ``last_report_at`` and ``closed_at``, none of which a conductor's
    # own round touches -- a decision, a verdict, an acceptance, a bind -- so a board
    # that just moved would keep ageing and eventually read as stale while it is
    # current. The fold stamps this where it accepts an entry, so every kind of entry
    # refreshes it.
    #
    # The item stamps stay as the FALLBACK, for a checkpoint written before the fold
    # carried this key: its items are still stamped, so an older answer beats none.
    item_newest = ""
    added = 0
    for item in items:
        by_state.setdefault(item["state"], []).append(item)
        for stamp in (item["created_at"], item["last_report_at"], item["closed_at"]):
            if stamp and stamp > item_newest:
                item_newest = stamp
        if item["round"] >= conductor["round"]:
            added += 1
    # The fallback is computed in the same pass and chosen only after it, never with a
    # short-circuit inside the loop: that would stop at the FIRST item's stamp instead
    # of the newest one, which is a wrong age rather than a missing one.
    newest = conductor["last_entry_at"] or item_newest

    columns: list[PipelineBoardColumn] = []
    for state in BOARD_COLUMN_NAMES:
        columns.append(
            {
                "name": state,
                "cards": [_card(item, judgment) for item in by_state.get(state, [])],
            }
        )
    segments: list[PipelineBoardSegment] = [
        {"name": state, "n": len(by_state.get(state, []))} for state in BOARD_COLUMN_NAMES
    ]
    return {
        "revision": conductor["round"],
        "age_seconds": _age_seconds(newest, now_epoch),
        "since": conductor["first_entry_at"] or UNSAID,
        "columns": columns,
        "progress": {
            # ITEMS only. ``omitted`` counts ENTRIES the fold dropped -- a straggler
            # from a purged board, an entry naming no item -- and one dropped entry is
            # not one missing item, so adding the two produces a total that belongs to
            # neither. The drops reach the reader as their own count instead, which is
            # the honest place for them: they are a fact about the LOG, not about the
            # board's work.
            "total": len(items),
            "added_since": added,
            "segments": segments,
        },
        "omitted": view["omitted"],
    }


def _card(item: WorkBoardItem, judgment: PipelineBoardJudgment) -> PipelineBoardCard:
    """One row. ``id`` and ``sub`` from the fold; ``of`` and ``you`` gated judgments."""
    pr = item["pr"]
    worker = item["worker_session_key"]
    return {
        "id": f"#{pr}" if isinstance(pr, int) else item["item_id"],
        "sub": worker if worker else UNSAID,
        # Never derived. The fold holds no check tally, so the only honest provider
        # answer with no publisher value is UNSAID.
        "of": _gate_fraction(judgment["checks"].get(item["item_id"], "")),
        "you": _gate_action(judgment["you"].get(item["item_id"], "")),
    }


def _stats(view: WorkBoardView, judgment: PipelineBoardJudgment) -> list[PipelineBoardStat]:
    """The metric tiles: a derived count each, with the publisher's gloss if any."""
    conductor = view["conductor"]
    pairs = (
        ("items", str(len(view["items"]))),
        ("entries", str(conductor["entries"])),
        ("round", str(conductor["round"])),
    )
    out: list[PipelineBoardStat] = []
    for key, value in pairs:
        note = judgment["notes"].get(key, "").strip()
        out.append({"k": key, "v": value, "note": note or UNSAID})
    return out


def _age_seconds(newest_stamp: str, now_epoch: float) -> int | Unsaid:
    """Seconds since the newest work entry, or :data:`UNSAID` when there is none.

    Measured from the LOG, not from the read: a live read's own clock is always about
    zero, and "how old is this information" is a question about the log. A board with
    no entry yet has no age, which is not the same fact as an age of zero.
    """
    if not newest_stamp:
        return UNSAID
    try:
        when = datetime.fromisoformat(newest_stamp)
    except ValueError:
        return UNSAID
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(now_epoch - when.timestamp()))


# --------------------------------------------------------------------------
# the single exit to the data island
# --------------------------------------------------------------------------


def panel_payload(panel: PipelineBoardPanel) -> dict[str, Any]:
    """*panel* as the JSON the data island carries: every :data:`UNSAID` becomes null.

    ONE exit, because the template's ``classify`` already distinguishes three states
    and ``null`` is the one it renders as "not said". Leaving the sentinel in would
    print ``__unsaid__`` on the page -- wrong, but visibly wrong, which is why this
    conversion failing is not a silent zero.
    """
    return cast("dict[str, Any]", _strip_unsaid(panel))


def _strip_unsaid(value: Any) -> Any:
    if value == UNSAID:
        return None
    if isinstance(value, dict):
        return {k: _strip_unsaid(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_unsaid(v) for v in value]
    return value
