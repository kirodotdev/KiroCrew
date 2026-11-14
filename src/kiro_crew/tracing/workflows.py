"""Workflow runs as one trace: the run, its phases, its agent calls (tracing RFC §5.7).

A workflow run is a run kind of ``tracing/surfaces.py`` (``TRIGGER_WORKFLOW``).
:func:`run_root` opens its ``workflow <name>`` anchor and, when the run ends, its
``workflow outcome <status>`` marker, which carries the summed spend of every agent
call (R2, R3). In between, this module adds what only the workflow DSL has:

- ``phase <title>``: one span per ``ctx.phase()``, from its ``phase_started`` event
  to the next one or the run's end, since there is no phase-end event. Its id is
  ``H(workflow.phase, run_id, cont, phase_index)`` and its roll-up is its calls'
  spend.
- Each ``ctx.agent()`` call that runs live is one turn under its phase
  (:func:`bind_agent`), named ``invoke_agent <phase>:<label>``, with turn id
  ``H(workflow.agent, run_id, agent_id)``. A call replayed from a prior run's
  results streams no turn, so a resumed run re-emits none.
- The completion turn: :func:`pin_launcher` records the launching turn when the run
  starts, and :func:`delivering` hands the run to the turn that reports its result.
  That turn joins the run's trace under its root and links to the launcher (R4).

Ids are deterministic, so running the same run again rebuilds the same tree. Names
are script text: each is collapsed to one line, passed whole through
``validate_attrs`` (the check every other span string gets), then cut to
:data:`NAME_MAX`.

Off: :func:`run_root` costs ``surfaces.root``'s switch read and context-variable
write plus one table lookup at its end, and the observer hands every event on
untouched.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator

from kiro_crew.tracing import provider, runs, surfaces, turns

logger = logging.getLogger(__name__)

KIND = surfaces.TRIGGER_WORKFLOW
#: Longest workflow name, phase title or call label a span name keeps.
NAME_MAX = 64
#: Runs, and launchers, remembered for a completion turn; the oldest go first.
_MAX_RUNS = 1024
#: The queued-entry ``meta`` key naming the run a completion prompt reports.
META_RUN_ID = "workflow_run_id"


def label(text: object) -> str:
    """*text* as one bounded line a span name may carry; ``""`` when nothing usable is left.

    Checked whole, then cut: cutting first could leave a credential fragment too
    short for the redactor to recognise.
    """
    from kiro_crew.metrics.schema import validate_attrs

    line = " ".join(str(text or "").split())
    if not line:
        return ""
    try:
        return str(validate_attrs({"name": line}).get("name", ""))[:NAME_MAX].strip()
    except ValueError:
        return ""


@dataclass
class _Phase:
    index: int
    span: Any
    span_id: str
    rollup: runs.Rollup
    #: Live calls bound under the phase and not yet released.
    calls: int = 0
    #: When the next phase or the run's end closed it; the span ends at this time.
    end_ns: int | None = None
    ended: bool = False


@dataclass
class _Call:
    """One bound ``ctx.agent()`` call: release it with :func:`release_agent`."""

    token: contextvars.Token
    phase: _Phase | None


class _Run:
    def __init__(self, run_id: str, ref: runs.RunRef) -> None:
        self.run_id = run_id
        self.ref = ref
        self.phase: _Phase | None = None


_lock = threading.Lock()
_runs: OrderedDict[str, _Run] = OrderedDict()
#: run_id -> (trace id, span id) of the turn that launched it.
_launchers: OrderedDict[str, tuple[str, str]] = OrderedDict()
#: turn id -> the run that completion turn took, for a retry of it (:func:`recovery_meta`).
_taken: OrderedDict[str, str] = OrderedDict()


def _put(table: OrderedDict, key: str, value: Any) -> None:
    with _lock:
        table[key] = value
        table.move_to_end(key)
        while len(table) > _MAX_RUNS:
            table.popitem(last=False)


def _run(run_id: str) -> _Run | None:
    with _lock:
        return _runs.get(run_id)


def _common(run_id: str, **extra: Any) -> dict[str, Any]:
    return {"kirocrew.trigger": KIND, "kirocrew.workflow_run_id": run_id, **extra}


@contextmanager
def run_root(run_id: str, name: str, session_key: str) -> Iterator[surfaces.Surface]:
    """The run's root for this block: its anchor, then its outcome as the block ends.

    Set the yielded surface's ``status`` to say how the run ended. *session_key*
    is the launching session, whose ``session.id`` every span of the run wears.
    """
    with surfaces.root(
        KIND,
        run_name=label(name),
        run_key=(run_id,),
        root_session_key=session_key,
        attrs=_common(run_id),
    ) as surface:
        if surface.run is not None:
            _put(_runs, run_id, _Run(run_id, surface.run))
        try:
            yield surface
        finally:
            _end_phase(_run(run_id), final=True)


def observe(run_id: str, on_event: Callable[[Any], None] | None) -> Callable[[Any], None] | None:
    """*on_event*, also opening a phase span at each ``phase_started`` while tracing is on."""
    if provider.peek() is None:
        return on_event

    def record(event: Any) -> None:
        if getattr(event, "type", "") == "phase_started":
            _start_phase(run_id, getattr(event, "data", {}) or {})
        if on_event is not None:
            on_event(event)

    return record


def _start_phase(run_id: str, data: dict) -> None:
    state = provider.peek()
    run = _run(run_id)
    if state is None or run is None:
        return
    try:
        from kiro_crew.tracing import ids

        _end_phase(run)
        index = data.get("phase_index")
        if not isinstance(index, int) or isinstance(index, bool):
            return
        ref = run.ref
        span_id = ids.span_id(state.namespace, f"{KIND}.phase", *ref.key, ref.cont, index)
        title = label(data.get("title"))
        span = provider.start_span(
            state,
            f"phase {title}".strip(),
            trace_id=ref.trace_id,
            span_id=span_id,
            parent=runs.parent_of(ref.trace_id, ref.span_id),
            attributes=runs._run_attrs(
                state, ref, _common(run_id, **{"kirocrew.phase_index": index})
            ),
        )
        run.phase = _Phase(index, span, span_id, runs.Rollup())
    except Exception:
        logger.debug("workflow phase trace failed", exc_info=True)


def _end_phase(run: _Run | None, *, final: bool = False) -> None:
    """Close the run's open phase now; its span ends once its live calls settle.

    A parallel branch can open the next phase while another branch's call under
    this one is still running, and that call's spend belongs to this phase's
    roll-up. *final* (the run's end) ends the open phase whatever is still in flight;
    a phase closed earlier waits for its calls, which the runner settles before the
    run ends.
    """
    phase = run.phase if run is not None else None
    if run is None or phase is None:
        return
    run.phase = None
    with _lock:
        phase.end_ns = time.time_ns()
        settled = phase.calls == 0 or final
    if settled:
        _finish(phase)


def _finish(phase: _Phase) -> None:
    with _lock:
        if phase.ended:
            return
        phase.ended = True
    try:
        phase.span.set_attributes(phase.rollup.attrs())
        phase.span.end(end_time=phase.end_ns)
    except Exception:
        logger.debug("workflow phase trace end failed", exc_info=True)


def bind_agent(
    run_id: str, agent_id: str, call_label: str, phase_title: str, phase_index: int
) -> _Call | None:
    """Bind one live ``ctx.agent()`` call as a turn under its phase. Never raises.

    The call sits under the open phase when *phase_index* is that phase's, else
    under the run's root. Release with :func:`release_agent`. Unbound (``None``)
    when tracing is off or the run has no trace.
    """
    state = provider.peek()
    run = _run(run_id)
    if state is None or run is None:
        return None
    try:
        from kiro_crew.tracing import ids

        phase = run.phase if run.phase is not None and run.phase.index == phase_index else None
        join = replace(run.ref, span_id=phase.span_id) if phase is not None else run.ref
        name = ":".join(part for part in (label(phase_title), label(call_label)) if part)

        def _ended(turn: turns.TurnTrace) -> None:
            if phase is not None:
                phase.rollup.add(turn.spend)

        token = turns.bind_turn_root(
            ids.digest(state.namespace, f"{KIND}.agent", run_id, agent_id)[:32],
            join=join,
            attrs=_common(run_id, **{"kirocrew.agent_id": agent_id}),
            on_end=_ended,
            rollup_key=run.ref.span_id,
            name=f"{turns.OP_INVOKE_AGENT} {name}".strip(),
        )
    except Exception:
        logger.debug("workflow agent trace bind failed", exc_info=True)
        return None
    if token is None:
        return None
    if phase is not None:
        with _lock:
            phase.calls += 1
    return _Call(token, phase)


def release_agent(call: _Call | None) -> None:
    """Undo :func:`bind_agent`; the last call of a closed phase ends its span."""
    if call is None:
        return
    turns.release_turn_root(call.token)
    phase = call.phase
    if phase is None:
        return
    with _lock:
        phase.calls -= 1
        settled = phase.calls == 0 and phase.end_ns is not None
    if settled:
        _finish(phase)


# ── the completion turn ───────────────────────────────────────────────────


def pin_launcher(run_id: str, session_key: str) -> None:
    """Remember the turn launching *run_id*: the one open now for *session_key*."""
    if not run_id or provider.peek() is None:
        return
    turn = turns.open_turn_of_session(session_key)
    if turn is not None:
        _put(_launchers, run_id, (turn.trace_id, turn.span_id))


class _Cause:
    """A one-shot holder: the first turn to read it takes the run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


_CAUSE: contextvars.ContextVar[_Cause | None] = contextvars.ContextVar(
    "kirocrew_trace_workflow_cause", default=None
)


@contextmanager
def delivering(run_id: str) -> Iterator[None]:
    """Around starting the turn that reports *run_id*: hand that turn the run.

    The turn's task copies the context when it is created. Set even for ``""``, so
    a turn started from inside an earlier turn's task never inherits that turn's
    untaken run.
    """
    token = _CAUSE.set(_Cause(run_id) if run_id else None)
    try:
        yield
    finally:
        _CAUSE.reset(token)


def run_of(entries: Iterable[Any]) -> str:
    """The run the first of these queued entries reports, from its ``meta``; else ``""``."""
    for entry in entries:
        meta = entry.get("meta") if isinstance(entry, dict) else None
        run_id = meta.get(META_RUN_ID) if isinstance(meta, dict) else None
        if isinstance(run_id, str) and run_id:
            return run_id
    return ""


def completion_binding(
    turn_id: str = "",
) -> tuple[runs.RunRef | None, tuple[Any, ...], dict[str, Any]]:
    """``(join, links, attrs)`` for the turn reporting the run handed to it, taken once.

    The turn joins the run's trace under its root, links to the launching turn when
    that was pinned, and carries the run's id. ``(None, (), {})`` when no run was
    handed over, or this process never traced it. With *turn_id* the run is also
    kept under it, so a retry of that turn reports the same run
    (:func:`recovery_meta`).
    """
    holder = _CAUSE.get()
    if holder is None or not holder.run_id or provider.peek() is None:
        return None, (), {}
    run_id, holder.run_id = holder.run_id, ""
    run = _run(run_id)
    if run is None:
        return None, (), {}
    from kiro_crew.tracing import correlation

    if turn_id:
        _put(_taken, turn_id, run_id)
    with _lock:
        launcher = _launchers.get(run_id)
    attrs = {"kirocrew.workflow_run_id": run_id}
    links = (runs.link(*launcher, correlation.clean(attrs)),) if launcher is not None else ()
    return run.ref, links, attrs


def recovery_meta(turn_id: str) -> dict[str, str]:
    """The queued-entry ``meta`` a retry of turn *turn_id* needs to report the same run."""
    with _lock:
        run_id = _taken.get(turn_id) if turn_id else None
    return {META_RUN_ID: run_id} if run_id else {}


def reset_for_testing() -> None:
    """Forget every run and launcher. Test isolation only."""
    with _lock:
        _runs.clear()
        _launchers.clear()
        _taken.clear()
