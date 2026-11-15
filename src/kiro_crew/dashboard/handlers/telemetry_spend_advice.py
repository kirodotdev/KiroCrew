"""Plain recommendations for the Telemetry Spend tab, each with its measured share.

A recommendation is listed only when the token rows the spend breakdown already
read prove it: the share is a sum over recorded turns divided by the window's
total, never a projection of what a change would save. A rule whose inputs are
absent or too thin stays off the list.

The rows carry ids and numbers, not sentences: the dashboard words them in the
reader's language.
"""

from __future__ import annotations

from typing import Any

# A turn above this many context tokens re-sends that much on every call, which
# is what makes a long session expensive. The same absolute scale the spend
# breakdown's context bands use, because window sizes differ per model.
HIGH_CONTEXT_TOKENS = 600_000

# Below this share a rule is noise next to the rest of the window.
MIN_SHARE_PCT = 10.0

# A model is compared only on this many recorded turns, so one odd turn cannot
# set its per-turn price.
MIN_MODEL_TURNS = 5

# The dearest model must cost at least this many times the cheapest one per turn
# before swapping is worth a sentence.
MIN_PRICE_RATIO = 2.0


def spend_advice(
    total: float,
    high_context: float,
    by_model: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recommendations with a measured share, largest share first.

    ``total`` is the window's spend, ``high_context`` the part of it on turns above
    :data:`HIGH_CONTEXT_TOKENS`, and ``by_model`` the window's per-model
    ``{"credits", "turns"}`` entries keyed by model name.
    """
    if total <= 0:
        return []
    out: list[dict[str, Any]] = []

    share = high_context / total * 100
    if share >= MIN_SHARE_PCT:
        out.append(
            {"id": "high_context", "share_pct": round(share, 1), "tokens": HIGH_CONTEXT_TOKENS}
        )

    # A blank model is recorded as "unknown": not a model a person could pick.
    priced = {
        name: float(e["credits"]) / int(e["turns"])
        for name, e in by_model.items()
        if name != "unknown" and int(e["turns"]) >= MIN_MODEL_TURNS
    }
    if len(priced) >= 2:
        dearest = max(priced, key=lambda n: priced[n])
        cheapest = min(priced, key=lambda n: priced[n])
        if priced[cheapest] > 0 and priced[dearest] >= MIN_PRICE_RATIO * priced[cheapest]:
            share = float(by_model[dearest]["credits"]) / total * 100
            if share >= MIN_SHARE_PCT:
                out.append(
                    {
                        "id": "costly_model",
                        "share_pct": round(share, 1),
                        "model": dearest,
                        "cheaper_model": cheapest,
                        "ratio": round(priced[dearest] / priced[cheapest], 1),
                    }
                )

    return sorted(out, key=lambda a: -float(a["share_pct"]))
