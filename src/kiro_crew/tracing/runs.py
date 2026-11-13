"""Run roots: where turns that something else caused hang (tracing RFC §5.1, R2-R5).

A turn nothing caused is its own trace (``turns.py``). A turn another run caused
joins that run's trace instead, under a :class:`RunRef`: the trace id and the
span its children parent to, both deterministic hex ids, so nothing is held open
in memory and a restart can re-derive them. The run kinds share these helpers:

- **R2 anchors.** :func:`anchor` emits a run's root as a zero-length span that
  ends at once; children parent to it by id. :func:`outcome` writes the
  ``<kind> outcome <status>`` marker when the run ends.
- **R3 roll-ups.** :class:`Rollup` sums the spend of a span's child turns into
  ``kirocrew.usage.*_total`` keys, for a span that ends after its children.
  Never ``gen_ai.usage.*``: a backend would count the spend twice.
- **R4 joins.** A turn binds a :class:`RunRef` (``turns.bind_turn_root(join=)``)
  when one finished run is its only cause; with several causes it roots itself
  and :func:`link` points at each.
- **R5 budgets.** :func:`segment` moves a run onto a linked continuation trace
  once its trace holds :data:`SPAN_BUDGET` spans or is :data:`AGE_BUDGET_SECS`
  old, inside X-Ray's 7-day modification window and its trace-size limit.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from kiro_crew.tracing import provider

#: R5: a run's trace rolls over after this many spans or this age.
SPAN_BUDGET = 2000
AGE_BUDGET_SECS = 6 * 24 * 3600

#: Spend keys a child turn carries, and the roll-up key each one sums into.
_TOTALS = (
    ("gen_ai.usage.input_tokens", "kirocrew.usage.input_tokens_total"),
    ("gen_ai.usage.output_tokens", "kirocrew.usage.output_tokens_total"),
    ("kirocrew.usage.credits", "kirocrew.usage.credits_total"),
    ("kirocrew.usage.cost_usd", "kirocrew.usage.cost_usd_total"),
)
_TURNS_TOTAL = "kirocrew.usage.turns_total"

_MAX_SEGMENTS = 4096


@dataclass(frozen=True)
class RunRef:
    """One run's place in a trace: its trace id and the span its children hang under.

    ``kind`` and ``key`` name the run across continuations; ``cont`` counts
    them from 1. ``started`` is when this trace began (wall clock), for R5.
    ``root_session_key`` is the Crew session whose ``session.id`` every span of
    the trace carries (R6).
    """

    kind: str
    key: tuple[str, ...]
    trace_id: str
    span_id: str
    root_session_key: str = ""
    started: float = 0.0
    cont: int = 1


def parent_of(trace_id: str, span_id: str) -> Any:
    """A span handle with these ids, for use as a parent: no span is held open."""
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    return NonRecordingSpan(
        SpanContext(int(trace_id, 16), int(span_id, 16), False, TraceFlags(TraceFlags.SAMPLED))
    )


def link(trace_id: str, span_id: str, attributes: Mapping[str, Any] | None = None) -> Any:
    """A span link to the span with these ids."""
    from opentelemetry.trace import Link

    return Link(parent_of(trace_id, span_id).get_span_context(), dict(attributes or {}))


def _run_attrs(state: provider.TraceState, ref: RunRef, attrs: Mapping[str, Any]) -> dict:
    from kiro_crew.tracing import correlation

    key = ref.root_session_key
    return correlation.clean(
        {
            **attrs,
            "kirocrew.run.kind": ref.kind,
            "kirocrew.run.continuation": ref.cont,
            **correlation.session_attrs(state.hash_key, key, key),
        }
    )


def anchor(
    state: provider.TraceState,
    kind: str,
    name: str,
    key: tuple[str, ...],
    *,
    root_session_key: str = "",
    cont: int = 1,
    attrs: Mapping[str, Any] | None = None,
    links: tuple[Any, ...] = (),
) -> RunRef:
    """Emit run *kind*'s root as a zero-length span and return where children hang.

    Ids are ``H(trace.<kind>, *key, cont)`` and ``H(<kind>, *key, cont)``, so a
    restart that re-emits nothing still parents new children correctly.
    """
    from kiro_crew.tracing import ids

    ns = state.namespace
    ref = RunRef(
        kind,
        tuple(str(part) for part in key),
        ids.trace_id(ns, kind, *key, cont),
        ids.span_id(ns, kind, *key, cont),
        root_session_key,
        time.time(),
        cont,
    )
    label = name if cont == 1 else f"{name} (cont {cont})"
    provider.start_span(
        state,
        label,
        trace_id=ref.trace_id,
        span_id=ref.span_id,
        attributes=_run_attrs(state, ref, attrs or {}),
        links=links,
    ).end()
    return ref


def outcome(
    state: provider.TraceState,
    ref: RunRef,
    status: str,
    *,
    outcome_key: str = "",
    rollup: "Rollup | None" = None,
) -> None:
    """Write ``<kind> outcome <status>`` under *ref*, carrying the run's roll-up (R2, R3).

    *outcome_key* tells apart several ends of one run (a re-opened work item's
    ledger event id); a run with one end leaves it empty.
    """
    from kiro_crew.tracing import ids

    provider.start_span(
        state,
        f"{ref.kind} outcome {status}",
        trace_id=ref.trace_id,
        span_id=ids.span_id(
            state.namespace, f"{ref.kind}.outcome", *ref.key, ref.cont, outcome_key
        ),
        parent=parent_of(ref.trace_id, ref.span_id),
        attributes=_run_attrs(
            state, ref, {"kirocrew.run.status": status, **(rollup.attrs() if rollup else {})}
        ),
    ).end()


class Rollup:
    """Sums the spend of a span's child turns (R3). Not thread-safe: one owner."""

    def __init__(self) -> None:
        self._totals: dict[str, float] = {}
        self._turns = 0

    def add(self, spend: Mapping[str, Any]) -> None:
        """Add one child turn's spend attributes."""
        self._turns += 1
        for source, total in _TOTALS:
            value = spend.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                self._totals[total] = self._totals.get(total, 0) + value

    def attrs(self) -> dict[str, Any]:
        """The roll-up keys; empty when no child turn ended."""
        return {**self._totals, _TURNS_TOTAL: self._turns} if self._turns else {}


