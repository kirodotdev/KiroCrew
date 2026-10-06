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
from pathlib import Path
from typing import Any, Final, Literal, TypedDict, cast

# The host's byte cap, IMPORTED rather than restated. A local copy would need a reason to
# exist, and the obvious one -- keeping the dashboard package out of this module's import
# graph -- buys nothing: both consumers of this module (``agent_panel`` and
# ``dashboard/handlers/agent_panel``) are reached only from inside that package. A copy
# could only drift from the number the host actually enforces, and a producer bounding a
# card against a stale limit builds cards the host drops whole.
from kiro_crew.dashboard.dynamic_cards import MAX_DATA_BYTES as _HOST_MAX_DATA_BYTES
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
    "card_template_path",
    "panel_card_data",
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
        # DERIVED FROM THE ROUND, which is why the card does not print it. Printed beside the
        # round tile it is a second name for one number, and a reader cannot tell what
        # distinguishes them because nothing does. The suite asserts the relationship instead,
        # which is the stronger check: a provider that stopped deriving it reddens, where a
        # printed copy would simply have shown a second number.
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


#: The metric tiles a board has, in render order. A CLOSED vocabulary, like the column
#: names, and for the same reason: the card binds a tile's value by a field name built
#: from its key and prints the key as literal text in the page, so a key the provider
#: invents at run time would bind a tile to nothing while every gate stayed green. Read
#: by :func:`_stats` and by :func:`panel_card_data`, so the two cannot drift.
BOARD_STAT_KEYS: Final[tuple[str, ...]] = ("items", "entries", "round")


def _stats(view: WorkBoardView, judgment: PipelineBoardJudgment) -> list[PipelineBoardStat]:
    """The metric tiles: a derived count each, with the publisher's gloss if any."""
    conductor = view["conductor"]
    values = {
        "items": str(len(view["items"])),
        "entries": str(conductor["entries"]),
        "round": str(conductor["round"]),
    }
    assert set(values) == set(BOARD_STAT_KEYS), (
        "every tile in BOARD_STAT_KEYS needs a derived value here: "
        f"{sorted(set(values) ^ set(BOARD_STAT_KEYS))}"
    )
    out: list[PipelineBoardStat] = []
    for key in BOARD_STAT_KEYS:
        note = judgment["notes"].get(key, "").strip()
        out.append({"k": key, "v": values[key], "note": note or UNSAID})
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


# --------------------------------------------------------------------------
# the other exit: the dynamic dashboard CARD
# --------------------------------------------------------------------------
#
# A card is ``{html, data}`` (``dashboard/dynamic_cards.py``): inert layout plus a FLAT
# map of text the host binds by ``data-dashboard-field``. So this exit differs from
# :func:`panel_payload` in three ways that are all forced by the host rather than chosen.
#
# It is FLAT, because ``data`` is. It is TEXT, because the host binds a field by setting
# ``textContent`` -- there is no script in a card to compose a sentence out of parts, so
# every sentence is composed here. And it is BOUNDED: 24 fields and 4096 data bytes, a
# card over either cap being dropped whole rather than degraded, which is why an
# unbounded list cannot become a field per element.

CARD_TEMPLATE_ID: Final[str] = "pipeline_board"
"""The card page this flattener fills: ``dashboard_card_pages/pipeline_board.html``.

Not a crew webview template id and deliberately not spelled like one. The drawer's
per-crew template is selected by slugifying a crew NAME, so its ids carry hyphens; a
card page is named by its own slug and reached by path lookup, with no crew in the path
at all.

It lives in ``dashboard_card_pages`` rather than ``dashboard_templates`` on purpose. The
latter is the home of the ``TemplateSpec`` REGISTRY: every ``*.html`` there is required to
carry a registry row with a flat contract and a ``(data, judgment, captured_at)`` provider,
and ``test_dashboard_templates.py`` fails any page in that directory that does not. This card
is NOT that framework -- its board comes from ``build_pipeline_board`` (a ``PipelineBoardPanel``
return, a different signature) and is flattened to the page's fields by ``panel_card_data``, not
by a registered flat contract. Putting it under the registry package would ship it unregistered
and red that gate, or force the whole provider onto the registry's calling convention for no gain.
A dedicated directory keeps it out of a framework it does not use, and distinct from the per-crew
drawer templates in ``agent_panel_templates`` as well.
"""


def card_template_path() -> Path:
    """The card page's file. PACKAGE-RELATIVE, so a wheel and a checkout answer alike.

    Which is also why the page needs its own packaging entry: this path exists in a
    checkout whether or not the build copied the file, so a page missing from the wheel
    would render nothing while every gate here stayed green. ``setup.cfg`` and
    ``MANIFEST.in`` both carry ``dashboard_card_pages/*.html`` -- the sdist is built from the
    manifest and the wheel from the sdist, so an entry in one alone ships a half fix.
    """
    return Path(__file__).resolve().parent / "dashboard_card_pages" / f"{CARD_TEMPLATE_ID}.html"


