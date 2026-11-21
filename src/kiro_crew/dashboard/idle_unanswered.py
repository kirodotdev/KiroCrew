"""The dashboard's answers to the idle sweep's unanswered-turn questions.

``kiro_crew.session_cleanup_unanswered`` asks, before an idle reset, whether the
newest conversational row of the expiring session is a user prompt that never
got a reply, and after the reset asks to record that prompt's turn as failed.
Both answers read the live slot when the session has one, else the transcript
on disk (a channel or cron session), and write the same row a turn that raised
before it started writes (``queue_cycle.FAILED_BEFORE_START_TEXT``).

A slot that is running, reserved for admission or holding queued work is not
unanswered: its prompt is about to be answered. The recorder re-reads the tail
and marks only the very prompt the probe found, so a prompt sent while the
reset was awaited is never marked.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_runner import (
    FAILED_BEFORE_START_TEXT,
    failed_before_start_meta,
)
from kiro_crew.dashboard.chat_utils_injected_rows import is_system_injection
from kiro_crew.dashboard.chat_utils_slots import dashboard_slot_key
from kiro_crew.executors import maintenance_executor
from kiro_crew.history import ConversationLog
from kiro_crew.history_schema import _parse_transcript_ts
from kiro_crew.session_cleanup_unanswered import Unanswered

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

#: Rows the dashboard draws but the conversation does not contain.
_DISPLAY_ONLY_ROLES = frozenset({"notice", "card"})
#: How many transcript rows the disk path reads from the end.
_TAIL_ROWS = 10


def _is_prompt(row: dict[str, Any]) -> bool:
    """A real user prompt: not a steer and not a system injection."""
    if row.get("role") != "user":
        return False
    meta = row.get("meta")
    if isinstance(meta, dict) and (
        meta.get("steer") or meta.get("steerState") in {"requeued", "retracted"}
    ):
        return False
    return not is_system_injection(str(row.get("content") or ""))


def unanswered_prompt(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest conversational row when it is an unanswered prompt, else ``None``."""
    for row in reversed(messages):
        if not isinstance(row, dict) or row.get("role") in _DISPLAY_ONLY_ROLES:
            continue
        return row if _is_prompt(row) else None
    return None


def _identity(row: dict[str, Any]) -> object:
    return (row.get("ts"), row.get("content"))


def _age(row: dict[str, Any]) -> float:
    ts = row.get("ts")
    if isinstance(ts, (int, float)):
        return max(0.0, time.time() - float(ts))
    if isinstance(ts, str) and ts:
        dt = _parse_transcript_ts(ts)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    return 0.0


class DashboardUnansweredHooks:
    """``UnansweredHooks`` over the dashboard's slots and transcript."""

    def __init__(self, state: DashboardState) -> None:
        self._state = state

    def _slot(self, key: str) -> Any | None:
        slot_key = dashboard_slot_key(key)
        return self._state.get_slot(slot_key) if slot_key else None

    def _log(self) -> ConversationLog:
        return self._state.conversation_log or ConversationLog()

    async def _disk(self, fn: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(maintenance_executor(), fn)

    async def probe(self, key: str) -> Unanswered | None:
        slot = self._slot(key)
        if slot is not None:
            if slot.running or slot.queue_depth:
                return None
            row = unanswered_prompt(list(slot.messages))
        else:
            log = self._log()

            def _read() -> dict[str, Any] | None:
                path = log._path(key)
                if not path.exists():
                    return None
                return unanswered_prompt(log._read_tail_messages(path, _TAIL_ROWS, None))

            row = await self._disk(_read)
        return None if row is None else Unanswered(age=_age(row), prompt=_identity(row))

    async def mark_failed(self, key: str, prompt: object) -> None:
        slot = self._slot(key)
        if slot is not None:
            if slot.running or slot.queue_depth:
                return
            row = unanswered_prompt(list(slot.messages))
            if row is not None and _identity(row) == prompt:
                slot.append(
                    "error",
                    FAILED_BEFORE_START_TEXT,
                    "msg msg-err",
                    meta=failed_before_start_meta(),
                )
            return
        log = self._log()

        def _append_if_still_unanswered() -> None:
            with log._locked(key):
                path = log._path(key)
                if not path.exists():
                    return
                row = unanswered_prompt(log._read_tail_messages(path, _TAIL_ROWS, None))
                if row is not None and _identity(row) == prompt:
                    log.append(
                        key,
                        role="error",
                        content=FAILED_BEFORE_START_TEXT,
                        cls="msg msg-err",
                        extra_meta=failed_before_start_meta(),
                    )

        await self._disk(_append_if_still_unanswered)


def wire_unanswered_turn_hooks(state: DashboardState) -> None:
    """Hand ``SessionManager`` these hooks; a manager double without the setter
    is left alone and records no unanswered turn."""
    setter = getattr(state.sessions, "set_unanswered_turn_hooks", None)
    if callable(setter):
        setter(DashboardUnansweredHooks(state))