_lock = threading.Lock()
#: The live continuation of each run that rolled over, by ``(kind, key)``.
_segments: "OrderedDict[tuple[str, tuple[str, ...]], RunRef]" = OrderedDict()


def over_budget(ref: RunRef, *, now: float | None = None) -> bool:
    """Whether *ref*'s trace has used up R5's span count or age."""
    if provider.span_count(ref.trace_id) >= SPAN_BUDGET:
        return True
    return bool(ref.started) and (now if now is not None else time.time()) - ref.started >= (
        AGE_BUDGET_SECS
    )


def segment(state: provider.TraceState, ref: RunRef, name: str) -> RunRef:
    """Where a new child of run *ref* goes: *ref*, or its live continuation (R5).

    A trace past its budget gets a continuation anchor, ``<name> (cont N)``, in a
    new trace that links back to where the run was, and every later child of the
    run joins that one. Kept per process: after a restart the count starts over.
    """
    run = (ref.kind, ref.key)
    with _lock:
        live = _segments.get(run, ref)
        if not over_budget(live):
            return live
        nxt = anchor(
            state,
            ref.kind,
            name,
            ref.key,
            root_session_key=ref.root_session_key,
            cont=live.cont + 1,
            links=(link(live.trace_id, live.span_id),),
        )
        _segments[run] = nxt
        _segments.move_to_end(run)
        while len(_segments) > _MAX_SEGMENTS:
            _segments.popitem(last=False)
        return nxt


def reset_for_testing() -> None:
    """Forget every continuation. Test isolation only."""
    with _lock:
        _segments.clear()
