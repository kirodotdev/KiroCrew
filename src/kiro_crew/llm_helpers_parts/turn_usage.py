"""What a turn cost, which backend served it, and the per-interaction telemetry event.

The billing-stats walk along a provider's wrapper chain, a turn's ``TurnUsage`` summed
across its attempts, the backend label a usage row carries, and the one
``interaction`` telemetry event every surface records per successful turn.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.agent_sdk.drivers.acp_turn_vocab import LLMProvider, TurnUsage, resolve_billing_stats

logger = logging.getLogger("kiro_crew.llm_helpers")


# Runaway guard for the wrapper chain walked to find a turn's billing stats,
# NOT a depth limit. The walk is depth-first along the documented holders and
# identity-deduped, so a real provider stack is exhausted long before this;
# the bound exists for a source that synthesizes attributes (a ``MagicMock``
# answers every ``getattr`` with a fresh child). Sized so no plausible wrapper
# depth reaches it: session sharing, a channel link, a subagent companion and
# a fallback wrapper each add a layer, and the same object graph is the one
# ``dashboard.handlers.usage._wrapper_chain`` walks for the model with the
# same guard.
_WRAPPER_WALK_MAX_NODES = 64

# Holder attributes the billing-stats walk follows, in order.
_BILLING_STAT_HOLDERS: tuple[str, ...] = ("_client", "_handle", "_sess", "provider")

# Sentinel for "no prior stats object was observed", distinct from a provider that
# legitimately exposes None. Used by provider_last_turn_usage's identity guard.
_NO_PRIOR_STATS = object()

# Where stream_and_collect hands its caller the billing it observed across ALL
# attempts of one logical turn. A retry installs fresh per-turn stats, so a
# post-hoc read of last_prompt_stats sees only the final attempt and silently
# drops an attempt that was billed before a transient error. This carries the sum
# instead, and provider_last_turn_usage consumes it. The value is a
# (stats object, TurnUsage) pair: the provider outlives the turn, so the identity
# of the stats the sum was computed from is what tells a later turn that the sum
# is not its own.
_TURN_BILLED_ATTR = "_kc_turn_billed"


def record_interaction_event(client: LLMProvider, session_key: str, surface: str) -> None:
    """Record one per-interaction telemetry event via the PlatformContext seam.

    The Default ``TelemetryProvider.record_event`` is a no-op, so standalone is
    unchanged; a companion records one event per successful turn. Payload is
    strictly metadata (session key, surface, model) — never prompt/response text
    or file contents. Best-effort: a telemetry failure never affects the turn.

    Shared by every surface (dashboard, Slack) so the payload shape and the
    model-extraction reflection cannot drift between call sites.
    """
    from kiro_crew.platform import current_context

    try:
        # Resolve the active model across backend shapes. After Kiro startup the
        # provider's ``_client`` is an ``AcpSessionProvider`` that exposes the
        # model via a ``model`` property (backed by ``_handle.model``); before
        # startup / for the raw client it is the ``_model`` attribute. Try the
        # property first, then the raw attr, on the inner client then the outer.
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

    Returned as the object rather than a value so callers can compare identity:
    the runner installs a FRESH stats object as it begins a turn, which is what
    tells a completed turn apart from one that never started.

    Each holder is read through :func:`resolve_billing_stats`, the one spelling
    the wrappers use too: a provider's declared
    :meth:`LLMProvider.billing_stats` wins, and the ``last_prompt_stats``
    fallback keeps every holder that was found before the capability existed --
    the raw ``AcpClient``, and the doubles that stand in for a runner. One
    holder's broken read is skipped rather than abandoning the walk, so a faulty
    wrapper cannot lose a turn whose billing a later node still carries.
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

    ``since`` is the stats object observed before the attempt. The runner installs
    a fresh stats object as it begins a turn, with the credit counters at zero; a
    dispatch that fails BEFORE that point -- a busy session, a dead runtime --
    leaves the PREVIOUS turn's object in place, still carrying credits that were
    already recorded. Comparing identity reports nothing in that case instead of
    billing the earlier turn a second time.
    """
    stats = _billing_stats(provider)
    if stats is None:
        return TurnUsage()
    if since is not _NO_PRIOR_STATS and stats is since:
        return TurnUsage()
    try:
        # Prefer the stats object's own converter: it is the single source of
        # truth for stats -> TurnUsage and carries every billing dimension the
        # turn filled (claude seam: token counts + cache fields + cost_usd; kiro:
        # credits). Duck-typed so the doubles in tests (and any stats holder
        # predating the converter) fall through to the credits-only constructor,
        # which is byte-identical for the kiro seam. The converter's failure is
        # contained so a faulty to_turn_usage degrades to the credits read
        # rather than silently zeroing a turn that did bill.
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

    The single predicate behind every persist gate. Three hand-maintained
    copies of ``credits or input_tokens or output_tokens`` is how the claude
    seam's ``cost_usd`` (and a cost-free cache-only turn) get dropped; a gate
    that reads this cannot drift from its siblings when the next billing
    dimension is added.
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

    ``stream_and_collect`` breaks on ``EVENT_COMPLETE`` and returns only text,
    discarding the event's ``usage``. Background surfaces that dispatch through
    it (cron, heartbeat, autonudge, workflow, task-runner self-review) therefore
    have no event to hand :func:`persist_token_record_async`. This recovers the
    turn's billing and wraps it in a ``TurnUsage`` so it can be passed straight
    through as the ``event`` argument.

    A turn can span several attempts: ``stream_and_collect`` retries a busy
    session and a transient backend error, and each retry installs fresh per-turn
    stats. Reading the provider's live stats afterwards therefore sees only the
    LAST attempt and loses any earlier attempt that was already billed. So the
    total ``stream_and_collect`` accumulated is preferred when present, and
    consumed here -- one turn's billing is reported once. Callers that drive
    ``provider.stream`` themselves publish no total and fall back to the live
    read, which is what ``since`` guards (see :func:`_attempt_usage`).
    ``consume=False`` reads the total without clearing it, for a caller that
    inspects a turn whose recording reader comes after it.

    The total is published WITH the stats object it was computed from, and is
    accepted only while that object is still the provider's current one. The
    provider outlives the turn -- the shared background session is reused by every
    background caller, and a Slack session by every turn in its thread -- so a
    total left unread by one turn would otherwise be consumed by the next one,
    billing that turn's spend to the wrong surface and losing its own. Today every
    reader happens to be paired with a publish in the same turn, which makes the
    residue unreachable; the guard is here so that safety does not depend on the
    next caller preserving that pairing. A fresh turn installs fresh stats, so a
    stale total simply fails the identity check and the live read takes over.

    On the kiro (acp) seam the only non-zero per-turn billing signal is
    ``credits``; on the claude seam the token counts, cache fields, and
    ``cost_usd`` are filled instead. Whichever dimensions the turn's stats
    carried come through :meth:`to_turn_usage` intact. Providers that expose
    no stats (non-ACP backends, test doubles) yield an empty ``TurnUsage``
    (credits=0). Never raises.
    """
    try:
        accumulated = getattr(provider, _TURN_BILLED_ATTR, None)
        if isinstance(accumulated, tuple) and len(accumulated) == 2:
            published_stats, total = accumulated
            # Clear on every consuming read, match or not: a total that failed the
            # identity check belongs to a turn that is already over and must not be
            # seen again by a third one.
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

    The compatibility path behind :func:`_billing_stats`, and the lookup
    :func:`_provider_label` still needs: the turn-runner sits behind a different
    attribute per seam: the acp provider keeps it on ``_client``, the session
    provider on ``_handle``, and the shared background session hands non-kiro
    callers a thin adapter whose only link to the runner is ``_sess.provider``.
    Walking all of them keeps a background turn on the claude_code / bedrock seam
    from reporting 0 credits for a turn that was billed. Depth-first along
    :data:`_BILLING_STAT_HOLDERS` and identity-deduped, so a self-referential
    wrapper chain cannot loop and a stack of wrapper layers cannot exhaust the
    node budget on holder-less siblings before the runner is reached — the
    stats sit at the bottom of the chain, and a breadth-first walk with a node
    budget stops short of them a few layers down. :data:`_WRAPPER_WALK_MAX_NODES`
    is a runaway guard for attribute-synthesizing sources, not a depth limit.

    A name absent from this tuple is exactly how a new seam's spend went
    unreported, which is why the billing read prefers the provider's declared
    :meth:`LLMProvider.billing_stats` and only falls back here.
    """
    out: list[Any] = []
    seen: set[int] = set()
    # A stack: the LAST entry is visited next, so children are pushed in
    # reverse holder order to come off in holder order.
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

    Left unset the row lands with ``provider=""`` and drops out of the usage
    page's provider and provider-model breakdowns, so a background turn's spend
    would be recorded yet unattributable to a backend.

    ``provider_label`` is the shared resolver for this vocabulary -- the same
    ``acp`` / ``claude_code`` / ``kas`` keys the session map and resume-compat
    check use -- and it answers from the backend that actually served the turn,
    which is the precedence ``kiro_crew.llm_helpers`` already applies to the model and the
    agent. Reading ``config.agent.provider`` instead would report a declaration:
    that field's enum admits only ``acp``, so a claude_code-backed session would
    be labelled ``acp``.

    The resolver only recognises a provider it is handed directly, and the shared
    background session wraps one behind ``_sess.provider``, so the wrapper chain
    is walked and the first node that names a non-default backend wins. Falling
    back to the default label rather than to ``""`` matches every other writer of
    this field.
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
