"""``kirocrew.tool.call.duration`` -- how long each tool round-trip takes.

The turn histogram already reports how long a whole agent loop takes, which
makes a slow turn visible but never says which part was slow. A turn is model
calls plus every tool round-trip, so without this instrument a 4-minute turn is
indistinguishable between "the model was slow" and "one shell command took four
minutes", and the tool population that dominates real latency cannot be seen at
all.

**Cardinality: the tool NAME is never an attribute.** MCP tool names are
unbounded -- any server the user installs contributes its own -- and the
recorder caches one instrument per distinct name with no eviction, so a
name-valued attribute is a cardinality bomb (``metrics/schema.py``). The label
is a normalised ``tool_kind`` instead, and normalisation is not optional
politeness: the ACP ``kind`` field arrives verbatim from the agent, which
``hooks.py`` already documents when it explains why its auto-approve decision is
an ALLOW-list rather than a denylist. :func:`classify_tool_kind` applies the
same reasoning here -- a kind outside :data:`TOOL_KINDS` becomes ``other``, so
an agent cannot mint series by inventing kinds.

An MCP tool is classified ``mcp`` whatever kind it claims. The kind an MCP
server reports is its own vocabulary, so keeping it would put third-party
strings in the allowlist's path; and "this call left the process over MCP" is
the more useful fact anyway, since it is the difference between a builtin and a
round-trip through a server that can be slow or wedged.

**Two layers, one registry, exactly one sample.** There is no single choke point
to instrument, because the two backends parse tool frames in different places.
The kiro backend runs on ``AcpRuntime`` + ``AcpSessionHandle``, which parses
through the shared ``acp/_dispatch.py`` builders; the claude backend (and the app
worker pools that build a client directly, e.g. ``knowledge/llm_pool``) stays on
``acp/client.py``, which re-implements the same shaping inline and never calls
``parse_session_update``. ``providers/acp.py`` constructs an ``AcpClient`` and
then swaps in an ``AcpSessionProvider`` at startup for the kiro path, so both
parsers are live in a normal install and both are instrumented here.

Every surface -- dashboard, Slack, Discord, cron, subagents, task runner,
workflow -- sits DOWNSTREAM of those two: they consume the emitted ``AcpEvent``
stream rather than re-parsing frames. So instrumenting both parsers covers every
surface, and nothing else needs a call site.

The start times live in ONE process-global registry keyed by ``toolCallId``, and
a finish POPS its entry: a call with no recorded start emits nothing. So if a
frame is ever seen by both layers, the first finish records it and the second is
a no-op -- the layering cannot double-count.

**Why not reuse the watchdog's existing dispatch clock.**
``acp/liveness.py::ToolCallState`` already stamps ``dispatch_ts`` on a tool call,
and ``AcpSessionHandle`` clears it on the result. It is deliberately NOT reused:
``_inflight_tool`` is a SINGLE SLOT holding the most recent call, which is the
right shape for stall attribution (the oracle only asks "what are we waiting on
now") and the wrong shape for a histogram -- interleaved or overlapping tool
calls overwrite each other, so durations would be attributed to the wrong call
and some would never be recorded at all. A ``toolCallId``-keyed registry is what
makes every call its own sample. It also exists only on the kiro path, so it
could not serve the claude one.

Every function is best-effort and swallows its own failures: a tool call must
never fail because its telemetry did.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: Registered in ``metrics/provider.py``'s bucket table -- a tool call spans
#: sub-millisecond file reads to multi-minute builds, so it carries its own
#: boundary family rather than sharing the request or turn ones.
TOOL_CALL_METRIC = "kirocrew.tool.call.duration"

#: ``tool_kind`` for a call that went out over MCP, whatever kind it reported.
TOOL_KIND_MCP = "mcp"

#: ``tool_kind`` for a kind this build does not recognise -- including the ACP
#: protocol's own literal ``"other"``, an empty kind, and anything an agent
#: invents. Folding them together is what bounds the series.
TOOL_KIND_OTHER = "other"

#: The recognised ACP tool kinds, plus Kiro Crew's own ``client_built_in`` (used
#: when kiro-cli's security filter cancels a tool use). Union of the two sets
#: ``hooks.py`` maintains for its approval gate, so this allowlist and the gate's
#: vocabulary cannot silently diverge.
TOOL_KINDS = frozenset(
    {
        "read",
        "fetch",
        "search",
        "edit",
        "write",
        "create",
        "delete",
        "move",
        "execute",
        "think",
        "switch_mode",
        "client_built_in",
        TOOL_KIND_MCP,
        TOOL_KIND_OTHER,
    }
)

#: Terminal ACP tool-call statuses. A non-terminal update (``pending``,
#: ``in_progress``) is not an outcome, so it records nothing and leaves the
#: call's start time in place for its real completion.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"

#: Every value ``outcome`` can carry. Exactly the terminal statuses: an unknown
#: status is not folded into ``other`` because :func:`record_tool_call_finished`
#: returns without emitting for anything outside this set, so no such sample can
#: exist. (Contrast :data:`TOOL_KIND_OTHER`, which IS reachable -- an unknown
#: kind still belongs to a call whose duration is recorded.)
OUTCOMES = frozenset({OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_CANCELLED})

_TERMINAL_STATUSES = frozenset({OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_CANCELLED})

# A turn cannot legitimately hold this many tool calls open at once, so a
# registry that reaches the cap is evidence of frames whose completion never
# arrived (a killed process, a cancelled turn). It is dropped wholesale rather
# than grown for the life of the process -- the same trade ``AcpClient`` makes
# for its ``_skill_read_noted`` bookkeeping. The cost is missing samples for the
# calls that were in flight, never a leak.
_MAX_OPEN_CALLS = 4096

_lock = threading.Lock()
_open_calls: dict[str, tuple[float, str]] = {}


def _registry_key(tool_call_id: str, scope: str) -> str:
    """Origin-scoped registry key, spelled exactly as ``_dispatch``'s caches are.

    ``toolCallId`` is assigned by the BACKEND and is unique only within one
    backend session, while this registry is process-global and one ``AcpRuntime``
    hosts many sessions. Without the scope two sessions reusing an id collide:
    the second start is dropped as a duplicate and the first finish pops the
    shared entry, so one sample is misattributed and the other is lost.

    ``acp/_dispatch.py`` already solved this for its tool-input caches with
    ``_ck = f"{cache_scope}|{tool_call_id}"``; this is the same key so the two
    cannot disagree about what "the same tool call" means.
    """
    return f"{scope}|{tool_call_id}" if scope else tool_call_id


def classify_tool_kind(kind: str | None, *, mcp_server_name: str | None = None) -> str:
    """Map an ACP ``kind`` onto a member of :data:`TOOL_KINDS`.

    ``mcp_server_name`` wins when set: see the module docstring for why an MCP
    call is labelled by its transport rather than by the kind it claims.
    """
    if mcp_server_name:
        return TOOL_KIND_MCP
    if kind and kind in TOOL_KINDS:
        return kind
    return TOOL_KIND_OTHER


def note_tool_call_started(
    tool_call_id: str | None,
    *,
    kind: str | None = None,
    mcp_server_name: str | None = None,
    scope: str = "",
) -> None:
    """Record when *tool_call_id* started, with its normalised kind.

    *scope* is the emitting session's cache scope -- see :func:`_registry_key`
    for why a process-global registry cannot key on the raw backend id.

    Idempotent per scoped id: an id already open keeps its ORIGINAL start time,
    so the ``tool_call_update`` refinements that follow a call (and the second
    layer, if it also sees the frame) cannot reset the clock and shrink the
    measurement.
    """
    if not tool_call_id:
        return
    try:
        resolved = classify_tool_kind(kind, mcp_server_name=mcp_server_name)
        key = _registry_key(tool_call_id, scope)
        with _lock:
            if key in _open_calls:
                return
            if len(_open_calls) >= _MAX_OPEN_CALLS:
                _open_calls.clear()
            _open_calls[key] = (time.perf_counter(), resolved)
    except Exception:
        logger.debug("tool call start bookkeeping failed", exc_info=True)


def record_tool_call_finished(
    tool_call_id: str | None, *, status: str | None, scope: str = ""
) -> None:
    """Emit one ``kirocrew.tool.call.duration`` sample; never raises.

    A non-terminal *status* returns without touching the registry, so the call
    is still measured when its real completion arrives. A terminal status POPS
    the entry, which is what makes the two instrumented layers add up to exactly
    one sample per call. *scope* must match the one its start was recorded under.
    """
    if not tool_call_id or not status or status not in _TERMINAL_STATUSES:
        return
    try:
        with _lock:
            started = _open_calls.pop(_registry_key(tool_call_id, scope), None)
        if started is None:
            return
        started_at, resolved_kind = started
        # ``perf_counter``, not ``monotonic``: on Windows the latter advances in
        # ~15.6ms ticks, so a call that completes inside one tick measures exactly
        # 0.0 and the guard below drops it. That silently hid EVERY sub-tick tool
        # call on Windows -- which is most cached reads -- and turned the whole
        # platform's fast population into no data. ``perf_counter`` is the
        # highest-resolution clock available on every platform and is equally
        # monotonic, so the guard keeps meaning "unmeasurable" rather than "fast".
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        if elapsed_ms <= 0:
            # Same rule as ``metrics/turns.py``: an absent sample reads as "no
            # data", while a recorded 0 renders as a plausible 0ms tool call.
            return
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().histogram(
            TOOL_CALL_METRIC,
            elapsed_ms,
            unit="ms",
            attrs={"tool_kind": resolved_kind, "outcome": status},
            description="Tool call round-trip duration (ms), by tool kind.",
        )
    except Exception:
        logger.debug("tool call duration emit failed", exc_info=True)


def open_call_count() -> int:
    """Number of tool calls currently awaiting a terminal status (tests only)."""
    with _lock:
        return len(_open_calls)


def reset_open_calls() -> None:
    """Drop all in-flight bookkeeping. For test isolation, not production use."""
    with _lock:
        _open_calls.clear()
