"""A lesson the agent saves is drawn in the conversation that saved it.

``learn_add`` posts to ``/api/lessons``; when the write changed the store the route
appends ONE ``card`` row (surface ``lesson``) to the caller's chat. These tests pin
which writes earn a row (inserted / enriched, never unchanged / deduped / refused),
that every write is its own row, that only the agent's MCP path draws one (the
Memory tab's own Add does not), that the rule is redacted before it reaches the
browser, and that a failure to draw never changes the route's answer.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.dashboard import chat_cards
from kiro_crew.history import ConversationLog
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

SLOT = "chat-1"
AGENT_KEY = "dashboard_chat-1"


class _Slot:
    def __init__(self, key: str = SLOT) -> None:
        self.key = key
        self.messages: list[dict[str, Any]] = []

    def append(self, role: str, content: str, cls: str, meta: dict | None = None) -> dict:
        row = {"role": role, "content": content, "cls": cls, "meta": dict(meta or {})}
        row["meta"]["mid"] = f"m{len(self.messages)}"
        self.messages.append(row)
        return row


class _State:
    def __init__(self, slot: _Slot | None) -> None:
        self._slot = slot

    def get_slot(self, key: str) -> _Slot | None:
        return self._slot if self._slot is not None and key == self._slot.key else None


def _lesson_rows(slot: _Slot) -> list[dict]:
    return [
        m["meta"]["card"]
        for m in slot.messages
        if m["role"] == "card" and m["meta"].get("card", {}).get("surface") == "lesson"
    ]


class TestRecordLessonSaved:
    @pytest.mark.parametrize("outcome", ["inserted", "enriched"])
    def test_a_write_that_changed_the_store_draws_one_row(self, outcome: str) -> None:
        slot = _Slot()
        chat_cards.record_lesson_saved(_State(slot), SLOT, "always use pnpm", outcome)

        rows = _lesson_rows(slot)
        assert len(rows) == 1
        assert rows[0]["rule"] == "always use pnpm"
        assert rows[0]["status"] == outcome
        assert rows[0]["slot"] == SLOT
        assert rows[0]["id"]

    @pytest.mark.parametrize("outcome", ["unchanged", "deduped", "refused", "", None])
    def test_a_write_that_changed_nothing_draws_nothing(self, outcome: Any) -> None:
        slot = _Slot()
        chat_cards.record_lesson_saved(_State(slot), SLOT, "always use pnpm", outcome)
        assert slot.messages == []

    def test_every_write_is_its_own_row_even_for_the_same_rule(self) -> None:
        slot = _Slot()
        state = _State(slot)
        chat_cards.record_lesson_saved(state, SLOT, "always use pnpm", "inserted")
        chat_cards.record_lesson_saved(state, SLOT, "prefer tabs", "inserted")
        chat_cards.record_lesson_saved(state, SLOT, "always use pnpm", "enriched")

        rows = _lesson_rows(slot)
        assert [r["rule"] for r in rows] == ["always use pnpm", "prefer tabs", "always use pnpm"]
        assert len({r["id"] for r in rows}) == 3

    def test_the_rule_is_redacted_before_the_browser_draws_it(self) -> None:
        slot = _Slot()
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        chat_cards.record_lesson_saved(_State(slot), SLOT, f"deploy with key {secret}", "inserted")

        (row,) = slot.messages
        assert secret not in row["meta"]["card"]["rule"]
        assert secret not in row["content"]

    def test_no_slot_draws_nothing_and_does_not_raise(self) -> None:
        chat_cards.record_lesson_saved(_State(None), SLOT, "always use pnpm", "inserted")

    def test_an_append_failure_does_not_raise(self) -> None:
        slot = _Slot()
        slot.append = MagicMock(side_effect=RuntimeError("disk full"))  # type: ignore[method-assign]
        chat_cards.record_lesson_saved(_State(slot), SLOT, "always use pnpm", "inserted")


@pytest.mark.asyncio
class TestLessonsRouteDrawsTheRow:
    """Driven through ``api_lessons_create`` so the hook's placement is pinned too."""

    async def _post(
        self,
        outcome: LessonWriteOutcome,
        *,
        session_key: str = AGENT_KEY,
        internal: bool = True,
        slot_key: str = SLOT,
        record: MagicMock | None = None,
    ) -> tuple[Any, MagicMock]:
        from kiro_crew.dashboard.handlers import cron

        request = MagicMock()
        state = MagicMock()
        state.conversation_log = ConversationLog()
        state._background_tasks = set()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": session_key}
        flags = {"internal_auth": True} if internal else {}
        request.get = lambda key, default=None: flags.get(key, default)
        attach_body(request, {"rule": "always use pnpm", "category": "knowledge"})
        vs = MagicMock()
        vs.embed_lesson.return_value = None
        vs.write_lesson.return_value = LessonWriteResult(outcome)
        record = record or MagicMock()
        with (
            patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(
                cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))
            ),
            patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vs)),
            patch.object(cron, "_embed_lesson_bounded", return_value=None),
            patch.object(cron, "_sel"),
            patch(
                "kiro_crew.dashboard.session_control.caller_slot_key",
                return_value=slot_key,
            ),
            patch.object(chat_cards, "load_conversation_window", new=AsyncMock()),
            patch.object(chat_cards, "record_lesson_saved", record),
        ):
            resp = await cron.api_lessons_create(request)
        return resp, record

    async def test_an_agent_save_draws_the_row_in_its_slot(self) -> None:
        resp, record = await self._post(LessonWriteOutcome.INSERTED)
        assert resp.status == 200
        record.assert_called_once()
        _state, slot_key, rule, outcome = record.call_args.args
        assert (slot_key, rule, outcome) == (SLOT, "always use pnpm", "inserted")

    async def test_the_memory_tab_add_draws_nothing(self) -> None:
        _resp, record = await self._post(
            LessonWriteOutcome.INSERTED, session_key="dashboard:ui", internal=False
        )
        record.assert_not_called()

    async def test_a_caller_without_the_internal_secret_draws_nothing(self) -> None:
        _resp, record = await self._post(LessonWriteOutcome.INSERTED, internal=False)
        record.assert_not_called()

    async def test_a_key_with_no_live_slot_draws_nothing(self) -> None:
        _resp, record = await self._post(LessonWriteOutcome.INSERTED, slot_key="")
        record.assert_not_called()

    async def test_a_drawing_failure_keeps_the_route_answer(self) -> None:
        record = MagicMock(side_effect=RuntimeError("boom"))
        resp, _ = await self._post(LessonWriteOutcome.INSERTED, record=record)
        record.assert_called_once()
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "ok": True,
            "outcome": "inserted",
            "reason": None,
            "superseded": [],
        }

    async def test_the_helper_swallows_a_failure(self) -> None:
        from kiro_crew.dashboard.handlers import cron

        request = MagicMock()
        request.get = lambda key, default=None: True if key == "internal_auth" else default
        with (
            patch("kiro_crew.dashboard.session_control.caller_slot_key", return_value=SLOT),
            patch.object(
                chat_cards,
                "load_conversation_window",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await cron._surface_lesson_saved(request, MagicMock(), AGENT_KEY, "r", "inserted")
