"""The ``usage`` session fold: what a session spent in tokens and credits, the context
injected per turn, and its compactions -- with the caps it keeps and the credit table
(``CREDIT_SOURCES``) every billing fold charges by.
"""

from __future__ import annotations

import math
from typing import Any, Final

from kiro_crew.crew_log.projection_usage_measured import (
    context_apply,
    context_render,
    context_start,
)
from kiro_crew.crew_log.projection_values import _as_int, _as_str, _keyable, _note_omitted
from kiro_crew.crew_log.schema import Entry

#: Distinct models a ``usage`` projection details per model. Past it the whole-
#: session totals stay exact and ``models_omitted`` counts the models left out,
#: the same posture as ``TOOL_NAME_LIMIT``. A turn carries a model string, so an
#: unbounded ``by_model`` would grow the checkpoint over a long session.
MODEL_LIMIT: Final[int] = 100

#: The token dimensions ``turn/completed`` bills, in the order it declares them.
TOKEN_DIMENSIONS: Final[tuple[str, ...]] = ("input", "output", "cache_read", "cache_write")


# --------------------------------------------------------------------------- #
# usage
# --------------------------------------------------------------------------- #


def _usage_start() -> dict[str, Any]:
    return {
        "turns_completed": 0,
        "credits": 0.0,
        # The same charges the total above sums, kept apart by who spent them. Every
        # bucket is present from the start, so an absent source reads as "spent
        # nothing" rather than leaving the reader to guess whether the split is
        # partial. ``reported`` counts the entries that carried a measurement, which
        # is what says how much of the bucket's total is covered -- an absent
        # ``credits`` is an unmetered provider, never a zero charge.
        "credits_by_source": {source: {"credits": 0.0, "reported": 0} for source in CREDIT_SOURCES},
        "tokens": {dimension: 0 for dimension in TOKEN_DIMENSIONS},
        "tokens_turns": 0,
        "duration_ms": 0,
        "duration_turns": 0,
        "by_model": {},
        "models_omitted": 0,
        "models_omitted_saturated": False,
        "omitted_models": [],
        "context_tokens": 0,
        "context_chars": 0,
        "context_blocks": 0,
        "context_estimated": 0,
        # The context part: one fixed-size state folded by ``context_apply`` (see
        # ``projection_usage_measured``). The four counters above are session totals
        # the Crew Log panel reads; nothing here grows with the session.
        "ctx": context_start(),
        "compactions": 0,
        "freed_pct": 0.0,
        "steps": 0,
        "step_ms": 0,
    }


