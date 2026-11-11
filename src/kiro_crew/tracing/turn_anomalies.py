"""One JSON line per ACP turn that ran abnormally slow or ended badly.

Both turn stream functions (see :mod:`kiro_crew.tracing.turns`) pass their
event stream through :func:`watched`. It times the turn from Crew's side:

    start -> first event -> first model output -> ... -> end

A normal turn writes nothing. A turn logs one WARNING line, tagged
:data:`TAG` and carrying a JSON object, when any of these holds:

- the first model output (text, thought or tool call) took longer than
  :data:`FIRST_OUTPUT_SLOW_SECS`, or never came on a turn that ran that long;
- the longest silence while the model, not a tool or the user, held the turn
  was longer than :data:`MODEL_GAP_SLOW_SECS`;
- the execution layer reported a retry wait, a model fallback or an MCP server
  that failed to start;
- the turn stopped on a timeout, a stale recovery or an ``error:`` reason, or
  the stream raised.

A line holds timings, counts, the stop reason and the exception class name.
Never prompt text, tool input or output, or an exception message.

The line goes through the normal logger, so in the gateway it lands in
``gateway.log``, whose own rollover bounds the disk it takes
(``grep turn-anomaly gateway.log`` finds the lines). One session writes at most
:data:`RATE_LIMIT_LINES` lines per :data:`RATE_LIMIT_WINDOW_SECS`; the next line
it writes after that names how many it dropped (``suppressed``).

Best-effort throughout: a failure here is logged at debug and the turn goes on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from kiro_crew.tracing.turns import _MAX_KEY_LEN, _chain_attr

logger = logging.getLogger(__name__)

FIRST_OUTPUT_SLOW_SECS = 30.0
MODEL_GAP_SLOW_SECS = 60.0
RATE_LIMIT_LINES = 5
RATE_LIMIT_WINDOW_SECS = 60.0
#: Sessions the rate limiter remembers; past this it forgets them all.
_MAX_TRACKED_SESSIONS = 256
#: Open tool calls one turn tracks; past this the set is dropped.
_MAX_OPEN_TOOLS = 1024
#: The fixed word every anomaly line starts with, for grep.
TAG = "turn-anomaly"

_OUTPUT_KINDS = frozenset({"text_chunk", "thinking_chunk", "tool_call"})
#: ``acp.types.TERMINAL_TOOL_STATUSES``, spelled out like ``tracing.turns``'s stop
#: reasons so this module does not load the ACP package; ``test_turn_anomalies`` pins it.
_TOOL_TERMINAL = frozenset({"completed", "failed", "cancelled", "canceled", "refused"})
_TOOL_EVENT_KINDS = frozenset({"tool_result", "tool_call_update"})
_USER_ASK_KINDS = frozenset({"permission_request", "elicitation_request", "mcp_oauth_request"})
_ASK_ENDING_KINDS = frozenset({"complete", "mcp_server_initialized", "mcp_server_init_failure"})
_BAD_STOPS = frozenset({"timeout", "stale_recover"})


#: The clock every timing reads; a test swaps it for a fake.
_clock = time.monotonic


def _now() -> float:
    return _clock()


_lock = threading.Lock()
# session key -> (window start, lines written in it, lines suppressed since last write)
_rate: dict[str, list[float]] = {}


def _admit(session_key: str, now: float) -> tuple[bool, int]:
    """Whether *session_key* may write a line now, and how many it dropped before."""
    with _lock:
        if session_key not in _rate and len(_rate) >= _MAX_TRACKED_SESSIONS:
            _rate.clear()
        entry = _rate.setdefault(session_key, [now, 0.0, 0.0])
        if now - entry[0] >= RATE_LIMIT_WINDOW_SECS:
            entry[0], entry[1] = now, 0.0
        if entry[1] >= RATE_LIMIT_LINES:
            entry[2] += 1
            return False, 0
        entry[1] += 1
        suppressed, entry[2] = int(entry[2]), 0.0
        return True, suppressed


def write(record: dict[str, Any]) -> bool:
    """Write *record* as one line, subject to the rate limit. ``True`` if written."""
    allowed, suppressed = _admit(str(record.get("session_key", "")), _now())
    if not allowed:
        return False
    if suppressed:
        record = {**record, "suppressed": suppressed}
    logger.warning("%s %s", TAG, json.dumps(record, sort_keys=True, default=str))
    return True


def _closed(exc: BaseException) -> bool:
    """A consumer closing or cancelling the stream, not a failure."""
    return isinstance(exc, (GeneratorExit, asyncio.CancelledError))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class TurnTimer:
    """One turn's client-side timings. Feed it events; :meth:`finish` decides."""

    def __init__(self, source: object) -> None:
        self._source = source
        self.start = _now()
        self.first_event: float | None = None
        self.first_output: float | None = None
        self._last = self.start
        self.max_model_gap = 0.0
        self._open_tools: set[str] = set()
        self._user_ask = False
        self.notices: dict[str, int] = {}
        self.tool_calls = 0
        self.events = 0
        self.stop_reason = ""
        self._finished = False

    def _note_gap(self, now: float) -> None:
        # A silence while a tool runs or the user is asked is not the model's.
        if not self._open_tools and not self._user_ask:
            self.max_model_gap = max(self.max_model_gap, now - self._last)
        self._last = now

    def observe(self, event: Any) -> None:
        now = _now()
        self._note_gap(now)
        self.events += 1
        if self.first_event is None:
            self.first_event = now
        kind = getattr(event, "kind", "")
        if kind in _USER_ASK_KINDS:
            self._user_ask = True
        elif kind in _OUTPUT_KINDS or kind in _TOOL_EVENT_KINDS or kind in _ASK_ENDING_KINDS:
            # The model, a tool or the MCP server the user was signing in to
            # moved, so the ask was answered. A status or subagent frame
            # meanwhile does not end it.
            self._user_ask = False
        if kind in _OUTPUT_KINDS and self.first_output is None:
            self.first_output = now
        call_id = getattr(event, "tool_call_id", "") or ""
        if kind == "tool_call":
            self.tool_calls += 1
            # Backend-chosen ids: one longer than the tracer keeps is not tracked.
            if 0 < len(call_id) <= _MAX_KEY_LEN:
                if len(self._open_tools) >= _MAX_OPEN_TOOLS:
                    self._open_tools.clear()
                self._open_tools.add(call_id)
        elif kind in _TOOL_EVENT_KINDS and (
            getattr(event, "tool_final", False)
            or getattr(event, "tool_status", "") in _TOOL_TERMINAL
        ):
            self._open_tools.discard(call_id)
        elif kind == "structured_status":
            if getattr(getattr(event, "status", None), "wait_reason", "") == "retry_wait":
                self._count("retry_wait")
        elif kind == "model_fallback":
            self._count("model_fallback")
        elif kind == "mcp_server_init_failure":
            self._count("mcp_server_init_failure")
        elif kind == "complete":
            self.stop_reason = str(getattr(event, "stop_reason", "") or "")

    def _count(self, name: str) -> None:
        self.notices[name] = self.notices.get(name, 0) + 1

    def reasons(self, exc: BaseException | None) -> list[str]:
        """Why this turn is abnormal; empty for a normal turn."""
        end = _now()
        out: list[str] = []
        if self.first_output is not None:
            if self.first_output - self.start > FIRST_OUTPUT_SLOW_SECS:
                out.append("slow_first_output")
        elif end - self.start > FIRST_OUTPUT_SLOW_SECS:
            out.append("no_output")
        if self.max_model_gap > MODEL_GAP_SLOW_SECS:
            out.append("slow_model_gap")
        if self.notices:
            out.append("notices")
        if self.stop_reason in _BAD_STOPS or self.stop_reason.startswith("error:"):
            out.append("bad_stop")
        if exc is not None and not _closed(exc):
            out.append("exception")
        return out

    def record(self, reasons: list[str], exc: BaseException | None) -> dict[str, Any]:
        end = _now()
        first_event = None if self.first_event is None else self.first_event - self.start
        first_output = None if self.first_output is None else self.first_output - self.start
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_key": _chain_attr(self._source, ("_session_key",))[:256],
            "backend": str(getattr(self._source, "backend", "") or "")[:64],
            "model": _chain_attr(self._source, ("_resolved_model_id", "_model"))[:128],
            "reasons": reasons,
            "first_event_s": _round(first_event),
            "first_output_s": _round(first_output),
            "max_model_gap_s": _round(self.max_model_gap),
            "total_s": _round(end - self.start),
            "events": self.events,
            "tool_calls": self.tool_calls,
            "notices": self.notices,
            "stop_reason": self.stop_reason[:128],
            "error": "" if exc is None or _closed(exc) else type(exc).__name__,
        }

    def finish(self, exc: BaseException | None) -> None:
        """Decide and maybe write once; later calls do nothing."""
        if self._finished:
            return
        self._finished = True
        try:
            # The silence up to the end counts: a turn that timed out waiting
            # on the model ends on its longest gap.
            self._note_gap(_now())
            reasons = self.reasons(exc)
            if reasons:
                write(self.record(reasons, exc))
        except Exception:
            logger.debug("turn anomaly log failed", exc_info=True)


async def watched(source: object, events: AsyncGenerator[Any, None]) -> AsyncGenerator[Any, None]:
    """Yield *events* unchanged, writing one line at the end if the turn was abnormal."""
    timer = TurnTimer(source)
    error: BaseException | None = None
    try:
        async with aclosing(events) as inner:
            async for event in inner:
                try:
                    timer.observe(event)
                except Exception:
                    logger.debug("turn anomaly observe failed", exc_info=True)
                if getattr(event, "kind", "") == "complete":
                    # The turn ends here: a consumer that breaks on complete
                    # closes the stream only after its own post-turn work.
                    timer.finish(None)
                yield event
    except BaseException as exc:
        error = exc
        raise
    finally:
        timer.finish(error)


def reset_for_testing() -> None:
    """Forget the rate limiter. Test isolation only."""
    with _lock:
        _rate.clear()
