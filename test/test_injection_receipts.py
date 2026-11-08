"""A turn's prompt records WHICH lessons it carried, as a local per-turn receipt.

The lesson tiers decide their kept rows from a ranking and a budget that change
every turn, so the set cannot be rebuilt afterwards. These cases pin the receipt:
the renderer that keeps the rows records their memory keys and the omitted count,
nothing is recorded outside a collection, the dispatch gate writes one line per
admitted turn that rendered a lesson tier, and an incognito or temporary session
writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew import context_omissions, injection_receipts
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import row_mid
from kiro_crew.lesson_validation import render_lesson_tier, render_withheld_tier
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import finished


def _rows(count: int) -> list[tuple[dict, str]]:
    return [({"key": f"lesson.{i:012x}"}, f"rule number {i} " + "x" * 80) for i in range(count)]


def _tier(entries, budget: int, kind: str = context_omissions.LESSON_RULES):
    return render_lesson_tier(
        entries,
        budget,
        header="[H]",
        footer="[F]",
        omission="[omitted {count} of {total} over {limit}]",
        omission_kind=kind,
    )


class TestTheRendererRecordsItsKeptRows:
    def test_an_over_budget_tier_records_exactly_the_rows_it_rendered(self):
        entries = _rows(10)
        with injection_receipts.collect() as receipts:
            block, omitted = _tier(entries, 400)
        kept = receipts[injection_receipts.LESSONS]["ids"]
        assert omitted > 0
        assert receipts[injection_receipts.LESSONS]["omitted"] == omitted
        assert len(kept) == 10 - omitted
        # Every recorded key is a row whose text is in the block, and no other is.
        for row, text in entries:
            assert (row["key"] in kept) == (f"- {text}" in block)

    def test_an_under_budget_tier_records_every_row(self):
        entries = _rows(3)
        with injection_receipts.collect() as receipts:
            _tier(entries, 0, context_omissions.LESSON_FINDINGS)
        assert receipts == {
            injection_receipts.FINDINGS: {
                "ids": [row["key"] for row, _ in entries],
                "omitted": 0,
            }
        }

    def test_a_render_outside_a_collection_records_nowhere(self):
        _tier(_rows(5), 200)
        with injection_receipts.collect() as receipts:
            pass
        assert receipts == {}

    def test_a_render_with_no_kind_records_nothing(self):
        with injection_receipts.collect() as receipts:
            render_lesson_tier(_rows(3), 0, header="[H]", footer="[F]", omission="{count}")
        assert receipts == {}

    def test_the_last_render_of_a_tier_is_the_one_recorded(self):
        entries = _rows(10)
        with injection_receipts.collect() as receipts:
            _tier(entries, 0)
            _, omitted = _tier(entries, 300)
        assert receipts[injection_receipts.LESSONS]["omitted"] == omitted
        assert len(receipts[injection_receipts.LESSONS]["ids"]) == 10 - omitted

    def test_a_withheld_tier_records_no_ids_and_every_row_omitted(self):
        with injection_receipts.collect() as receipts:
            render_withheld_tier(
                7,
                header="[H]",
                footer="[F]",
                notice="{total}",
                omission_kind=context_omissions.LESSON_FINDINGS,
            )
        assert receipts == {injection_receipts.FINDINGS: {"ids": [], "omitted": 7}}

    def test_a_row_with_no_key_counts_but_names_nothing(self):
        # The legacy JSONL store's rows carry no stable id.
        entries = [(object(), "rule a"), ({"key": 5}, "rule b"), ({"key": "lesson.a"}, "c")]
        with injection_receipts.collect() as receipts:
            _tier(entries, 0)
        assert receipts[injection_receipts.LESSONS] == {"ids": ["lesson.a"], "omitted": 0}

    def test_an_unknown_kind_is_a_programming_error(self):
        with pytest.raises(ValueError):
            injection_receipts.record("rules", [])

    def test_two_concurrent_collections_do_not_share_ids(self):
        async def compose(key: str) -> dict:
            with injection_receipts.collect() as receipts:
                await asyncio.sleep(0)
                injection_receipts.record(injection_receipts.LESSONS, [{"key": key}])
                await asyncio.sleep(0)
            return receipts

        async def both():
            return await asyncio.gather(compose("lesson.a"), compose("lesson.b"))

        first, second = asyncio.run(both())
        assert first[injection_receipts.LESSONS]["ids"] == ["lesson.a"]
        assert second[injection_receipts.LESSONS]["ids"] == ["lesson.b"]


def _line(receipts, **overrides):
    fields = dict(
        session_key="dashboard_chat-1-2",
        turn_index=4,
        user_msg_id="m-1",
        model="m",
        agent="kirocrew",
        phase="session_start",
        store="",
        now=datetime(2026, 10, 13, 15, 42, 22, 877000, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return injection_receipts.build_line(receipts, **fields)


class TestTheLine:
    def test_no_rendered_tier_writes_no_line(self):
        assert _line({}) is None

    def test_the_line_carries_keys_and_counts_and_no_text(self):
        line = _line({injection_receipts.LESSONS: {"ids": ["lesson.a"], "omitted": 625}})
        assert line == {
            "ts": "2026-10-13T15:42:22.877Z",
            "session_key": "dashboard_chat-1-2",
            "turn_index": 4,
            "user_msg_id": "m-1",
            "model": "m",
            "agent": "kirocrew",
            "surface": "dashboard",
            "phase": "session_start",
            "memory_store": "",
            "lessons": ["lesson.a"],
            "lessons_omitted": 625,
            "findings": [],
            "findings_omitted": 0,
            "turn_lessons": [],
        }

    def test_per_message_lessons_alone_write_a_line(self):
        line = _line({injection_receipts.TURN_LESSONS: {"ids": ["lesson.c"], "omitted": 0}})
        assert line["turn_lessons"] == ["lesson.c"]
        assert line["lessons"] == [] and "turn_lessons_omitted" not in line

    def test_a_channel_key_names_its_surface(self):
        line = _line(
            {injection_receipts.FINDINGS: {"ids": [], "omitted": 2}}, session_key="slack:C1:1"
        )
        assert line["surface"] == "slack"

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "Incognito"])
    def test_a_session_that_keeps_nothing_records_nothing(self, mode):
        assert not injection_receipts.records_session(mode)

    @pytest.mark.parametrize("mode", ["", None, "normal"])
    def test_an_ordinary_session_records(self, mode):
        assert injection_receipts.records_session(mode)

    def test_only_a_user_row_names_the_message(self):
        assert injection_receipts.user_row_mid({"role": "user", "meta": {"mid": "m-9"}}) == "m-9"
        assert injection_receipts.user_row_mid({"role": "subagent", "meta": {"mid": "x"}}) == ""
        assert injection_receipts.user_row_mid(None) == ""
        assert injection_receipts.user_row_mid({"role": "user"}) == ""


class TestTheShard:
    def test_lines_append_to_the_local_day_shard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: tmp_path / "injections")
        first = _line({injection_receipts.LESSONS: {"ids": ["lesson.a"], "omitted": 0}})
        second = _line({injection_receipts.LESSONS: {"ids": ["lesson.b"], "omitted": 1}})
        injection_receipts.write_line(first)
        injection_receipts.write_line(second)
        day = datetime(2026, 10, 13, 15, 42, 22, tzinfo=timezone.utc).astimezone()
        shard = tmp_path / "injections" / f"{day.strftime('%Y-%m-%d')}.jsonl"
        rows = [json.loads(text) for text in shard.read_text(encoding="utf-8").splitlines()]
        assert rows == [first, second]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_the_directory_is_private(self, tmp_path, monkeypatch):
        target = tmp_path / "injections"
        monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: target)
        injection_receipts.write_line(
            _line({injection_receipts.LESSONS: {"ids": [], "omitted": 0}})
        )
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o700

    def test_a_failed_write_never_raises(self, tmp_path, monkeypatch, caplog):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: blocker / "injections")
        injection_receipts.write_line(
            _line({injection_receipts.LESSONS: {"ids": [], "omitted": 0}})
        )
        assert "could not append" in caplog.text

    def test_a_scheduled_write_lands_off_the_loop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: tmp_path / "injections")

        async def go():
            injection_receipts.schedule_write(
                _line({injection_receipts.LESSONS: {"ids": ["lesson.a"], "omitted": 0}})
            )
            await asyncio.get_running_loop().shutdown_default_executor()

        asyncio.run(go())
        assert len(list((tmp_path / "injections").glob("*.jsonl"))) == 1


class TestTheTurnWritesTheReceipt:
    async def _turn(
        self,
        tmp_path,
        monkeypatch,
        *,
        memory_mode: str = "",
        trailing_system: bool = False,
        linked: str = "",
    ):
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "workspace"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        state = _make_state(tmp_path, context_builder=builder)

        def build(message, *_args, **_kwargs):
            # Runs on the composer's worker thread, as the real build does.
            _tier(_rows(2), 0)
            return ("[Learned corrections]\n- r\n[End of learned corrections]\n\n" + message, None)

        state.context_builder.build_message = MagicMock(side_effect=build)
        state.context_builder.ensure_store = AsyncMock(return_value=object())
        provider = MagicMock()

        async def stream(*_args, **_kwargs):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        state.sessions.consume_replay_suppression = MagicMock(return_value=False)
        state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
        state.sessions.record_failure = AsyncMock()
        state.broadcast_ws = MagicMock()
        written: list[dict] = []
        monkeypatch.setattr(injection_receipts, "schedule_write", written.append)
        monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
        monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())

        slot = state.get_or_create_slot("receipts-chat")
        if memory_mode:
            slot.memory_mode = memory_mode
        if linked:
            slot.linked_session_key = linked
        row = slot.append("user", "plan it")
        if trailing_system:
            # A row appended after the user's own, as a prompt-expansion chip is.
            slot.append("system", "Expanded a saved prompt")
        await asyncio.wait_for(chat_runner._run_chat(state, slot, "plan it"), 30)
        await finished(drain_background_tasks(state), "draining the background tasks", timeout=10)
        # The turn really composed: a refused or failed turn would write nothing too.
        state.context_builder.build_message.assert_called_once()
        assert any(m.get("role") == "assistant" for m in slot.messages)
        return written, row

    @pytest.mark.asyncio
    async def test_an_admitted_turn_writes_one_line_joined_to_its_message(
        self, tmp_path, monkeypatch
    ):
        written, row = await self._turn(tmp_path, monkeypatch)
        assert len(written) == 1
        line = written[0]
        assert line["session_key"] == "dashboard:receipts-chat"
        assert line["surface"] == "dashboard"
        assert line["lessons"] == ["lesson.000000000000", "lesson.000000000001"]
        assert line["lessons_omitted"] == 0
        assert line["user_msg_id"] == (row_mid(row) or "")
        assert line["user_msg_id"]

    @pytest.mark.asyncio
    async def test_a_linked_channel_session_records_the_key_it_runs_on(self, tmp_path, monkeypatch):
        written, _ = await self._turn(tmp_path, monkeypatch, linked="slack:C1:1700000000.000100")
        assert len(written) == 1
        assert written[0]["session_key"] == "slack:C1:1700000000.000100"
        assert written[0]["surface"] == "slack"

    @pytest.mark.asyncio
    async def test_a_row_appended_after_the_user_row_keeps_the_join(self, tmp_path, monkeypatch):
        written, row = await self._turn(tmp_path, monkeypatch, trailing_system=True)
        assert len(written) == 1
        assert written[0]["user_msg_id"] == row_mid(row)
        assert written[0]["user_msg_id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_a_session_that_keeps_nothing_writes_no_line(self, tmp_path, monkeypatch, mode):
        written, _ = await self._turn(tmp_path, monkeypatch, memory_mode=mode)
        assert written == []