def _credit_charge(value: Any) -> float | None:
    """*value* as a credit charge this module can arithmetic on, else ``None``.

    THE module's one screen for a charge coming off the wire, shared by every fold that
    reads a ``credits`` field -- ``usage`` through :func:`_bill_credits`, and ``subagents``
    per row. Spelled once because it is one question, and two spellings of it drift: the
    second would be the one that forgets a case.

    Two shapes get through a naive check. ``bool`` is an ``int`` in Python, so ``True``
    would bill as one credit. And a JSON integer is UNBOUNDED while a ``float`` is not, so
    ``float(10 ** 400)`` raises ``OverflowError`` rather than returning a wrong number --
    and nothing between here and :func:`fold_session` catches it, so an escaping raise
    would cost the whole projection over one line that stays on disk for good. A line this
    module cannot interpret costs that line and nothing else.

    Deliberately does NOT screen the VALUE for sign, NaN or infinity. That is the caller's
    business and belongs on the RESULT: a charge has many unusable shapes and an
    accumulated total has one invariant (stay finite, never go down), so checking the total
    needs one clause where checking the charge needs a clause per shape.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _bill_credits(state: dict[str, Any], entry: Entry) -> float | None:
    """Add this entry's charge to the session total and to its own source bucket.

    One function for all three spenders, so the total and the split cannot drift
    apart: a bucket that is incremented somewhere the total is not would make the
    two disagree, and a reader has no way to tell which half is wrong.

    Returns the charge, so ``turn/completed`` can also attribute it per model, and
    ``None`` when the entry carried no ``credits`` at all. Absent credits are NOT
    zero: a provider that does not bill in them writes no key, and folding that in
    as 0.0 would state a measurement nobody made. ``reported`` beside each bucket is
    what tells a reader how many charges the bucket's total covers.
    """
    source = _CREDIT_SOURCE_OF.get(entry.type)
    if source is None:
        return None
    billed = _credit_charge(entry.data.get("credits"))
    if billed is None:
        return None
    bucket = state["credits_by_source"][source]
    total = state["credits"] + billed
    grown = bucket["credits"] + billed
    # ONE invariant, and it is about the RESULT rather than the charge: a spend
    # total stays finite, and it never goes down.
    #
    # Checking the charge instead needs a new clause per shape, and the shapes
    # outnumber the clauses -- NaN, each infinity, a negative, and a pair of finite
    # values that overflow on the way up are four different inputs with one
    # consequence. That consequence is what is worth stating, because it is also
    # what cannot be undone: ``round`` keeps a non-finite total non-finite, the
    # savepoint persists it, and a cold refold reads the same entry again, so the
    # fold serves a broken total for the life of the unit.
    #
    # Zero passes deliberately. It does not move the total, and a provider reporting
    # 0.0 measured zero -- a different fact from a closer that reported nothing,
    # which is what ``reported`` beside each bucket exists to tell apart.
    #
    # ``by_model`` needs no check of its own: every billed charge is non-negative, so
    # a model's row is a sub-sum of ``state["credits"]`` and cannot exceed a total
    # this guard has already proved finite.
    if not math.isfinite(total) or not math.isfinite(grown):
        return None
    if total < state["credits"] or grown < bucket["credits"]:
        return None
    state["credits"] = total
    bucket["credits"] = grown
    bucket["reported"] += 1
    return billed


def _usage_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    billed = _bill_credits(state, entry)
    # The context part folds every entry it reads through its own pure apply; an entry
    # that does not concern it hands back the same object.
    state["ctx"] = context_apply(state["ctx"], entry)
    if entry.type == "turn/completed":
        state["turns_completed"] += 1
        model = _as_str(data.get("model"))
        by_model = state["by_model"]
        per_model = None
        if _keyable(model):
            per_model = by_model.get(model)
            if per_model is None and len(by_model) < MODEL_LIMIT:
                per_model = {"turns": 0, "credits": 0.0, "credits_turns": 0, "tokens": 0}
                by_model[model] = per_model
        if per_model is None:
            # Past the model budget, or too long to key safely, the whole-session
            # totals below still count this turn exactly; only the per-model
            # detail is dropped. ``models_omitted`` counts the DISTINCT models
            # left out -- once each, not once per turn they ran -- which keeps the
            # retained ``by_model`` bounded over a long session without inventing
            # models that do not exist.
            _note_omitted(
                state,
                model,
                seen_key="omitted_models",
                count_key="models_omitted",
                saturated_key="models_omitted_saturated",
                budget=MODEL_LIMIT,
            )
        if per_model is not None:
            per_model["turns"] += 1
        # The session total and the turn bucket were already billed above. Only the
        # per-model row is left, and it stays turn-scoped: a child's closer names no
        # model, so attributing its spend to the parent turn's model would charge one
        # model for work another did.
        if billed is not None and per_model is not None:
            per_model["credits"] += billed
            per_model["credits_turns"] += 1
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            # A block counts as a REPORT only when some dimension is above zero. Logs
            # on disk carry four-zero blocks on closers whose provider sent no
            # counts; a completed turn cannot have cost zero tokens, so a block of
            # zeros is an absence written as a measurement, and counting it would
            # tell a reader beside a real bill that the turn measured 0. The sums
            # below are unaffected either way -- adding zeros moves nothing.
            reported = False
            for dimension in TOKEN_DIMENSIONS:
                measured = _as_int(tokens.get(dimension))
                state["tokens"][dimension] += measured
                if per_model is not None:
                    per_model["tokens"] += measured
                if measured > 0:
                    reported = True
            if reported:
                state["tokens_turns"] += 1
        duration = data.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            state["duration_ms"] += duration
            state["duration_turns"] += 1
    elif entry.type == "context/composed":
        state["context_tokens"] += _as_int(data.get("tokens"))
        state["context_chars"] += _as_int(data.get("chars"))
        if data.get("tokens_estimated") is True:
            state["context_estimated"] += 1
        sources = data.get("sources")
        if isinstance(sources, list):
            state["context_blocks"] += sum(1 for source in sources if isinstance(source, dict))
    elif entry.type == "compaction/applied":
        state["compactions"] += 1
        freed = data.get("freed_pct")
        if isinstance(freed, (int, float)) and not isinstance(freed, bool):
            state["freed_pct"] += float(freed)
    elif entry.type == "step/completed":
        state["steps"] += 1
        state["step_ms"] += _as_int(data.get("ms"))


def _usage_render(state: dict[str, Any]) -> dict[str, Any]:
    tokens = dict(state["tokens"])
    return {
        "turns": {
            "completed": state["turns_completed"],
            # Turn-scoped on purpose, and it stays that way now the total covers
            # three sources: this number answers how many of the session's TURNS
            # reported a cost, which a whole-session count could not.
            "credits_reported": state["credits_by_source"]["turn"]["reported"],
            "tokens_reported": state["tokens_turns"],
            "duration_reported": state["duration_turns"],
        },
        "credits": round(state["credits"], 6),
        # The total above, split by who spent it. Rounded per bucket so the parts
        # are each readable; a reader comparing them against the total is comparing
        # two roundings of the same sum, not two different sums.
        "credits_by_source": {
            source: {
                "credits": round(row["credits"], 6),
                "reported": row["reported"],
            }
            for source, row in state["credits_by_source"].items()
        },
        "tokens": {**tokens, "total": sum(tokens.values())},
        "duration_ms": state["duration_ms"],
        "by_model": {
            name: {
                "turns": row["turns"],
                "credits": round(row["credits"], 6),
                "credits_reported": row["credits_turns"],
                "tokens": row["tokens"],
            }
            for name, row in sorted(state["by_model"].items())
        },
        "models_omitted": state["models_omitted"],
        # True once the dedup budget is spent: ``models_omitted`` is then a floor,
        # not a total, the same posture ``names_omitted`` takes.
        "models_omitted_saturated": state["models_omitted_saturated"],
        "context": {
            "tokens": state["context_tokens"],
            "chars": state["context_chars"],
            "blocks": state["context_blocks"],
            "estimated_turns": state["context_estimated"],
            # The newest turn's one breakdown, the 30-turn trend ring and the newest
            # session-start row; see ``projection_usage_measured.context_render``.
            **context_render(state["ctx"]),
        },
        "compactions": {
            "count": state["compactions"],
            "freed_pct": round(state["freed_pct"], 4),
        },
        "steps": {"completed": state["steps"], "ms": state["step_ms"]},
    }


#: Where a credit charge came from, which is the split ``usage`` keeps beside its
#: total. Fixed rather than discovered: these are the three writers that carry a
#: ``credits`` field, so the buckets are a closed set and a reader is never shown a
#: partial split. A fourth spender would add a bucket here and move ``usage``'s own
#: :attr:`_Fold.state_version`, which is the version its savepoints carry.
CREDIT_SOURCES: Final[tuple[str, ...]] = ("turn", "subagent", "background")

#: Which bucket each billing entry type lands in.
_CREDIT_SOURCE_OF: Final[dict[str, str]] = {
    "turn/completed": "turn",
    "subagent/completed": "subagent",
    "subagent/failed": "subagent",
    "background/completed": "background",
}
