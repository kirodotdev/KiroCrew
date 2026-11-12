"""Tracer lifecycle: off by default, built once the switch reads on, kept across config changes.

``telemetry.traces_enabled`` (default off) is read on a worker thread that
:func:`watch_config` starts at gateway boot; the turn paths only :func:`peek`
at what that read resolved. Off means no OpenTelemetry SDK import, no
``TracerProvider`` and no export thread: every hook point costs one call that
returns ``None``. On means one ``TracerProvider`` with a ``BatchSpanProcessor``
writing to the local JSONL sink under ``<data home>/traces`` and nothing else;
spans never leave the host from this module.

Kept apart from ``metrics/provider.py``: the two signals share the shard store
and the attribute guard, not a lifecycle. :func:`watch_config` re-reads the
switch after a ``telemetry.*`` write that moves one of its inputs, keeping the
one tracer it built; a process that registers no watcher never reads the switch
and is not traced.

**Deterministic ids.** The SDK mints ids through its ``IdGenerator``. Every span
here is started by :func:`start_span`, which pins the ids the ``tracing/ids.py``
formula gives for the next ``start_span`` call on this thread; the generator
hands those out and falls back to random ids for any span it did not pin.
"""

from __future__ import annotations

import logging
import random
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Instrumentation scope name (RFC decision f's safe default).
SCOPE_NAME = "kiro_crew"


@dataclass(frozen=True)
class TraceState:
    """A live tracer and what every span derivation needs from this install."""

    tracer: Any
    provider: Any
    namespace: str
    #: Key for hashed correlation ids; ``None`` when the vault could not give one.
    hash_key: bytes | None
    #: The local JSONL sink, whose caps a config write updates in place.
    sink: Any = None


_lock = threading.Lock()
_resolved = False
#: What the hooks are served: the tracer while the switch is on, else ``None``.
_state: TraceState | None = None
#: The one tracer this process ever builds. Kept across an off/on toggle and
#: shut down only by :func:`shutdown` (process exit, tests): a shutdown under a
#: streaming turn would drop the spans it still has to end, and a second tracer
#: would be a second writer on this process's shard.
_built: TraceState | None = None
_pins = threading.local()


def current() -> TraceState | None:
    """The live trace state, resolving it on THIS thread when it is not yet known.

    Resolution reads config and, when tracing is on, the vault and the namespace
    file, so the turn paths never call this: they call :func:`peek`.
    """
    if _resolved:
        return _state
    with _lock:
        if not _resolved:
            _install_locked(_build())
        return _state


def resolved() -> bool:
    """Whether the switch is resolved: read since the process started or since it
    was last cleared (a ``telemetry.*`` write that moved an input, or :func:`shutdown`).
    """
    return _resolved


def peek() -> TraceState | None:
    """The trace state if already resolved, else ``None``; never reads or blocks.

    The hook points run on the event loop and on the ACP reader task, where a
    config read, a vault key's fsync or a stalled data-home disk must not stall
    every turn. Resolution happens off the loop: on the thread
    :func:`watch_config` starts at boot, and on the worker thread that applies a
    ``telemetry.*`` write. A turn that starts before it finishes is not traced.
    """
    return _state if _resolved else None


def _install_locked(state: TraceState | None) -> None:
    global _resolved, _state
    _state = state
    _resolved = True


def traces_dir() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / "traces"


def _inputs(cfg: Any) -> tuple[bool, object, object]:
    """The settings a tracer is built from; a write that moves none keeps it."""
    return (cfg.traces_enabled is True, cfg.retention_days, cfg.max_total_mb)


#: ``_inputs`` of the last resolution, or ``None`` when it could not read config.
_built_inputs: tuple[bool, object, object] | None = None


def _build() -> TraceState | None:
    """Read the switch and, only when it is on, import the SDK and build."""
    global _built_inputs
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load().telemetry
    except Exception as exc:
        logger.warning("tracing config load failed; tracing off: %s", exc)
        _built_inputs = None
        return None
    _built_inputs = _inputs(cfg)
    global _built
    # Today's caps reach the kept tracer whatever the switch says: a turn still
    # open on it keeps exporting (and pruning), so with tracing just switched off
    # a cap the operator set to 0 must stop deletions too.
    if _built is not None and _built.sink is not None:
        try:
            _built.sink.set_caps(retention_days=cfg.retention_days, max_total_mb=cfg.max_total_mb)
        except (TypeError, ValueError):  # a malformed section: keep the caps it has
            logger.warning("tracing caps unreadable; keeping the current ones")
    # Only a real True turns it on: a section that is not a TelemetryConfig (a
    # test double, a malformed load) must never start an export thread.
    if cfg.traces_enabled is not True:
        return None
    if _built is not None:  # switched back on: the same tracer, today's caps
        return _built
    try:
        _built = _build_live(cfg)
    except Exception as exc:  # tracing must never cost a turn
        logger.warning("tracing init failed; tracing off: %s", exc)
    return _built


def _hash_key() -> bytes | None:
    try:
        from kiro_crew.config.paths import config_dir
        from kiro_crew.secrets.vault import SecretVault

        return SecretVault(config_dir()).derive_subkey("trace-correlation")
    except Exception:
        logger.warning("trace correlation key unavailable; hashed ids omitted")
        return None


def _build_live(cfg: Any) -> TraceState:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from kiro_crew.tracing import ids
    from kiro_crew.tracing.exporter import JsonlSpanExporter

    directory = traces_dir()
    # Everything that can fail runs BEFORE the processor exists: its constructor
    # starts the export thread, and a thread nothing owns is never shut down.
    namespace, hash_key = ids.load_namespace(directory), _hash_key()
    exporter = JsonlSpanExporter(
        directory, retention_days=cfg.retention_days, max_total_mb=cfg.max_total_mb
    )
    processor = BatchSpanProcessor(exporter)
    try:
        return new_state(processor, namespace, hash_key, sink=exporter)
    except BaseException:
        processor.shutdown()
        raise


def new_state(
    processor: Any, namespace: str, hash_key: bytes | None, *, sink: Any = None
) -> TraceState:
    """A tracer that hands every finished span to *processor*.

    The production processor is a ``BatchSpanProcessor`` over the JSONL sink;
    a test passes a ``SimpleSpanProcessor`` over an in-memory exporter.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.id_generator import IdGenerator

    from kiro_crew import __version__

    class _PinnedIds(IdGenerator):
        def generate_span_id(self) -> int:
            pinned = getattr(_pins, "span_id", None)
            _pins.span_id = None
            return pinned if pinned is not None else random.getrandbits(64) or 1

        def generate_trace_id(self) -> int:
            pinned = getattr(_pins, "trace_id", None)
            _pins.trace_id = None
            return pinned if pinned is not None else random.getrandbits(128) or 1

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "kirocrew",
                "service.version": __version__,
                "kirocrew.trace_ns": namespace,
            }
        ),
        id_generator=_PinnedIds(),
    )
    provider.add_span_processor(processor)
    return TraceState(provider.get_tracer(SCOPE_NAME), provider, namespace, hash_key, sink)


#: Spans started per trace id in this process, newest traces last: what the run
#: roots' span budget (``tracing/runs.py``) reads. Bounded like the turn registries.
_span_counts: "OrderedDict[str, int]" = OrderedDict()
_counts_lock = threading.Lock()
_MAX_COUNTED_TRACES = 4096


def span_count(trace_id: str) -> int:
    """How many spans this process has started in *trace_id*."""
    with _counts_lock:
        return _span_counts.get(trace_id, 0)


def _count_span(trace_id: str) -> None:
    with _counts_lock:
        _span_counts[trace_id] = _span_counts.pop(trace_id, 0) + 1
        while len(_span_counts) > _MAX_COUNTED_TRACES:
            _span_counts.popitem(last=False)


def start_span(
    state: TraceState,
    name: str,
    *,
    trace_id: str,
    span_id: str,
    parent: Any = None,
    attributes: Mapping[str, Any] | None = None,
    links: Sequence[Any] = (),
) -> Any:
    """Start a span with the given hex ids under *parent* (``None`` = a root).

    A root is started under an EMPTY context, never whatever span happens to be
    current, so its trace id is always the pinned one. Under a parent the span
    takes the parent's trace id, which the caller passes as *trace_id* too.
    """
    from opentelemetry import trace
    from opentelemetry.context import Context

    context = trace.set_span_in_context(parent) if parent is not None else Context()
    _pins.trace_id = int(trace_id, 16)
    _pins.span_id = int(span_id, 16)
    try:
        span = state.tracer.start_span(
            name, context=context, attributes=dict(attributes or {}), links=list(links)
        )
    finally:
        _pins.trace_id = None
        _pins.span_id = None
    _count_span(trace_id)
    return span


def shutdown() -> None:
    """Flush and shut down the tracer, and forget the switch (process exit, tests)."""
    global _resolved, _state, _built
    with _lock:
        doomed, _state, _built, _resolved = _built, None, None, False
    if doomed is not None:
        try:
            doomed.provider.shutdown()
        except Exception as exc:  # a best-effort flush must never raise
            logger.warning("tracer provider shutdown failed: %s", exc)


#: How long the gateway's exit path waits for queued spans to reach the sink.
EXIT_FLUSH_SECS = 5.0


async def flush_for_exit() -> None:
    """Flush queued spans before a hard exit, off the loop and bounded; never raises.

    The gateway leaves through ``os._exit``, which skips the SDK's own atexit
    flush, so without this the spans of the turns that ended during shutdown
    are lost. A no-op when tracing was never on.
    """
    import asyncio

    if _built is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(shutdown), timeout=EXIT_FLUSH_SECS)
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.warning("trace spans did not fully flush before exit")


def reset_for_testing() -> None:
    """Shut down and forget the tracer, so a test starts from the switch."""
    shutdown()
    with _counts_lock:
        _span_counts.clear()


def install_for_testing(state: TraceState | None) -> None:
    """Serve *state* (a tracer over an in-memory exporter, say) to every caller."""
    global _built, _built_inputs
    with _lock:
        _built, _built_inputs = state, None  # read from no config: any write re-reads
        _install_locked(state)


#: The registered applier, kept so :func:`watch_config` stays idempotent.
_config_sub: object = None


def _apply_config_change() -> None:
    """Re-read the switch after a ``telemetry.*`` write that moved one of its inputs.

    Nothing is shut down: it resolves again at once, on this worker thread,
    serving the one tracer this process builds (the kept one, or a first one
    when none was built yet) with the new caps, or ``None`` when the switch is
    off or the build fails. A turn still streaming keeps ending its spans on the
    tracer it started with.
    """
    global _resolved, _state
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        now: tuple[bool, object, object] | None = _inputs(KiroCrewConfig.load().telemetry)
    except Exception:
        now = None
    # Under the lock, which a build holds from its config read to its install:
    # a build that read the old value is compared here, one not yet started will
    # read the new value itself.
    with _lock:
        if not (_resolved and now != _built_inputs):
            return
        _state, _resolved = None, False
    # Re-read it now, on this worker thread, so no turn has to wait for it.
    current()


async def _on_config_change(change: object) -> None:
    import asyncio

    del change  # the inputs are compared instead, off the loop
    await asyncio.to_thread(_apply_config_change)


def watch_config() -> None:
    """Read the switch now, off the loop, and again on every ``telemetry.*`` write.

    Called once at gateway boot. Idempotent per process.
    """
    global _config_sub
    if _config_sub is not None:
        return
    from kiro_crew.config import live

    _config_sub = live.subscribe("telemetry", callback=_on_config_change, name="tracing")
    # Named like the metrics consent worker: it is the same kind of read (a
    # telemetry consent switch, off the loop), and the test floor joins threads
    # of that name before a test's data-home pin lifts.
    threading.Thread(target=current, name="kirocrew-telemetry-consent", daemon=True).start()
