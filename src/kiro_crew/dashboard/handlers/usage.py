"""Kiro usage handlers — local session analytics + kiro-cli billing."""

# This module is the usage handlers' facade: routes, callers across the gateway and tests
# import and patch every usage name through it, as
# ``kiro_crew.dashboard.handlers.usage.<name>``. A read of a name an owner holds is served
# from that owner, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# module holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. It
# keeps the usage routes, the model a turn is recorded under, and the token-record writer
# (the one module of the family that imports ``TurnUsage``). New work goes to the owner of
# its responsibility:
#
#   ``usage_store`` -- the shard store, the reporting window, slot categories, row readers
#   ``usage_spend`` -- a slot's spend, the per-day credits, one slot's per-turn usage
#   ``usage_context`` -- context-window occupancy and a session's context trace
#   ``usage_cost`` -- the Telemetry panel's spend breakdown
#   ``usage_history`` -- the Usage page's per-day token history
#   ``usage_sessions`` -- the Usage page's kiro-cli session activity and its cached parse

from __future__ import annotations

import asyncio
import getpass
import importlib as _importlib
import json
import logging
import math
import sys as _sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard.handlers.usage_sessions import _CACHE_TTL, _usage_sessions_snapshot
from kiro_crew.dashboard.handlers.usage_store import (  # noqa: F401 - usage_series reads these per row
    _coerce_int,
    _parse_row_day,
    _parse_row_ts,
    _shard_path_for,
    _shards_in_window,
    _usage_number,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.metrics.turns import (
    OUTCOME_UNCLASSIFIED,
    emit_turn_duration,
    emit_turn_usage,
    turn_outcome,
)
from kiro_crew.owner_only_files import owner_only_opener_for

logger = logging.getLogger(__name__)


_CACHE: dict[str, Any] = {}
_CACHE_TS: float = 0.0
_CACHE_LOCK = LoopBoundLock()


def read_context_tokens(source: object) -> tuple[int, int]:
    """Return ``(context_used, context_window)`` from a provider/client.

    Reads the provider's public ``context_used_tokens()`` /
    ``context_window_tokens()`` accessors (declared on ``providers/base.py`` and
    implemented for ACP in ``providers/acp.py`` and ``acp/session_provider.py``).
    Guarded with ``getattr`` so non-ACP providers and test doubles that lack the
    accessors — or an accessor that raises — yield ``(0, 0)`` rather than
    propagating. Never raises: this is best-effort analytics on the turn hot
    path, and a measurement helper must never break the turn it measures.
    """
    try:
        used_fn = getattr(source, "context_used_tokens", None)
        window_fn = getattr(source, "context_window_tokens", None)
        if not callable(used_fn) or not callable(window_fn):
            return (0, 0)
        return (int(used_fn()), int(window_fn()))
    except Exception:
        return (0, 0)


# Runaway guard for :func:`_wrapper_chain`, NOT a depth limit. The walk follows
# the four documented holder attributes and stops at objects it has already
# seen, so a real provider stack — a handful of wrappers around one handle and
# one runtime — is exhausted long before this. What the bound exists for is a
# source that SYNTHESIZES attributes: a ``MagicMock`` answers every ``getattr``
# with a fresh child, so without a bound the walk would never end. It is sized
# so that no plausible wrapper depth can reach it: a wrapper layer carrying all
# four holders costs three nodes on the way down, so this admits twenty-plus
# stacked layers, an order of magnitude past anything a session accumulates.
_WRAPPER_CHAIN_MAX_NODES = 64

# Holder attributes, in the order they are followed. ``_runtime`` is LAST on
# purpose — see :func:`_wrapper_chain`.
_WRAPPER_HOLDERS: tuple[str, ...] = ("client", "_client", "_handle", "_runtime")


def _wrapper_chain(source: object) -> list[object]:
    """Collect *source* and the provider/client/handle wrappers nested under it.

    ``providers/acp.py`` keeps its inner object on ``_client`` (also exposed as
    ``client``), ``acp/session_provider.py`` keeps the session handle on
    ``_handle``, and both the handle and the provider keep the spawned CLI
    runtime on ``_runtime`` (``session_handle_class.py`` ``__init__``, ``session_provider.py:61``)
    — and these nest, because ``providers/acp_runtime_start.py`` assigns an
    ``AcpSessionProvider`` to ``_client`` (the ``-> AcpClient`` annotation there
    carries a ``type: ignore``). A default Kiro turn therefore hides its resolved
    state two levels down, at ``provider.client._handle``, so probing a fixed
    depth misses it.

    Depth-first along the holder attributes, with an identity-based visited set
    so a wrapper that points back at itself (or two wrappers that share a
    runtime) terminates. Depth-first, not breadth-first, because the model state
    sits at the BOTTOM of the holder chain while every wrapper layer above it —
    a fallback wrapper, session sharing, a subagent companion, a channel-linked
    session — carries siblings that hold nothing. A breadth-first walk with a
    node budget spends that budget on the siblings and, a few layers down,
    stops short of the ``_model`` node; ``_resolve_model`` then persists a blank
    and the credits land in the read-time ``unknown`` bucket. Following the
    chain downward first reaches the handle regardless of how many layers are
    stacked on top; :data:`_WRAPPER_CHAIN_MAX_NODES` is a runaway guard for
    attribute-synthesizing sources, not a depth limit.

    ``_runtime`` is traversed **last** deliberately. It is the only holder of
    ``_agent`` for the session-provider shape (``AcpRuntime._agent``), but it also
    carries the process-level ``--model`` argument (``AcpRuntime._model``); visiting
    it after the whole ``_handle`` subtree keeps session-level model state ahead
    of process-level state when :func:`read_effective_model` falls through to
    ``_model``.
    """
    chain: list[object] = []
    # A stack: the LAST entry is visited next, so children are pushed in
    # reverse holder order to come off in holder order.
    pending: list[object] = [source]
    while pending and len(chain) < _WRAPPER_CHAIN_MAX_NODES:
        node = pending.pop()
        if node is None or any(seen is node for seen in chain):
            continue
        chain.append(node)
        for holder in reversed(_WRAPPER_HOLDERS):
            inner = getattr(node, holder, None)
            if inner is not None and not isinstance(inner, (str, bytes, int)):
                pending.append(inner)
    return chain


def read_effective_agent(source: object) -> str:
    """Return the agent id that actually served the turn, or ``""``.

    The slot's ``agent`` is an alias that ``resolve_agent_bindings()`` maps to a
    concrete kiro agent before dispatch (``dashboard/chat_turn/turn_admission.py``'s
    ``_turn_admit`` resolves it and passes the result into ``get_or_create``), so a slot
    set to ``default`` can be served by ``kirocrew``. Recording the alias would attribute the turn
    to an agent that never ran. ``AcpClient`` stores what it was constructed with
    on ``_agent`` (``acp/client_class.py``, ``__init__``), which is that resolved value — the
    same "what actually ran" precedence used for the model. Never raises.
    """
    try:
        for node in _wrapper_chain(source):
            candidate = getattr(node, "_agent", "")
            if isinstance(candidate, str) and candidate:
                return candidate
    except Exception:
        pass
    return ""


def read_effective_model(source: object) -> str:
    """Return the model id the provider actually resolved for the turn, or ``""``.

    Attribution only — deliberately NOT the same as
    ``chat_runner._backfill_canonical_model``. That helper canonicalizes and
    DROPS Bedrock profile-form ids for non-``claude_code`` providers, because its
    result is written back into ``slot.model`` and a profile id there pins the
    slot to one profile+region across resumes. Nothing here is written back, so
    the raw resolved id is what we want: for cost attribution, the profile that
    actually served the turn is strictly more informative than the alias the
    caller asked for.

    Walks the wrapper chain via :func:`_wrapper_chain` rather than a fixed depth,
    because a default Kiro turn hides the resolved id two levels down.

    Two passes over the collected chain, because a resolved id **anywhere** beats
    a plain ``_model`` anywhere: ``_resolved_model_id`` is the id the backend
    actually settled on (the same precedence ``AcpClient`` uses internally,
    ``self._resolved_model_id or self._model``), while ``_model`` may still hold
    the ``"auto"`` sentinel or a pre-resolution request. Skips ``"auto"``. Never
    raises.
    """
    try:
        chain = _wrapper_chain(source)
        for attr in ("_resolved_model_id", "_model"):
            for node in chain:
                candidate = getattr(node, attr, "")
                if isinstance(candidate, str) and candidate and candidate.strip().lower() != "auto":
                    return candidate
    except Exception:
        pass
    return ""


def _source_requests_auto(source: object) -> bool:
    """Whether the provider chain reports Auto, as a request or from the backend.

    This is deliberately separate from :func:`read_effective_model`: ``auto``
    is not a resolved model id, but it is useful attribution when a completed
    turn has no more specific id from the backend. Two signals count: a
    ``_model`` request of ``auto``, and a ``_resolved_model_id`` of ``auto``.
    The second is what a session on the default model holds: the start path
    never calls ``set_model`` for it, so ``_model`` stays ``""``, while
    ``session/new`` stores the backend's ``currentModelId`` (``auto``) as the
    resolved id. That is the backend saying Auto served the session. A blank
    ``_model`` with a blank or missing ``_resolved_model_id`` proves nothing
    was selected, so it remains blank in the row store.
    """
    try:
        return any(
            isinstance(candidate := getattr(node, attr, ""), str)
            and candidate.strip().lower() == "auto"
            for node in _wrapper_chain(source)
            for attr in ("_model", "_resolved_model_id")
        )
    except Exception:
        return False


def read_turn_model(source: object) -> str:
    """Return what served the turn: a resolved id, ``"auto"``, or ``""``.

    :func:`read_effective_model` alone collapses two different situations into
    ``""``: a turn whose model the provider never reported, and a turn the user
    deliberately handed to Auto. They read identically to a consumer, so a
    display surface that omits a blank model shows nothing for an Auto turn and
    an absent model becomes indistinguishable from a missing measurement.

    ``"auto"`` is not a model id and is never presented as one — it is the
    honest answer to "who chose", which is all the backend discloses when
    Auto routes a turn (the ACP per-turn metadata frame carries context and
    metering only). Callers that need a concrete id for a pricing or window
    lookup must keep using :func:`read_effective_model`. ``"auto"`` comes back
    when the chain requests Auto or the backend reported ``auto`` as the
    resolved id (a session left on the default model); a chain with neither
    stays ``""``. Never raises.
    """
    resolved = read_effective_model(source)
    if resolved:
        return resolved
    return "auto" if _source_requests_auto(source) else ""


def _resolve_model(model: str, model_source: object) -> str:
    """Resolve the model to record, retaining a known Auto selection.

    A concrete resolved id remains the preferred accounting dimension. When a
    completed Auto turn exposes no concrete id, ``"auto"`` still distinguishes
    that deliberate backend choice from an unavailable model source. A blank
    caller value with no Auto request remains blank because it carries no model
    information at all.
    """
    requested = (model or "").strip().lower()
    if requested not in ("", "auto"):
        return model
    resolved = read_turn_model(model_source) if model_source is not None else ""
    if resolved:
        return resolved
    # An explicit Auto request stands on its own: the caller named it, so it is
    # recorded even when the provider chain no longer reports it.
    return "auto" if requested == "auto" else ""


def _build_token_record(
    slot_key: str,
    model: str,
    event: object,
    provider: str,
    now: datetime,
    *,
    surface: str = "",
    agent: str = "",
    context_used: int = 0,
    context_window: int = 0,
    elapsed_ms: int = 0,
    app: str = "",
) -> dict[str, Any]:
    """Build the JSONL token-usage record dict (no I/O).

    ``app`` stamps the OWNING app on the row when the turn ran on an app-owned
    slot. Ownership recorded at write time is what survives the slot: a live
    ``_app`` check at read time both leaks on slot-name reuse (a recreated slot
    would vouch for the previous owner's rows) and denies an app its own
    completed sessions. Empty for dashboard/user slots, and absent from rows
    written before the field existed — an app caller sees neither, which is
    the deny-by-default answer for ownership that was never recorded.

    ``surface`` tags the dispatch origin (``dashboard``, ``cron``, ``subagent``,
    …) and ``agent`` the agent id resolved for the turn. ``context_used`` /
    ``context_window`` record context-window occupancy (read from the provider
    via :func:`read_context_tokens` at the call site). All four are additive and
    default to empty/0, so pre-existing shards and existing callers stay valid.

    ``elapsed_ms`` is the caller's locally measured wall clock for the turn and
    is the FALLBACK for ``duration_ms``: the provider-reported value wins when
    non-zero, otherwise the local measurement is recorded. Both are needed
    because the acp provider always reports ``TurnUsage.duration_ms == 0``
    (nothing assigns it), so a provider-only read would record a literal 0 for
    every real turn and leave the row store unable to answer "how long did this
    turn take".

    This mirrors the precedence the OTEL emit path already uses
    (``chat_runner._attach_turn_stats``: ``value = duration_ms or elapsed_ms``),
    so the histogram and the row store cannot disagree about one turn.
    """
    # Usage lives on event.usage (TurnUsage). Fall back to the event itself when
    # it isn't a real TurnUsage (legacy / non-AcpEvent producers, test doubles).
    # credits is float-coerced so a non-numeric value can't break json.dumps;
    # non-finite floats are caught once for every field in _write_token_record.
    _u = getattr(event, "usage", None)
    u = _u if isinstance(_u, TurnUsage) else event
    try:
        credits = float(getattr(u, "credits", 0.0))
    except (TypeError, ValueError):
        credits = 0.0
    # Turn stop reason, read off the EVENT (AcpEvent carries it; a bare
    # TurnUsage from provider_last_turn_usage does not — recorded as "").
    # Free-form per-row (this store has no cardinality limit, unlike OTel
    # attrs), so watchdog outcomes (STOP_REASON_TOOL_STALL / _STALE_RECOVER)
    # can be joined against the row's ``agent`` field retroactively — this is
    # where per-agent stall analysis happens, deliberately NOT on metric attrs.
    _stop = getattr(event, "stop_reason", "")
    return {
        "_type": "tokens",
        "ts": now.isoformat(),
        "slot": slot_key,
        "app": app,
        "provider": provider or "",
        "model": model or "",
        "input": getattr(u, "input_tokens", 0),
        "output": getattr(u, "output_tokens", 0),
        "cache_create": getattr(u, "cache_creation_tokens", 0),
        "cache_read": getattr(u, "cache_read_tokens", 0),
        "cost": getattr(u, "cost_usd", 0.0),
        "credits": credits,
        "turns": getattr(u, "num_turns", 0),
        "duration_ms": getattr(u, "duration_ms", 0) or _coerce_int(elapsed_ms),
        # Additive per-turn fields: context occupancy + dispatch origin. Old
        # shards lack these keys; readers must tolerate their absence.
        # context_* are int-coerced so a bad value can't break json.dumps.
        "surface": surface or "",
        "agent": agent or "",
        "context_used": _coerce_int(context_used),
        "context_window": _coerce_int(context_window),
        # Additive: the context occupancy after each of the turn's model calls,
        # added up (kiro-cli reports it per call; ``turns`` above counts the
        # calls). Read off the turn's usage, so no caller passes it.
        "context_sum": _coerce_int(getattr(u, "context_sum_tokens", 0)),
        # Additive: the turn's terminal stop reason ("" when the producer has
        # none). str-coerced so a non-string on a test double / legacy event
        # can't break json.dumps.
        "stop_reason": _stop if isinstance(_stop, str) else "",
    }


def _finite_only(record: dict[str, Any]) -> dict[str, Any]:
    """Replace non-finite floats with 0.0 so the row stays valid JSON.

    ``json.dumps`` writes NaN and the infinities as the bare tokens ``NaN`` /
    ``Infinity`` / ``-Infinity``. Those are not valid JSON, but ``json.loads``
    accepts them, so a single such row travels silently from the store into a
    ``web.json_response`` body that no browser will parse — taking every panel
    reading the row store down rather than losing one turn's numbers. Provider
    floats (``cost_usd``, ``credits``) reach the record unvalidated, and a float
    is exactly what a bad one looks like, so the check cannot be a type check.
    """
    return {
        k: (0.0 if isinstance(v, float) and not math.isfinite(v) else v) for k, v in record.items()
    }


# The record fields that make a row billed: the same six dimensions
# ``llm_helpers.usage_has_billing`` reads off a ``TurnUsage``, by their record
# names. A claude-seam turn bills in tokens with ``cost`` still 0 (cost is a
# session-cumulative delta that lands only when a cost frame arrives) and
# credits stay 0 off the kiro path, so a credits-and-cost check alone would
# stay silent for exactly the token-billed rows.
_BILLED_RECORD_FIELDS: tuple[str, ...] = (
    "credits",
    "cost",
    "input",
    "output",
    "cache_create",
    "cache_read",
)


def _record_is_billed(record: dict[str, Any]) -> bool:
    """Whether a BUILT record carries any billing dimension, non-finite aside.

    Mirrors ``llm_helpers.usage_has_billing`` over the record's own fields
    (:data:`_BILLED_RECORD_FIELDS`), so the write site judges the row it is
    about to persist rather than the event it was built from. A non-finite
    value is a corrupt measurement, handled by :func:`_write_token_record`'s
    own sanitizer, not a charge.
    """
    for key in _BILLED_RECORD_FIELDS:
        value = record.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value != 0:
            return True
    return False


def _warn_unattributed_billing(record: dict[str, Any]) -> None:
    """Log — loudly — a billed row about to be persisted with no model.

    Read time renders a blank ``model`` as ``unknown`` (``cost_breakdown``).
    Left as the only signal, that bucket is where an attribution failure is
    discovered weeks later, once it is large enough to notice, not where and
    when it happens. The row is still persisted — the charge is real and
    dropping it would understate spend — and the failure goes on record at the
    moment of writing, naming the slot, surface and provider so the dispatch
    path that lost the model can be found, and the billed dimensions so the
    size of the loss is on record too. Nothing else: the row's other fields
    are per-turn telemetry and do not belong in a log line.
    """
    if str(record.get("model") or "") or not _record_is_billed(record):
        return
    logger.warning(
        "usage row: model attribution failed for a billed turn; persisting with a blank "
        "model (read time renders it as 'unknown'). slot=%s surface=%s provider=%s "
        "credits=%s cost=%s tokens=%s",
        record.get("slot", ""),
        record.get("surface", ""),
        record.get("provider", ""),
        record.get("credits", 0),
        record.get("cost", 0),
        {k: record.get(k, 0) for k in ("input", "output", "cache_create", "cache_read")},
    )


def _write_token_record(record: dict[str, Any], now: datetime) -> None:
    """Append a prebuilt token record to today's shard (blocking I/O)."""
    _warn_unattributed_billing(record)
    shard_path = _shard_path_for(now)
    parent = shard_path.parent
    # mkdir only when missing — the dir is created once per day, not per turn.
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # allow_nan=False turns a non-finite value into a raised ValueError instead
    # of an invalid-JSON row. Sanitizing only on that failure keeps the hot path
    # free of a per-turn scan of every field.
    try:
        line = json.dumps(record, allow_nan=False)
    except ValueError:
        # Name the fields before sanitizing. Without this the fallback rewrites
        # a provider's measurement to 0.0 and the turn is persisted as free,
        # with nothing on record that it happened. Field NAMES only: the values
        # are the corrupt measurement and the rest of the record is per-turn
        # telemetry, neither of which belongs in a log line.
        #
        # Re-raise when nothing non-finite is present: allow_nan=False is not
        # the only way json.dumps raises ValueError, and sanitizing cannot fix
        # the others. Swallowing them here would emit a warning naming no field
        # and blame corruption that did not occur.
        non_finite = sorted(
            k for k, v in record.items() if isinstance(v, float) and not math.isfinite(v)
        )
        if not non_finite:
            raise
        logger.warning(
            "usage row: replaced non-finite value(s) with 0.0 before persisting; " "fields=%s",
            ",".join(non_finite),
        )
        line = json.dumps(_finite_only(record), allow_nan=False)
    with open(shard_path, "a", encoding="utf-8", opener=owner_only_opener_for(shard_path)) as f:
        f.write(line + "\n")


def persist_token_record(
    slot_key: str,
    model: str,
    event: object,
    provider: str = "",
    *,
    surface: str = "",
    agent: str = "",
    context_used: int = 0,
    context_window: int = 0,
    elapsed_ms: int = 0,
    app: str = "",
    model_source: object = None,
) -> None:
    """Append a token usage record to today's shard under
    ``<data home>/usage/tokens/YYYY-MM-DD.jsonl`` (synchronous).

    Has NO caller today, and unlike :func:`persist_token_record_async` it does
    not emit ``kirocrew.turn.duration``. A future direct caller of this variant
    would therefore write a usage row for a turn that appears in no latency or
    fault-rate reading — silently reopening the gap the async variant's emit
    closed. If you reach for this for a real per-turn path, emit there too (or
    call the async variant).

    The ``provider`` field tags the source LLM backend (acp,
    claude_code, bedrock) so the dashboard chart can filter by provider.
    ``surface`` / ``agent`` tag the dispatch origin and resolved agent, and
    ``context_used`` / ``context_window`` record context-window occupancy — all
    additive and defaulted so existing callers stay valid.

    ``elapsed_ms`` is the caller's locally measured turn wall clock, used only
    when the provider reports no duration. Every dispatch surface owns its own
    measurement because there is no global turn boundary to hang one clock on.

    ``model_source`` is a provider/client used ONLY to fill ``model`` when the
    caller could not resolve one (several dispatch surfaces never pick a model
    explicitly and would otherwise record an empty string, losing the
    per-model attribution dimension). An explicit ``model`` always wins.

    On the async chat hot path, prefer ``persist_token_record_async`` which
    offloads the blocking write off the event loop.
    """
    try:
        model = _resolve_model(model, model_source)
        now = datetime.now().astimezone()
        _write_token_record(
            _build_token_record(
                slot_key,
                model,
                event,
                provider,
                now,
                surface=surface,
                agent=agent,
                context_used=context_used,
                context_window=context_window,
                elapsed_ms=elapsed_ms,
                app=app,
            ),
            now,
        )
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


async def persist_token_record_async(
    slot_key: str,
    model: str,
    event: object,
    provider: str = "",
    *,
    surface: str = "",
    agent: str = "",
    context_used: int = 0,
    context_window: int = 0,
    elapsed_ms: int = 0,
    app: str = "",
    model_source: object = None,
    emit_metric: bool = True,
) -> None:
    """Async variant: builds the record on-loop, offloads the file write.

    Called per agent turn (EVENT_COMPLETE) from the chat runner, which runs on
    the aiohttp event loop, where a synchronous open/append would block all
    co-resident coroutines for the IO window. These are best-effort analytics
    (no fsync, exceptions swallowed), so off-loop write loses no durability.
    See :func:`persist_token_record` for the ``surface`` / ``agent`` /
    ``context_used`` / ``context_window`` / ``elapsed_ms`` / ``model_source``
    fields.

    ALSO emits the per-turn OTEL family — ``kirocrew.turn.duration`` plus the
    turn's usage (``kirocrew.turn.tokens`` and whichever of
    ``kirocrew.turn.credits`` / ``kirocrew.turn.cost_usd`` the backend billed in)
    — and this is the only place that does. Being the one call every dispatch
    surface already makes once per turn is exactly why: an emit sited in
    ``chat_runner`` beside the dashboard turn loop leaves cron, heartbeat, memory
    consolidation, subagents, task-runner steps, workflow stages and every
    messaging channel absent from turn latency and fault rate entirely — and
    absent does not read as absent, it reads as healthy. See
    :mod:`kiro_crew.metrics.turns`.

    Every sample reuses the BUILT record's own fields — ``duration_ms``, the
    token counts, ``credits``/``cost``, and the RESOLVED ``model``/``provider`` —
    so the row store and the metrics cannot disagree about one turn, the property
    the record builder's docstring already claims and now enforces by
    construction rather than by two call sites being handed the same variables.

    ``emit_metric=False`` says the CALLER owns this turn's samples. The
    dashboard passes it because its persist call sits behind a
    ``usage_has_billing`` gate: a turn that timed out having billed nothing
    writes no row, and letting the row's absence swallow the samples would drop
    exactly the faults ``fault_rate`` exists to count. It emits unconditionally
    itself instead, which is also how its ``stall_exhausted`` refinement reaches
    the histogram. Do NOT set this without emitting — the turn then goes
    unrecorded, which reads as a healthy gap rather than a missing one.
    """
    try:
        model = _resolve_model(model, model_source)
        now = datetime.now().astimezone()
        record = _build_token_record(
            slot_key,
            model,
            event,
            provider,
            now,
            surface=surface,
            agent=agent,
            context_used=context_used,
            context_window=context_window,
            elapsed_ms=elapsed_ms,
            app=app,
        )
        # Before the offloaded write: a file-write failure must not cost the
        # latency sample, which needs nothing from disk.
        if emit_metric:
            _emit_turn_histogram(record, slot_key, event)
        await asyncio.to_thread(_write_token_record, record, now)
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


def _emit_turn_histogram(
    record: dict,
    slot_key: str,
    event: object,
) -> None:
    """Emit this turn's OTEL samples from a built record.

    ``kirocrew.turn.duration`` plus the turn's usage family
    (``kirocrew.turn.tokens`` and whichever of ``kirocrew.turn.credits`` /
    ``kirocrew.turn.cost_usd`` the backend billed in). Split out so the emit is
    one statement in the persist path and can be asserted directly by a test
    without writing a row.

    Reads every value off the RECORD rather than recomputing from
    ``event``/``elapsed_ms``: the builder already applies the
    provider-reported-wins-else-wall-clock rule for the duration and already
    coerces the usage fields, so duplicating either here is how the row store and
    the metrics would come to disagree about one turn. ``model`` and ``provider``
    come from the same place for the same reason — the record's ``model`` is the
    RESOLVED one (``_resolve_model``), so the attribute names the model that ran
    rather than the alias the caller asked for.

    An ABSENT ``stop_reason`` attribute and a ``None``/empty one mean different
    things and must not collapse. A bare ``TurnUsage`` has no such attribute and
    cannot say how its turn ended (``unclassified``); an ``LLMEvent`` whose
    ``stop_reason`` is ``None`` is reporting a CLEAN turn, which is what the acp
    path leaves on a normal completion. The distinction lives here rather than in
    ``turn_outcome`` because only this layer can see whether the attribute exists
    at all — and it must not be resolved by a default: labelling the bare-usage
    case ``ok`` claims successes nobody observed, while labelling it ``unknown``
    puts it in ``telemetry._TERMINAL_FAULT_OUTCOMES`` and invents a fault for
    every clean background turn.
    """
    _MISSING = object()
    stop = getattr(event, "stop_reason", _MISSING)
    outcome = OUTCOME_UNCLASSIFIED if stop is _MISSING else turn_outcome(stop)  # type: ignore[arg-type]  # noqa: E501
    model = str(record.get("model") or "")
    provider = str(record.get("provider") or "")
    emit_turn_duration(
        record.get("duration_ms"),
        session_key=slot_key,
        outcome=outcome,
        model=model,
        provider=provider,
    )
    emit_turn_usage(
        input_tokens=record.get("input"),
        output_tokens=record.get("output"),
        credits=record.get("credits"),
        cost_usd=record.get("cost"),
        model=model,
        provider=provider,
    )


def get_usage_cache() -> dict:
    """Public accessor for billing usage cache from sessions handler."""
    try:
        from kiro_crew.dashboard.handlers.sessions import _usage_cache

        return dict(_usage_cache) if _usage_cache else {}
    except (ImportError, TypeError):
        logger.debug("Failed to read billing cache", exc_info=True)
        return {}


async def api_kiro_usage(request: web.Request) -> web.Response:
    """GET /api/usage/kiro — local session analytics + cached billing."""
    global _CACHE, _CACHE_TS
    now = time.time()

    # Fast path — lock-free read is intentional; worst case is one extra
    # cache refresh which is harmless (double-checked locking pattern).
    if now - _CACHE_TS < _CACHE_TTL and _CACHE:
        return web.json_response(_CACHE)

    async with _CACHE_LOCK:
        # Re-check after acquiring lock; another request may have refreshed.
        now = time.time()
        if now - _CACHE_TS < _CACHE_TTL and _CACHE:
            return web.json_response(_CACHE)

        username = getpass.getuser()
        sessions, refreshing = await _usage_sessions_snapshot()

        # Get billing from existing usage cache
        billing: dict = {}
        usage = get_usage_cache()
        # Only surface billing when a real credit plan parsed. The cache can hold
        # an {"available": False} sentinel (kiro-cli absent / unparseable output)
        # which is truthy but carries no billing fields — treat it as no billing.
        if usage.get("credits_plan") is not None:
            billing = {
                "credits_used": usage.get("credits_used"),
                "credits_plan": usage.get("credits_plan"),
                "credits_overage": usage.get("credits_overage"),
                "percentage": usage.get("percentage"),
                "cost_usd": usage.get("cost_usd"),
                "resets": usage.get("resets"),
                "plan": usage.get("plan"),
                "overage_rate": usage.get("overage_rate"),
            }

        response: dict[str, Any] = {
            "username": username,
            "sessions": sessions,
            "billing": billing,
            "refreshing": refreshing,
        }

        if "error" in sessions:
            response["error"] = sessions["error"]
        elif not refreshing:
            # A partial response must never enter the two-minute full-response
            # cache or the frontend's fast poll would keep reading the placeholder.
            _CACHE = response
            _CACHE_TS = time.time()

    return web.json_response(response)


async def api_usage(request: web.Request) -> web.Response:
    """GET /api/usage — usage stats for the kiro-cli (KiroACP) provider."""
    return await api_kiro_usage(request)


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.usage_store",
    "kiro_crew.dashboard.handlers.usage_spend",
    "kiro_crew.dashboard.handlers.usage_context",
    "kiro_crew.dashboard.handlers.usage_cost",
    "kiro_crew.dashboard.handlers.usage_history",
    "kiro_crew.dashboard.handlers.usage_sessions",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.usage_context import (  # noqa: F401
        _CONTEXT_CACHE,
        _CONTEXT_CACHE_KEY,
        _CONTEXT_CACHE_TS,
        _CONTEXT_CACHE_TTL,
        _CONTEXT_TOP_SESSIONS,
        _engine_view,
        _row_iso,
        context_occupancy,
        context_trace,
    )
    from kiro_crew.dashboard.handlers.usage_cost import (  # noqa: F401
        _COMPACTION_DROP_PCT,
        _COMPACTION_PCT,
        _COST_CACHE,
        _COST_CACHE_KEY,
        _COST_CACHE_TS,
        _COST_CACHE_TTL,
        _COST_MIN_GROWTH_TURNS,
        _COST_TOP_CONVOS,
        cost_breakdown,
    )
    from kiro_crew.dashboard.handlers.usage_history import (  # noqa: F401
        _TOKEN_CACHE,
        _TOKEN_CACHE_KEY,
        _TOKEN_CACHE_TS,
        _TOKEN_CACHE_TTL,
        _TOKEN_HISTORY_DAYS,
        _parse_token_history,
    )
    from kiro_crew.dashboard.handlers.usage_sessions import (  # noqa: F401
        _SESSIONS_CACHE,
        _SESSIONS_CACHE_LOCK,
        _SESSIONS_CACHE_TS,
        _USAGE_SESSION_REFRESH_TASK,
        _USAGE_SESSION_WAIT_SECONDS,
        _cached_parse_sessions,
        _empty_session_summary,
        _parse_sessions,
    )
    from kiro_crew.dashboard.handlers.usage_spend import (  # noqa: F401
        _BARE_CHAT_SLOT_RE,
        _SLOT_SPEND_CACHE,
        _SLOT_SPEND_CACHE_AT,
        _SLOT_SPEND_CACHE_SIG,
        _SLOT_SPEND_TTL_S,
        TURN_USAGE_FIELDS,
        daily_credits,
        slot_spend,
        slot_turn_usage,
        spend_key_for_slot,
    )
    from kiro_crew.dashboard.handlers.usage_store import (  # noqa: F401
        _BACKGROUND_CHANNELS,
        _LEGACY_TELEMETRY_SURFACES,
        _MIN_ROLLING_DAYS,
        _NON_SESSION_CHANNELS,
        _SESSIONS_DIR,
        _SESSIONS_HISTORY_DAYS,
        _TOKEN_USAGE_DIR,
        MAX_WINDOW_DAYS,
        NAVIGABLE_CATEGORY,
        SPEND_WINDOW_DAYS,
        TimeWindow,
        _canonical_telemetry_surface,
        _parse_bound,
        _parse_row_dt,
        _sessions_dir,
        _token_usage_dir,
        is_session_slot,
        resolve_window,
        session_category,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import model_registry  # noqa: F401
    from kiro_crew.config.paths import data_home, kiro_sessions_dir  # noqa: F401
    from kiro_crew.constants import crew_log_enabled, env_file_display  # noqa: F401
    from kiro_crew.context_blocks import USER_LABEL  # noqa: F401
    from kiro_crew.hooks import validate_file_path  # noqa: F401
    from kiro_crew.jsonl_util import bounded_records  # noqa: F401
    from kiro_crew.messaging.link import telemetry_channel_of  # noqa: F401
