"""Surface roots: the one way an engine opens the turns it starts (tracing RFC §5.2, layer 2).

Every engine that starts an ACP turn without a dashboard turn around it (a channel
message, a cron run, a heartbeat, a background job, a webhook, a task-runner step)
calls :func:`root` (or :func:`rooted`) around the turn, so every root kind gets the same tree:

- a kind with no run of its own (``RUN_KINDS`` excludes it) roots each turn by
  itself, ``trace_id`` = the turn id (R1);
- a run kind (``cron``, ``workflow``) emits a ``<kind> <name>`` anchor first (R2), the turns it
  streams join that anchor's trace (R4), and the block ends with a
  ``<kind> outcome <status>`` marker carrying the turns' summed spend (R3).

Either way every span of the turns carries ``kirocrew.trigger`` and, for a channel,
``kirocrew.channel``. Every prompt streamed inside one :func:`root` block is the
next attempt of the same turn (``kirocrew.attempt``), so a retry shows as attempt 2
rather than a new trace. A *step* is a span under the run for work that is not a
model turn (a script or command cron run), so such a run has no ``chat`` span.

Off (``provider.peek()`` is ``None``): one context-variable write, no SDK import.
Ids are deterministic (``tracing/ids.py``): the anchor from ``(kind, *key, cont)``,
the step from ``(<kind>.step, *key, cont, step)``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Coroutine, Iterator, Mapping, ParamSpec, TypeVar

from kiro_crew.tracing import provider, runs, turns

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

TRIGGER_CHANNEL = "channel"
TRIGGER_CRON = "cron"
TRIGGER_HEARTBEAT = "heartbeat"
TRIGGER_BACKGROUND = "background"
TRIGGER_WEBHOOK = "webhook"
TRIGGER_TASK = "task"
TRIGGER_WORKFLOW = "workflow"
#: Every ``kirocrew.trigger`` value: a closed vocabulary, never caller text.
TRIGGERS = frozenset(
    {
        TRIGGER_CHANNEL,
        TRIGGER_CRON,
        TRIGGER_HEARTBEAT,
        TRIGGER_BACKGROUND,
        TRIGGER_WEBHOOK,
        TRIGGER_TASK,
        TRIGGER_WORKFLOW,
    }
)
#: The triggers that are runs: an anchor, joined turns and an outcome marker.
RUN_KINDS = frozenset({TRIGGER_CRON, TRIGGER_WORKFLOW})

STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
#: The run was due but its payload never started (a gate refused or deferred it).
STATUS_SKIPPED = "skipped"
#: The statuses a step span marks as an error.
_ERROR_STATUSES = frozenset({STATUS_FAILED, STATUS_CANCELLED})


class Surface:
    """An open :func:`root` block. Set :attr:`status` to say how the run ended."""

    def __init__(self, trigger: str, run: runs.RunRef | None) -> None:
        self.trigger = trigger
        #: The run's place in its trace, or ``None`` (not a run kind, or tracing off).
        self.run = run
        #: Written on the outcome marker; an exception out of the block overrides it
        #: only while it is still ``finished``.
        self.status = STATUS_FINISHED


def _status_of(exc: BaseException | None, status: str) -> str:
    # A status the block set is the run's own reading of how it ended.
    if exc is None or status != STATUS_FINISHED:
        return status
    return STATUS_CANCELLED if isinstance(exc, asyncio.CancelledError) else STATUS_FAILED


@contextmanager
def root(
    trigger: str,
    *,
    channel: str = "",
    run_name: str = "",
    run_key: tuple[str, ...] = (),
    root_session_key: str = "",
    step: str = "",
    attrs: Mapping[str, Any] | None = None,
) -> Iterator[Surface]:
    """Open a surface root for the turns streamed in this block. Never raises its own error.

    *trigger* is one of :data:`TRIGGERS`. *channel* names a channel's namespace.
    For a run kind, *run_key* identifies the run and *run_name* labels its anchor
    (a closed-vocabulary label, or a name the caller bounded and checked, as
    ``tracing/workflows.py`` does with a workflow's name; never raw user text), and
    *root_session_key* is the
    Crew session whose ``session.id`` every span of the run wears (R6); *step* opens a span under the run
    for the whole block, for a run that streams no model turn. *attrs* are extra
    allowlisted keys for the run's spans.

    Opened inside another root bound in this task (a background one-liner a
    dashboard turn awaits), the block's turns are its own, not more attempts of
    the outer turn; the outer binding is back once the block ends.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown surface trigger {trigger!r}")
    common = {"kirocrew.trigger": trigger, "kirocrew.channel": channel}
    state = provider.peek()
    surface = Surface(trigger, None)
    step_span: Any = None
    if state is not None and trigger in RUN_KINDS and run_key:
        surface.run, step_span = _open_run(
            state, trigger, run_name, run_key, root_session_key, step, common, attrs
        )
    token = turns.bind_turn_root(uuid.uuid4().hex, join=surface.run, attrs=common)
    error: BaseException | None = None
    try:
        yield surface
    except BaseException as exc:
        error = exc
        raise
    finally:
        turns.release_turn_root(token)
        if state is not None and surface.run is not None:
            _close_run(
                state,
                surface.run,
                step_span,
                _status_of(error, surface.status),
                {**common, **(attrs or {})},
            )


def rooted(
    trigger: str,
) -> Callable[[Callable[_P, Coroutine[Any, Any, _T]]], Callable[_P, Coroutine[Any, Any, _T]]]:
    """Decorate an engine's async turn function so each call runs in its own :func:`root`.

    For an engine whose one call is one turn and its retries (a webhook, a
    heartbeat task, a task-runner step, a background one-liner).
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown surface trigger {trigger!r}")

    def wrap(fn: Callable[_P, Coroutine[Any, Any, _T]]) -> Callable[_P, Coroutine[Any, Any, _T]]:
        @functools.wraps(fn)
        async def run(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with root(trigger):
                return await fn(*args, **kwargs)

        return run

    return wrap


def _open_run(
    state: provider.TraceState,
    kind: str,
    name: str,
    key: tuple[str, ...],
    root_session_key: str,
    step: str,
    common: Mapping[str, Any],
    attrs: Mapping[str, Any] | None,
) -> tuple[runs.RunRef | None, Any]:
    """The run's anchor, its roll-up and its step span; ``(None, None)`` on any failure."""
    ref: runs.RunRef | None = None
    try:
        from kiro_crew.tracing import ids

        extra = {**common, **(attrs or {})}
        ref = runs.anchor(
            state,
            kind,
            f"{kind} {name}".strip(),
            key,
            root_session_key=root_session_key,
            attrs=extra,
        )
        turns.open_rollup(ref.span_id)
        span = None
        if step:
            span = provider.start_span(
                state,
                f"{kind} {step}",
                trace_id=ref.trace_id,
                span_id=ids.span_id(state.namespace, f"{kind}.step", *ref.key, ref.cont, step),
                parent=runs.parent_of(ref.trace_id, ref.span_id),
                attributes=runs._run_attrs(state, ref, extra),
            )
        return ref, span
    except Exception:
        logger.debug("surface run open failed", exc_info=True)
        if ref is not None:
            turns.close_rollup(ref.span_id)
        return None, None


def _close_run(
    state: provider.TraceState,
    ref: runs.RunRef,
    step_span: Any,
    status: str,
    attrs: Mapping[str, Any],
) -> None:
    """End the step span and write the outcome, which carries the anchor's *attrs*."""
    try:
        if step_span is not None:
            if status in _ERROR_STATUSES:
                from opentelemetry.trace import Status, StatusCode

                step_span.set_status(Status(StatusCode.ERROR))
            step_span.set_attribute("kirocrew.run.status", status)
            step_span.end()
        runs.outcome(state, ref, status, rollup=turns.close_rollup(ref.span_id), attrs=attrs)
    except Exception:
        logger.debug("surface run close failed", exc_info=True)