# There is deliberately NO separate "version the card reads" constant.
#
# One was here, assigned from :data:`CONTRACT_VERSION`, and it could therefore never differ
# from the thing it claimed to guard -- so the pin asserting the two equal was vacuous and the
# mismatch it was supposed to make visible was unreachable through it. The card has no script,
# so the comparison the drawer template did in JavaScript happens in :func:`_contract_note`
# against ``CONTRACT_VERSION`` directly.
#
# The DISCLOSURE stays, because a mismatch is still reachable from the other side: the value
# compared is the one carried on the PANEL, and a record built by an older provider and stored
# on disk reaches the flattener untouched whenever its board is served as published.

#: There is deliberately no constant for the FIELD cap. The byte cap is imported from the
#: host (see the ``_HOST_MAX_DATA_BYTES`` import at the top of this module), but
#: ``normalize_card`` spells 24 inline and exports nothing, so a name here would be a second copy of a number with no
#: importable source -- and the field count is already asserted the way that matters, by handing
#: the real card to the real ``normalize_card``.

#: Rows one column may print before the rest are counted instead of listed. A first
#: attempt only -- :func:`panel_card_data` lowers it until the whole card fits the byte
#: cap, and says in the row text how many rows it did not print.
_ROWS_PER_COLUMN: Final[int] = 12

#: Bytes each PUBLISHER-WRITTEN field may take. Every other field on the card is derived
#: -- a count, a stamp, a state name -- and is short by construction; these three are free
#: text an agent hands to ``panel_publish``, which caps the whole payload at 64 KB. That is
#: sixteen times this card's entire data budget, so one long sentence in any of them makes
#: the card oversized at EVERY row limit: the row ladder below can only shorten rows, so it
#: retreats to one row per column, still does not fit, and ``normalize_card`` refuses the
#: card whole. A refused card is not a smaller card -- it is no card, reported to the reader
#: as though the feature were switched off. So each of these is clipped first, and the clip
#: says so in the text, exactly as the row retreat states what it did not print.
_LEDE_BYTES: Final[int] = 400
_NOTE_BYTES: Final[int] = 120
_ACTION_BYTES: Final[int] = 200
#: Each CELL of a row line. ``of`` is publisher-written too -- it arrives from
#: ``judgment["checks"]`` and :func:`_gate_fraction` admits any ``N/M`` of ASCII digits, so
#: ``"<2100 digits>/<2100 digits>"`` is a legal publish far under the payload cap and lands
#: ~4200 bytes in one cell that no rung of the ladder can shorten. ``id`` and ``sub`` come from
#: the fold rather than a publisher, but an item id is not length-bounded either, so all three
#: are clipped: the rule is that NO single value can make the card unbuildable, not that the
#: publisher-written ones cannot.
_CELL_BYTES: Final[int] = 120

#: The header NAME. ``meta.name`` is a crew display name -- ``MEMBER_NAME_MAX_CHARS`` admits up to
#: ``MAX_SHORT_STRING`` (500) characters, ~2000 UTF-8 bytes of non-ASCII -- and it lands in
#: ``fixed``, which the row-retreat ladder never shortens (the ladder drops only ``columns``
#: entries). So an unclipped name alone could push ``panel_card_data`` past ``MAX_DATA_BYTES`` and
#: make ``normalize_card`` refuse the whole card, dropping the board to "unavailable" for a crew
#: whose only sin is a long name -- the exact "no single value can make the card unbuildable"
#: contract the cells above keep. Clipped at the lede's budget: a name is a headline, not a cell.
_NAME_BYTES: Final[int] = _LEDE_BYTES

#: What a clipped value ends with. Words rather than an ellipsis: a bare "..." reads as the
#: publisher's own punctuation, so a reader cannot tell a trimmed sentence from a trailing
#: one, which is the same ambiguity a silent row prefix creates.
_CLIPPED: Final[str] = " [trimmed]"

NOT_SAID: Final[str] = "not recorded"
"""What :data:`UNSAID` reads as on the card. Words, never a blank and never a zero.

TWO KINDS OF FIELD, and only one of them uses this. A VALUE field holds something a reader
counts, so a blank there cannot be told from a zero and the gap has to be named. A NOTICE field
says something ABOUT a value -- the contract version, a metric's gloss -- and an empty notice
means there is nothing to notice, which is the honest reading and the quiet one.

Naming a silent notice puts words where the reader expected an explanation and gets an apology
instead: a tile reading "ROUND 3 / not said" is a number made to look incomplete, and a malformed
board becomes a wall of the phrase. :func:`_notice` is the one place that distinction is applied.
"""


def _notice(value: Any, budget: int) -> str:
    """A NOTICE field: the publisher's words, or nothing at all.

    Deliberately not :func:`_text`. A notice nobody wrote is absent rather than unknown, so it
    says nothing; a notice that was written and cannot be read still says so, because that is a
    fact about the publisher's data rather than about its silence.
    """
    if value is UNSAID or value == UNSAID or value is None:
        return ""
    text = _text(value)
    return "" if text == NOT_SAID else _clip(text, budget)


