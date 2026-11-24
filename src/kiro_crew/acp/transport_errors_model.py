"""Which model an ACP error names, and whether the account is served it.

The provider's wording does not tell an entitlement failure from a capacity blip:
both arrive as "The model 'X' is not available". The patterns here name the
rejected model, and :func:`_model_is_unentitled` judges it against the account's
advertised list, or against the backend's own verdict when KAS names one
(``data.errorType: "InvalidModelError"``). ``transport_errors`` reads these for
its user-facing wording and its retry verdict, so the two cannot drift apart.
"""

from __future__ import annotations

import re
from typing import Sequence

from kiro_crew.acp.runtime_models import DEFAULT_MODEL, model_is_unusable

_RE_MODEL_UNAVAILABLE = re.compile(r"[Tt]he model '([^']+)' is not available")
# kiro-cli >= 2.16 rewording of the same capacity/rollout rejection, which
# names NO model: "The model you've selected is temporarily unavailable.
# Please use '/model' to select a different model and try again." Without its
# own pattern this wording fell through to the unknown-shape branch and was
# classified terminal, so unattended callers (cron, subagents, consolidation)
# failed fast on a momentary blip their retry ladder exists to absorb — the
# exact drift hazard the marker-coupling note above warns about. The quote
# class covers both the straight and typographic apostrophe in "you've".
_RE_MODEL_TEMP_UNAVAILABLE = re.compile(
    r"[Tt]he model you['\u2019]ve selected is temporarily unavailable"
)
# MPS ValidationException wording for a model the partition/account does not
# serve — distinct from the "is not available" capacity string above. Covers the
# ``auto`` sentinel in partitions that do not serve it.
_RE_INVALID_MODEL_ID = re.compile(r"[Ii]nvalid model ID:\s*([^\s,;'\"]+)")
# KAS names the cause beside the prose: ``data.errorType: "InvalidModelError"`` is
# the backend's INVALID_MODEL_ID validation answer, a model the account is not
# served (or no model at all). Its message is the same "The model 'X' is not
# available" sentence a capacity blip gets, so the token, not the sentence,
# tells them apart. Read off the ``data`` text like ``MonthlyLimitError`` is.
_RE_KAS_INVALID_MODEL_TYPE = re.compile(r"\bInvalidModelError\b")


def _model_is_unentitled(data: str, available_models: Sequence[str] | None) -> str | None:
    """Return the rejected model name iff the account is not entitled to it.

    Upstream reports entitlement failures and transient capacity failures with
    the SAME string ("The model 'X' is not available"), so the string alone
    cannot tell them apart. The advertised model list can: it is captured at
    session init from what this account is actually served, so a rejected model
    that is absent from it was never on offer -- an entitlement problem no retry
    can fix. A rejected model that IS advertised really is a transient
    capacity/rollout blip.

    Returns None when the model is advertised, when nothing was rejected, or
    when *available_models* is None/empty (entitlement unknowable -- treat as
    transient rather than telling a user their plan lacks a model on no
    evidence).

    Both :func:`_format_acp_error` and :func:`_is_transient_raw_error` route
    through this single helper so the user-facing wording and the retry verdict
    cannot drift apart -- see the drift warning above.
    """
    # Two wordings name the rejected id: kiro-cli's "The model 'X' is not
    # available" and the MPS validation frame "Invalid model ID: X" (the shape
    # the background path's ``_rejected_model_from_error`` already accepts).
    # Both are judged against the same served list so the entitlement wording
    # and the retry verdict cannot depend on which frame the backend emitted.
    match = _RE_MODEL_UNAVAILABLE.search(data) or _RE_INVALID_MODEL_ID.search(data)
    if not match:
        return None
    rejected = match.group(1)
    if not available_models:
        # KAS advertises no served list, so its own token is the one way to tell:
        # the backend validated the id and refused it. The same sentence without
        # the token stays a capacity blip. With a served list the token does not
        # outrank it: the backend also answers it for a model that IS served but
        # whose backend config has not reached the region yet, which clears on
        # its own, so a list that names the model keeps the retry.
        return rejected if _RE_KAS_INVALID_MODEL_TYPE.search(data) else None
    # Route the served-list membership through the canonical helper: casing has
    # no meaning in these ids, and an entitled-but-differently-cased match must
    # not be reported as unentitled. The empty-list guard above preserves the
    # None-on-empty contract, so the helper's empty-means-usable answer is
    # never consulted here.
    if not model_is_unusable(rejected, available_models):
        return None
    return rejected


def _auto_remedy(available_models: Sequence[str] | None) -> str:
    """The "set agent.model to 'auto'" remediation step, or nothing when the
    partition does not serve ``auto``.

    The capacity-blip messages list three remedies, and the second is the
    ``auto`` sentinel. On a partition whose advertised list lacks ``auto`` that
    advice re-opens the circle the unentitled-``auto`` branch closes: the user
    follows it and the next turn dies on "no access to model 'auto'". So the
    step is emitted only when ``auto`` is served, or when the served list is
    unknown (nothing to check against, keep the historical advice). The
    numbering of the remaining step shifts so the list still reads (1)(2)(3)
    or (1)(2).
    """
    if model_is_unusable(DEFAULT_MODEL, available_models):
        return "or (2) "
    return f"(2) set agent.model to '{DEFAULT_MODEL}' in ~/.kiro/crew/config.json, or (3) "


def _rejected_model_from_error(error: object) -> str | None:
    """Return the model id a prompt-time error reports as invalid/unavailable.

    Powers the reactive fallback in ``run_bg_oneliner``: on the SUBSTITUTE
    (background) path, a rejected model is retried once against the account's
    advertised list. Matches both the MPS ``Invalid model ID: X``
    ValidationException (including the ``auto`` sentinel where a partition does
    not serve it) and the ``The model 'X' is not
    available`` wording. Returns None when the error names no specific model.
    """
    if not isinstance(error, dict):
        return None
    data = f"{error.get('data', '')} {error.get('message', '')}"
    m = _RE_INVALID_MODEL_ID.search(data) or _RE_MODEL_UNAVAILABLE.search(data)
    return m.group(1) if m else None
