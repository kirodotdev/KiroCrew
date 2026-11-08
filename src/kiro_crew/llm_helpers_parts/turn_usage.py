"""What a turn cost, which backend served it, and the per-interaction telemetry event.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.agent_sdk.drivers.acp_turn_vocab import LLMProvider, TurnUsage, resolve_billing_stats

logger = logging.getLogger("kiro_crew.llm_helpers")


# Runaway guard for the billing-stats wrapper walk, NOT a depth limit: a real
# provider stack ends long before it. It stops a source that synthesizes
# attributes (a ``MagicMock`` answers every ``getattr``). ``wrapper_chain``
# walks the same graph with the same guard.
_WRAPPER_WALK_MAX_NODES = 64

# Holder attributes the billing-stats walk follows, in order.
_BILLING_STAT_HOLDERS: tuple[str, ...] = ("_client", "_handle", "_sess", "provider")

# "No prior stats object was observed", distinct from a provider that exposes None.
_NO_PRIOR_STATS = object()

# Where stream_and_collect publishes the billing summed over ALL attempts of one
# turn. A retry installs fresh stats, so reading last_prompt_stats afterwards
# would drop an attempt billed before a transient error. The value is a
# (stats object, TurnUsage) pair: the provider outlives the turn, and the stats
# identity tells a later turn the sum is not its own.
_TURN_BILLED_ATTR = "_kc_turn_billed"


def record_interaction_event(client: LLMProvider, session_key: str, surface: str) -> None:
    """Record one per-interaction telemetry event via the PlatformContext seam.

    The payload is metadata only (session key, surface, model), never prompt,
    response or file text. Best-effort: a telemetry failure never affects the
    turn. Shared by every surface so the payload shape cannot drift.
    """
    from kiro_crew.platform import current_context

    try:
        # After Kiro startup the model is the inner client's ``model`` property;
        # before startup it is ``_model``. Try both, inner client first.
        inner = getattr(client, "_client", client)
        model = ""
        for obj in (inner, client):
            model = getattr(obj, "model", "") or getattr(obj, "_model", "") or ""
            if model:
                break
        current_context().telemetry.record_event(
            "interaction",
            {"session_key": session_key, "surface": surface, "model": model},
        )
    except Exception:
        logger.debug("telemetry.record_event(interaction) failed", exc_info=True)


def _billing_stats(provider: Any) -> Any:
    """The per-turn stats object behind *provider*, or ``None``.

    Returned as the object so callers can compare identity: the runner installs
    a FRESH stats object as a turn begins. Each holder is read through
    :func:`resolve_billing_stats`. A holder whose read fails is skipped, so a
    faulty wrapper cannot lose billing a later node still carries.
    """
    try:
        holders = _billing_stat_holders(provider)
    except Exception:
        logger.debug("billing stats holder walk failed", exc_info=True)
        return None
    for node in holders:
        try:
            stats = resolve_billing_stats(node)
        except Exception:
            logger.debug("billing stats read failed for one holder", exc_info=True)
            continue
        if stats is not None:
            return stats
    return None


def _attempt_usage(provider: Any, *, since: Any = _NO_PRIOR_STATS) -> TurnUsage:
    """Billing accrued on ONE attempt, read from the provider's live stats.

    ``since`` is the stats object seen before the attempt. A dispatch that fails
    before the runner installs fresh stats leaves the PREVIOUS turn's object in
    place; the identity check then reports nothing instead of billing it twice.
    """
    stats = _billing_stats(provider)
    if stats is None:
        return TurnUsage()
    if since is not _NO_PRIOR_STATS and stats is since:
        return TurnUsage()
    try:
        # Prefer the stats object's own converter: it carries every billing
        # dimension (claude: tokens, cache, cost_usd; kiro: credits). Without one,
        # or if it fails, fall back to credits so a billed turn is not zeroed.
        to_usage = getattr(stats, "to_turn_usage", None)
        if callable(to_usage):
            try:
                usage = to_usage()
            except Exception:
                logger.debug("to_turn_usage failed; falling back to credits", exc_info=True)
                usage = None
            if isinstance(usage, TurnUsage):
                return usage
        return TurnUsage(credits=float(getattr(stats, "credits", 0.0) or 0.0))
    except Exception:
        logger.debug("attempt usage read failed", exc_info=True)
    return TurnUsage()


def usage_has_billing(usage: TurnUsage) -> bool:
    """True when *usage* carries any billing dimension worth a row.

    The one predicate behind every persist gate, so a new billing dimension
    (like claude's ``cost_usd``) is not dropped by a stale copy.
    """
    return bool(
        usage.credits
        or usage.cost_usd
        or usage.input_tokens
        or usage.output_tokens
        or usage.cache_creation_tokens
        or usage.cache_read_tokens
    )


def _sum_usage(left: TurnUsage, right: TurnUsage) -> TurnUsage:
    """Add two attempts' billing. Never raises; unknown fields stay at zero."""
    try:
        return TurnUsage(
            credits=float(left.credits or 0.0) + float(right.credits or 0.0),
            input_tokens=int(getattr(left, "input_tokens", 0) or 0)
            + int(getattr(right, "input_tokens", 0) or 0),
            output_tokens=int(getattr(left, "output_tokens", 0) or 0)
            + int(getattr(right, "output_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(left, "cache_creation_tokens", 0) or 0)
            + int(getattr(right, "cache_creation_tokens", 0) or 0),
            cache_read_tokens=int(getattr(left, "cache_read_tokens", 0) or 0)
            + int(getattr(right, "cache_read_tokens", 0) or 0),
            cost_usd=float(getattr(left, "cost_usd", 0.0) or 0.0)
            + float(getattr(right, "cost_usd", 0.0) or 0.0),
            num_turns=int(getattr(left, "num_turns", 0) or 0)
            + int(getattr(right, "num_turns", 0) or 0),
            context_sum_tokens=int(getattr(left, "context_sum_tokens", 0) or 0)
            + int(getattr(right, "context_sum_tokens", 0) or 0),
        )
    except Exception:
        logger.debug("usage sum failed", exc_info=True)
        return left


def provider_last_turn_usage(
    provider: Any, *, since: Any = _NO_PRIOR_STATS, consume: bool = True
) -> TurnUsage:
    """Best-effort read of the just-completed turn's billing usage.

    ``stream_and_collect`` returns only text, so background surfaces (cron,
    heartbeat, autonudge, workflow, task-runner self-review) have no event for
    :func:`persist_token_record_async`. This rebuilds the turn's ``TurnUsage``.

    A turn can span several attempts, each with fresh stats, so the total
    ``stream_and_collect`` published is preferred and consumed here (reported
    once). ``consume=False`` reads it without clearing. Callers that drive
    ``provider.stream`` themselves publish no total and get the live read,
    guarded by ``since`` (see :func:`_attempt_usage`).

    The total is accepted only while its stats object is still the provider's
    current one. The provider outlives the turn, so a total left unread would
    otherwise bill one turn's spend to the next. Never raises; a provider with
    no stats yields an empty ``TurnUsage``.
    """
    try:
        accumulated = getattr(provider, _TURN_BILLED_ATTR, None)
        if isinstance(accumulated, tuple) and len(accumulated) == 2:
            published_stats, total = accumulated
            # Clear on every consuming read, match or not, so a stale total is
            # never seen by a third turn.
            if consume:
                try:
                    delattr(provider, _TURN_BILLED_ATTR)
                except Exception:
                    logger.debug("clearing accumulated turn usage failed", exc_info=True)
            if isinstance(total, TurnUsage) and published_stats is _billing_stats(provider):
                return total
    except Exception:
        logger.debug("accumulated turn usage read failed", exc_info=True)
    return _attempt_usage(provider, since=since)


def _billing_stat_holders(provider: Any) -> "list[Any]":
    """Objects that may carry ``last_prompt_stats``, nearest wrapper first.

    Each seam keeps the turn-runner behind a different attribute (``_client``,
    ``_handle``, ``_sess.provider``), so all are walked; otherwise a claude_code
    or bedrock background turn reports 0 credits. The walk is depth-first and
    identity-deduped: a self-referential chain cannot loop, and the stats at the
    bottom of a deep stack are reached before the node budget runs out.

    A seam missing from :data:`_BILLING_STAT_HOLDERS` goes unbilled, which is why
    the billing read prefers :meth:`LLMProvider.billing_stats`.
    """
    out: list[Any] = []
    seen: set[int] = set()
    # A stack: push children in reverse so they come off in holder order.
    frontier: list[Any] = [provider]
    while frontier and len(out) < _WRAPPER_WALK_MAX_NODES:
        node = frontier.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        out.append(node)
        for attr in reversed(_BILLING_STAT_HOLDERS):
            frontier.append(getattr(node, attr, None))
    return out


def _provider_label(provider: Any) -> str:
    """Backend label for the usage row's ``provider`` dimension. Never raises.

    An empty label drops the row out of the usage page's provider breakdowns.
    ``provider_label`` answers from the backend that served the turn; reading
    ``config.agent.provider`` would always say ``acp``. The wrapper chain is
    walked because a wrapped provider is not recognised directly; the first
    non-default label wins, else the default.
    """
    try:
        from kiro_crew.agent_sdk.drivers.acp import provider_label
        from kiro_crew.agent_sdk.drivers.acp_turn_vocab import PROVIDER_LABEL_DEFAULT

        for node in _billing_stat_holders(provider):
            label = provider_label(node)
            if label and label != PROVIDER_LABEL_DEFAULT:
                return label
        return PROVIDER_LABEL_DEFAULT
    except Exception:
        logger.debug("resolving provider label failed", exc_info=True)
        return ""