UNREADABLE: Final[str] = "could not be read"
"""What a value that WAS published and cannot be read as text reads as.

Its own words, and not folded into :data:`NOT_SAID`. Three states, not two: a field
nobody filled and a field holding an object are different facts, and a reader who is
told "not said" about the second will look for a publisher who never existed. The drawer
template distinguished them; a card with no script still can, because this function is
where the distinction is made.

A PHRASE rather than the bare adjective "unreadable": shown one word, a reader could not
tell missing-because-unknown from missing-because-broken, and the word appearing in the
LEDE was read as a verdict on the whole board rather than on that one sentence. "could not
be read" says what happened, and says it about this value.
"""


def _text(value: Any) -> str:
    """One published value as card text: readable text, :data:`NOT_SAID`, or
    :data:`UNREADABLE`.

    THE ONLY ROUTE a contract value takes into a data field, so the three states cannot
    be spelled differently in two places. Total by construction: the input is typed, but
    a panel built from a record that predates this contract reaches here untouched, so an
    object where a string belongs is a reachable value rather than a theoretical one --
    and ``str()`` on one whose ``__str__`` raises would take the whole card out.
    """
    if value is UNSAID or value == UNSAID or value is None:
        return NOT_SAID
    if isinstance(value, bool):
        # Before the ``int`` arm: ``bool`` is a subclass of it, so a stray ``True``
        # would otherwise print as ``1`` and read as a count.
        return UNREADABLE
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip() or NOT_SAID
    return UNREADABLE


def _prose(value: Any) -> str:
    """One published value that must READ as words, not as a number.

    Same three states as :func:`_text`, minus its ``int`` arm. That arm is right for a field
    whose value is a quantity, and wrong for one a reader consumes as language: a stray ``7``
    in the check tally prints "checks 7", which claims seven of something, and a stray ``3``
    in the action prints "-> 3", which a reader cannot act on or even interpret. Both fields
    are declared ``str`` by the contract, so a number there is a malformed record -- and
    "could not be read" is the true thing to say about it.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value is not UNSAID:
        return UNREADABLE
    return _text(value)


def _clip(text: str, budget: int) -> str:
    """*text* within *budget* UTF-8 bytes, saying so when it did not fit.

    Cut on a CHARACTER boundary, not a byte one: slicing UTF-8 bytes mid-sequence yields
    text that is not decodable, and the card's data is JSON. Encoding each prefix would be
    quadratic on a long value, so the byte budget bounds the character slice first (a
    character is at most four bytes) and one shrinking loop settles the remainder -- at most
    a few iterations, since only the multi-byte characters inside that slice can overshoot.

    :data:`NOT_SAID` and :data:`UNREADABLE` pass through untouched by being shorter than
    every budget; clipping them would be clipping this module's own words.
    """
    if len(text.encode("utf-8")) <= budget:
        return text
    room = max(0, budget - len(_CLIPPED.encode("utf-8")))
    body = text[: max(1, room)]
    while len(body.encode("utf-8")) > room and len(body) > 1:
        body = body[:-1]
    return body + _CLIPPED


def _count(value: Any) -> int | None:
    """*value* as a non-negative count, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _age_words(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _when(meta: Any) -> str:
    """The header's one time phrase: when it was captured, how old it is, stale or not.

    ONE field because the drawer's header made it one text node too: a captured stamp
    with an age beside it and the word ``stale`` appended reads as a phrase, and splitting
    it across three fields would spend two of the card's remaining slots on punctuation.
    """
    meta = meta if isinstance(meta, dict) else {}
    # THROUGH ``_human_stamp``, like ``_since``: the producer passes the record's
    # ``published_at``, written as an ISO timestamp, and a reader of a status header wants a
    # time of day rather than "2026-09-29T15:37:04+00:00".
    captured = _human_stamp(_text(meta.get("captured_at")))
    age = _count(meta.get("age_seconds"))
    cap = _count(meta.get("stale_after_seconds"))
    if age is None:
        # The age is measured from the LOG's newest entry, so a board with no entry has
        # no age. That is not an age of zero and does not get a number.
        return f"{captured} · age {NOT_SAID}"
    # LABELLED, like the missing branch above. Without the word this read "13:41 UTC * 10m 20s":
    # a bare duration next to a time, which a reader takes for how long something RAN. The two
    # branches now differ only in the value, which is the only thing that differs.
    words = f"{captured} · age {_age_words(age)}"
    return f"{words} · stale" if cap is not None and age > cap else words


def _human_stamp(text: str) -> str:
    """An ISO timestamp as a reader's date, or *text* unchanged when it is not one.

    Unchanged rather than blank or reformatted-anyway on a value that does not parse: the stamp
    comes from the log, and printing something a reader can compare to what the log holds
    matters more than printing something tidy.
    """
    try:
        when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        # ValueError: not an ISO timestamp (``fromisoformat`` also rejects a year outside
        # 1..9999 here). OverflowError / OSError: a value that PARSES but whose tz conversion
        # falls out of range -- a boundary year with an offset (``0001-...+23:59`` underflows
        # below ``datetime.min`` on the UTC shift, ``9999-...-14:00`` overflows above
        # ``datetime.max``) or a platform ``strftime`` limit. Either way the raw text is
        # returned -- the same "unchanged when it is not a usable one" contract -- rather than
        # letting the arithmetic escape past the ``except OSError`` at the drawer-read call
        # site as a 500 the card build is documented never to raise.
        return text


#: Said once, in the footer, because readers keep trying to click. Item ids, pull request numbers
#: and remainder counts all read as things that lead somewhere, and on this card none of them do:
#: a dynamic dashboard card is inert by contract, the host strips every control, and no rewording
#: of a count has removed the invitation. So the card states its own nature in WORDS -- the one
#: channel a reader who cannot see a cursor still has -- rather than leaving each identifier to
#: disappoint separately.
_READ_ONLY_NOTE: Final[str] = "nothing on this card is a link"


def _since(stamp: Any) -> str:
    """The footer: when this board's first entry landed, and that the card cannot be acted in.

    An ABSENCE KEEPS ITS NOUN here like everywhere else. A bare "not recorded" floated in the
    footer naming nothing, which is the same defect the row cells had: the phrase says something
    is missing and the reader cannot tell WHAT.
    """
    text = _text(stamp)
    if text in (NOT_SAID, UNREADABLE):
        return f"first entry {text} \u00b7 {_READ_ONLY_NOTE}"
    # FORMATTED like the header's stamp. A raw ISO value reads as machine output beside
    # prose, and the reader has no use for the offset or the seconds.
    return f"first entry {_human_stamp(text)} \u00b7 {_READ_ONLY_NOTE}"


def _contract_note(published: Any) -> str:
    """Whether the board and this card agree on the contract, in words.

    SILENT WHEN THEY AGREE, which is the one field on this card that may be empty.

    It said "contract version 1" on every healthy board, on the reasoning that a card saying
    nothing about its version cannot be told from one whose disclosure was dropped. That is a
    real risk and the wrong place to answer it: "contract" is a word about this code's internals
    and no reader of a status board can parse it, so the cost was permanent jargon in the
    header of every board that is fine. The risk moves to the suite instead, where a test
    requires both abnormal cases to produce text -- a guard that cannot be read by the wrong
    audience.

    Every other field carries a VALUE and is never blank, because there a blank is
    indistinguishable from a zero. This one carries a NOTICE, and an empty notice means there is
    nothing to notice.
    """
    # IN THE READER'S WORDS, not this code's. The docstring above already concedes that
    # "contract" is a word about internals, and both of these fire at the moment a reader most
    # needs to be told something useful -- so neither says "contract" and neither prints a
    # version number, which names nothing the reader can act on. What they must convey is the
    # same either way: this board may be wrong, and why.
    wrote = _count(published)
    if wrote is None:
        return f"the version that saved this board {UNREADABLE}, so some details may be wrong"
    if wrote != CONTRACT_VERSION:
        return (
            "this board was saved by a different version of Kiro Crew, "
            "so some details may be missing or wrong"
        )
    return ""


def _omitted_note(omitted: Any, total: Any = UNSAID) -> str:
    """Entries the fold dropped, as a positive statement either way.

    An item missing from a board is not recoverable by whoever reads it, so the count
    is stated on the card. Zero says so rather than saying nothing, because a field bound
    to the empty string is exactly what a dropped disclosure looks like.

    "NOT SHOWN" WAS THE WRONG VERB and it misread on every board: log entries are never
    displayed on this card at all, so "not shown" invited a reader to look for the ones that
    were, and one guessed that clicking might reveal them. What the number reports is how much
    of the log the fold left OUT of the counts beside it -- a fact about those numbers, not a
    promise of rows behind a control this card cannot have.
    """
    lost = _count(omitted)
    if lost is None:
        return f"log entries left out: {UNREADABLE}"
    if lost == 0:
        return "every log entry counted"
    # NAMED AGAINST THE TILE'S OWN TOTAL, because the two numbers are one system and a reader
    # cannot see that from two bare counts: the tile says how many entries the log holds, this
    # says how many of THOSE the fold dropped. Without the total a reader cannot tell whether
    # one is a subset of the other, and the tile is right beside it on every board.
    held = _count(total)
    if held is None:
        return f"{lost} log entries left out of these counts"
    return f"{lost} of the log's {held} entries left out of these counts"


#: The band the headline counts. One of :data:`BOARD_COLUMN_NAMES`, asserted below, so
#: the most-read number on the card cannot be built from a state no board ever carries.
_SETTLED_STATE: Final[str] = "accepted"

#: Every TERMINAL state -- the ones an item does not leave once it reaches them. The
#: headline's "settled" count sums these, and the count is ``accepted`` + ``rejected`` +
#: ``abandoned``, not ``accepted`` alone: a verdict against an item still settles it. Counting
#: only ``accepted`` had treated every terminal-but-not-accepted item as still moving. Derived
#: as the complement of ``open`` so the set stays correct if the vocabulary gains a state: a new
#: state is terminal unless it is the open one.
_TERMINAL_STATES: Final[frozenset[str]] = frozenset(BOARD_COLUMN_NAMES) - {"open"}

assert _SETTLED_STATE in BOARD_COLUMN_NAMES, (
    f"the card headline counts {_SETTLED_STATE!r}, which is not one of "
    f"{sorted(BOARD_COLUMN_NAMES)}"
)

assert _TERMINAL_STATES and _TERMINAL_STATES < frozenset(BOARD_COLUMN_NAMES), (
    f"the terminal set {sorted(_TERMINAL_STATES)} must be a non-empty proper subset of "
    f"{sorted(BOARD_COLUMN_NAMES)} -- the board needs at least one non-terminal (open) state"
)


def _legend(progress: Any, round_value: Any = UNSAID) -> str:
    """The progress line: counts with their denominator, never a percentage.

    Carries ``total``, ``added_since`` and every segment, because those three leaves have
    no field of their own -- the card's remaining slots went to the board itself, and a
    legend is how the drawer rendered them too: one text node reading
    ``2 of 6 settled · +1 in round N``.

    *round_value* NAMES the round rather than pointing at it. "this round" sat beside a tile
    reading ``round N`` and a reader could not tell whether the two meant the same pass, which
    is the whole cost of a demonstrative: it refers, and a card has no conversation to refer
    into. Read from the tile's own characters, so the phrase cannot disagree with the number
    printed next to it. Falls back to "this round" when the board publishes no readable round --
    vaguer, but never wrong.

    A remainder is NAMED rather than absorbed: segments that do not add up to the total
    mean the board and its bands disagree, and dividing the difference among the bands
    would hide exactly that.
    """
    present = isinstance(progress, dict) and bool(progress)
    progress = progress if isinstance(progress, dict) else {}
    total = _count(progress.get("total"))
    raw = progress.get("segments")
    bands: list[tuple[str, int]] = []
    for seg in raw if isinstance(raw, list) else []:
        if not isinstance(seg, dict):
            continue
        n = _count(seg.get("n"))
        if n is None:
            continue
        bands.append((_text(seg.get("name")), n))
    if total is None:
        # THREE STATES here too, and the difference matters more than anywhere else on
        # the card: nothing published is a provider that skipped a required key, while
        # something unreadable is a board whose own arithmetic cannot be drawn. Told the
        # same way, a reader chases the wrong half.
        return f"progress {UNREADABLE}" if present else f"progress {NOT_SAID}"
    if total == 0:
        # A well-formed board with nothing counted. An empty board, not a broken one,
        # and it must not reach the malformed reading above.
        #
        # NAMED ROUND, because the tiles beside it show a log with entries and a round past
        # its first: `no items yet` read as "nothing has ever happened here" and left a reader
        # unable to explain how an empty board has history. An empty board in a LATER round is
        # the ordinary case -- every item settled and closed -- and saying WHICH round says so
        # without asking the reader to match a demonstrative to the tile.
        return f"no items on the board {_round_phrase(round_value)}"
    terminal = sum(n for name, n in bands if name in _TERMINAL_STATES)
    # WHAT THE COLUMN HEADS DO NOT ALREADY SAY, and a ratio is not that. Every band has a
    # column whose head prints its name and its share, so a single band's ratio put the SAME
    # two numbers in two places. The count the legend adds is the one no single head can print:
    # how much of the board has SETTLED -- the AGGREGATE across every terminal band. No head
    # shows that sum (each shows one band), and "how far from done" is what a reader of a
    # progress line wants.
    #
    # SETTLED is every terminal state, not just `accepted`: an item that was rejected or
    # abandoned has finished too. An earlier version read "N still to settle" as
    # `total - accepted`, which both miscounted (rejected and abandoned were counted as still
    # moving) and, once corrected to `total - terminal`, became the OPEN band exactly -- and
    # "still to settle" is open by definition, so a corrected count there just restated the open
    # head. Counting the settled side instead is the aggregate no head carries and is correct
    # the moment it is phrased: an item is settled iff it is in a terminal band.
    settled = terminal
    # WITH ITS DENOMINATOR, like every other count here: a bare "3" on a card of counts says
    # nothing about whether that is most of the board or a corner of it. The repetition a reader
    # can see is the board's TOTAL recurring, which is the price of that rule and cheaper than
    # the figure it buys.
    parts = [f"{settled} of {total} settled"]
    counted = sum(n for _name, n in bands)
    if counted < total:
        parts.append(f"{total - counted} unaccounted")
    added = _count(progress.get("added_since"))
    if added:
        # "since" would be a duration; this is a count of items whose round is the
        # conductor's current one, which on a one-round board is every item.
        parts.append(f"+{added} {_round_phrase(round_value)}")
    return " · ".join(parts)


def _column_head(column: Any, total: int | None) -> str:
    """One column's heading: its name and its share of the board."""
    column = column if isinstance(column, dict) else {}
    cards = column.get("cards")
    held = len(cards) if isinstance(cards, list) else None
    name = _text(column.get("name"))
    if held is None:
        return f"{name} · {UNREADABLE}"
    # WITH ITS DENOMINATOR. A bare "3" over a column says nothing about whether that is
    # most of the board or a corner of it.
    return f"{name} · {held} of {total}" if total is not None else f"{name} · {held}"


def _column_rows(column: Any, *, limit: int, actions: bool) -> str:
    """One column's cards, as newline-separated text for a ``pre-line`` block.

    ONE FIELD for an unbounded list, which is the whole reason this is text rather than
    a field per card: a board may hold 256 items and the host drops a card over 24 fields
    WHOLE. A prefix is printed and the rest is COUNTED in the same text, so a reader is
    never shown part of a board as if it were all of it.
    """
    column = column if isinstance(column, dict) else {}
    raw = column.get("cards")
    cards = raw if isinstance(raw, list) else []
    if not cards:
        # "no items", not "empty": one adjective could not be told from a value that is
        # genuinely zero, which is the same confusion the absence words above address.
        return "no items"
    lines: list[str] = []
    for card in cards[:limit]:
        card = card if isinstance(card, dict) else {}
        # The id is PREFIXED unless it already announces itself. A pull request id opens with a
        # hash and needs no noun; a raw fold id reads "it_4", which a reader cannot place --
        # and it sits in a column beside siblings that do announce themselves, so the odd one
        # out is the one nobody can identify. Same reasoning as the tally's "checks".
        #
        # INCLUDING AN ABSENCE, which is the harder half. These cells are positional, with no
        # header over them, so the noun is the only thing saying WHICH detail a cell is about.
        # Exempting the absence words read tidier in the source and cost the reader the fact:
        # a row printed `#42 . not recorded . checks 161/161`, and on a board whose fold
        # could not be read, three bare `could not be read` cells in a line. The tally never
        # took the exemption -- it says `checks not recorded` -- so this is also what makes the
        # three cells answer the same way.
        shown_id = _clip(_text(card.get("id")), _CELL_BYTES)
        if shown_id and not shown_id.startswith("#"):
            shown_id = f"item {shown_id}"
        # The session key reads `chat-1875`, which names nothing a reader knows -- same answer
        # the id and the tally got, and for the same reason: a bare identifier in a column of
        # them explains itself to nobody who already knows the shape.
        #
        # "session", not the role that holds it. The field is a session key, and a reader asked
        # whether the role word named "a person, a chat, or a robot" -- three guesses, which is
        # what a role word costs when the thing on screen is an identifier. The noun says what the
        # STRING is; whose it is, the column it sits in already answers.
        shown_sub = _clip(_text(card.get("sub")), _CELL_BYTES)
        if shown_sub:
            shown_sub = f"session {shown_sub}"
        cells = [shown_id, shown_sub]
        # ``id`` and ``sub`` keep ``_text``: an item id or a session key arriving as a number
        # still reads as one. The two below do not, which is why they read differently.
        # The tally is published fraction-shaped ("41/47"), so on its own it says how many of
        # something without saying of WHAT. The noun is fixed because the field is: the contract
        # defines ``of`` as a CI check tally and refuses anything else there, so this cannot
        # mislabel a value some publisher used differently. Prefixed AFTER the clip, so the
        # label is not what gets trimmed away.
        cells.append(f"checks {_clip(_prose(card.get('of')), _CELL_BYTES)}")
        lines.append(" · ".join(cells))
        if actions:
            # The action belongs to ONE item, under that item's own row. Printed once for
            # the column instead, only the first item's sentence survives and every other
            # item needing a person goes unmentioned.
            you = card.get("you")
            if you is not UNSAID and you != UNSAID and you is not None:
                # CLIPPED, like every other publisher-written value: one very long action
                # sentence would otherwise be shortenable only by the ladder's second
                # retreat, which drops EVERY item's action rather than trimming the one
                # that does not fit.
                lines.append(f"    -> {_clip(_prose(you), _ACTION_BYTES)}")
    rest = len(cards) - limit
    if rest > 0:
        # A COUNT of what exists, not an offer to reveal it. "+N of M not shown" read as an
        # expander and a reader said they would try clicking it; nothing on an inert card can
        # answer that. Same arithmetic, stated as how many more items there are.
        lines.append(f"    +{rest} more items (of {len(cards)})")
    return "\n".join(lines)


def _round_phrase(round_value: Any) -> str:
    """``in round N`` when the board published a readable round, else ``this round``.

    One phrase, built once, because the legend says it in two places and two spellings of the
    same idea is the defect this fixes rather than a smaller version of it.
    """
    shown = _text(round_value)
    if not shown or shown in (NOT_SAID, UNREADABLE):
        return "this round"
    return f"in round {shown}"


def _stat_value(panel: Any, key: str) -> Any:
    """The raw ``v`` of one stat tile, or :data:`UNSAID` when the board has no such tile.

    Read from the panel rather than recomputed, so a phrase naming the tile's number cannot
    drift from the tile itself.
    """
    stats = panel.get("stats") if isinstance(panel, dict) else None
    for tile in stats if isinstance(stats, list) else []:
        if isinstance(tile, dict) and tile.get("k") == key:
            held = tile.get("v")
            # PARSED BACK from the tile's own text, which is where every card value lives. Read
            # from the fold instead, a phrase naming the tile's number could disagree with the
            # tile printed beside it; reading the same characters makes that impossible.
            if isinstance(held, str) and held.strip().isdigit():
                return int(held.strip())
            return held
    return UNSAID


#: Tiles whose label is a word only this system uses, with the gloss the card falls back to when
#: the publisher wrote none. ``items on board`` and ``log entries`` say what they are in their own
#: labels; "round" does not, and a reader who has never met the conductor never learns it. The
#: publisher's note is OPTIONAL, so most real boards would show the tile bare -- which is why this
#: is a fallback here rather than advice to publishers.
_STAT_GLOSS: Final[dict[str, str]] = {
    # PLAIN WORDS, and the test beside this pins which ones. A gloss naming the machinery
    # ("the conductor has swept its items") explains one unknown word with two more; a gloss
    # that is a bare synonym ("the current pass") is circular. What makes this one readable is
    # the referent: a pass OVER THIS BOARD is a thing on screen, so the reader learns what is
    # being counted without being told what a conductor is.
    "round": "one round is one full pass over this board",
    # THE TWO BIGGEST NUMBERS ON THE CARD, and a reader could not tell them apart: 41 against 6,
    # 400 against 40. Each label says what it counts and neither says how the two differ, so the
    # glosses define them AGAINST each other -- one item is a piece of work, one entry is a line
    # written about that work, so entries outnumber items by design rather than by accident.
    "items": "one item is one piece of work the crew is tracking",
    "entries": "one entry is one line the log holds about that work, so there are more of these",
}


def _note_beside(value: str, note: str) -> str:
    """*note* unless it says nothing the *value* has not already said.

    A tile is a value and a gloss. When the gloss repeats the value the tile reads the same
    phrase twice -- "could not be read / could not be read" -- which is the wall this module's
    own docstring warns about, and it tells the reader nothing the first phrase did not.
    """
    return "" if note.strip() == value.strip() else note


def _stat_field(key: str) -> str:
    """The field name a metric's value is bound by. The metric's KEY reaches the card
    here rather than as a value of its own: a tile's label is the same on every board
    ever published, so it is literal text in the page and this is what ties the two."""
    return f"stat_{key}"


def panel_card_data(panel: PipelineBoardPanel) -> dict[str, str]:
    """*panel* as the ``data`` half of a dynamic dashboard card.

    THE FLATTENING RULE, in four lines, because ``data`` is flat and a dotted name is not
    even legal -- ``normalize_card``'s ``_FIELD_NAME`` admits
    ``[a-zA-Z][a-zA-Z0-9_-]{0,47}`` only:

    1. A leaf's path becomes its field name with ``_`` for ``.``: ``meta.name`` is
       ``meta_name``, ``omitted`` is ``omitted``.
    2. A list whose length is fixed by a CLOSED vocabulary indexes into one field group
       per element: ``columns`` is :data:`BOARD_COLUMN_NAMES`, so ``columns[0]`` is
       ``column_0_``. ``stats`` is keyed by its own ``k`` instead of by position, so a
       tile's label can be literal text in the page.
    3. A list whose length is UNBOUNDED collapses to one text field on its parent, its
       elements newline-separated: a board holds up to
       :data:`~kiro_crew.work_vocab.WORK_STORED_ITEM_LIMIT` items and the host drops a
       card over 24 fields whole, so ``columns[0].cards`` is ``column_0_rows``.
    4. A leaf the page renders as part of a SENTENCE has no field of its own and reaches
       the card inside the finished sentence: ``meta.age_seconds`` is in ``meta_when``,
       ``progress.total`` is in ``progress_legend`` and in every ``column_N_head``. A
       card has no script to compose a sentence, so composition happens here.

    Every value is words. :data:`UNSAID` becomes :data:`NOT_SAID`, and a published value that
    cannot be read as text becomes :data:`UNREADABLE`. No field carrying a VALUE is ever the
    empty string: the host binds a field it was not given to ``""``, so a blank is what a
    DROPPED field looks like and must not also be what a real value looks like.

    The exception is a field carrying a NOTICE rather than a value -- ``contract_note`` and each
    ``stat_*_note`` -- which is empty exactly when there is nothing to notice. A notice has no
    zero to be confused with, so silence reads correctly there and a permanent phrase would be
    jargon on every healthy board. :func:`_notice` is the one place that decides it.

    Bounded before it returns: the row limit is lowered, and then the per-item action
    lines dropped, until the card fits the host's own ``MAX_DATA_BYTES``. Both retreats are
    stated in the card's own text.
    """
    meta = _mapping_of(panel, "meta")
    progress = _mapping_of(panel, "progress")
    total = _count(progress.get("total"))
    raw_columns = panel.get("columns")
    columns = raw_columns if isinstance(raw_columns, list) else []

    fixed: dict[str, str] = {
        "meta_name": _clip(_text(meta.get("name")), _NAME_BYTES),
        "meta_when": _when(meta),
        # The ENTRIES tile's total, so the two log-entry numbers read as one system.
        "omitted": _omitted_note(panel.get("omitted"), _stat_value(panel, "entries")),
        "contract_note": _contract_note(panel.get("contract_version")),
        "lede": _clip(_text(panel.get("lede")), _LEDE_BYTES),
        "progress_legend": _legend(progress, _stat_value(panel, "round")),
        # NAMED, not printed bare. The drawer's footer put this stamp on the page alone,
        # where it reads as a date with no claim attached -- a viewer cannot tell the
        # board's first entry from its last, from a capture time, or from a deadline. The
        # header already carries a stamp, so an unlabelled second one is worse than none.
        "since": _since(panel.get("since")),
    }
    # MATCHED BY NAME over the closed vocabulary, never indexed by position into what
    # arrived. Position looks equivalent because the provider emits the four states in
    # order, but a board missing one column then shifts every later column's field one
    # place and prints one state's items under another state's heading -- a wrong board
    # rather than an incomplete one, and nothing on the page says so. A state no column
    # carries gets an empty column, which is the true reading.
    by_name = {
        str(column.get("name") or ""): column for column in columns if isinstance(column, dict)
    }
    ordered = [by_name.get(name, {"name": name, "cards": []}) for name in BOARD_COLUMN_NAMES]
    for index, column in enumerate(ordered):
        fixed[f"column_{index}_head"] = _column_head(column, total)
    # KEYED BY THE CLOSED VOCABULARY, not by what arrived. Writing a field per stat the
    # payload happens to carry is the one mistake this card cannot survive: the host binds
    # a field it was NOT given to the empty string, so a tile whose key went missing
    # renders as a blank number -- indistinguishable from a real zero, on the three
    # figures a reader checks first. Every field the page binds is written here, every
    # time, and a tile with no value in the payload says so in words.
    raw_stats = panel.get("stats")
    found = {
        str(stat.get("k") or ""): stat
        for stat in (raw_stats if isinstance(raw_stats, list) else [])
        if isinstance(stat, dict)
    }
    for key in BOARD_STAT_KEYS:
        tile: Any = found.get(key) or {}
        # The value is derived (a count), the NOTE is the publisher's gloss -- so only the
        # note can be long enough to matter, and only it is clipped.
        shown = _text(tile.get("v"))
        fixed[_stat_field(key)] = shown
        # The publisher's gloss wins where there is one: it is about THIS board, while the
        # fallback is about the metric. Empty means the publisher was silent, not that the word
        # explains itself.
        gloss = _notice(tile.get("note"), _NOTE_BYTES) or _STAT_GLOSS.get(key, "")
        fixed[f"{_stat_field(key)}_note"] = _note_beside(shown, gloss)

    # The retreats, in order of what they cost a reader: printing fewer rows loses whole
    # items, dropping the action lines loses what a person must DO about items that are
    # still listed. Fewer rows goes first because the count of what was not printed
    # travels with it, and a dropped action leaves no trace on the row it belonged to.
    # CLAMPED to the longest column, not started at the cap. A limit above every column's
    # length prints the same rows as that length does, so starting at the cap spends a full
    # rebuild of all four columns on each step that cannot change the output -- invisible
    # at a cap of 12 and ~100k rebuilds per panel read the moment anyone raises it. The
    # first limit that can change anything is the longest column.
    longest = max(
        (len(c.get("cards") or []) for c in ordered if isinstance(c.get("cards"), list)),
        default=0,
    )
    start = max(1, min(_ROWS_PER_COLUMN, longest))
    smallest = dict(fixed)
    for actions in (True, False):
        for limit in range(start, 0, -1):
            smallest = dict(fixed)
            for index, column in enumerate(ordered):
                smallest[f"column_{index}_rows"] = _column_rows(
                    column, limit=limit, actions=actions
                )
            if _data_bytes(smallest) <= _HOST_MAX_DATA_BYTES:
                return smallest
    # One row per column, no actions, and still over: every remaining byte is in a field
    # this function cannot shorten without inventing a value, so return the smallest card
    # it can build honestly and let ``normalize_card`` refuse it. A refused card leaves
    # the previous one in place; a silently truncated sentence would not.
    return smallest


def _mapping_of(panel: Any, key: str) -> dict[str, Any]:
    """*panel*'s *key* as a mapping, or an empty one.

    The input is typed, so mypy proves the provider's own output has these keys. This
    guard is for the OTHER caller: a record written before this contract existed reaches
    the flattener untouched, and a non-mapping there would raise inside the one function
    whose whole job is to be total.
    """
    value = panel.get(key) if isinstance(panel, dict) else None
    return value if isinstance(value, dict) else {}


def _data_bytes(data: dict[str, str]) -> int:
    """Keys plus values, UTF-8 -- the same sum ``normalize_card`` caps."""
    return sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in data.items())
