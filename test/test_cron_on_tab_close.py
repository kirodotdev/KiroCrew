"""A cron job's ``on_tab_close="fresh"``: a run reopening its closed tab shows it empty.

The rows stay in the ``cron:<id>`` transcript below the tab's display floor
(``dashboard/display_floor.py``); the default ``resume`` reopens with them.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.cron import CronJob
from kiro_crew.cron_service.fields import build_job
from kiro_crew.cron_service.store import _job_from_record, job_record
from kiro_crew.dashboard import cron_inject
from kiro_crew.dashboard.chat_persistence_parts.save_transaction import save_slot_off_loop
from kiro_crew.dashboard.display_floor import DISPLAY_FLOOR_KEY
from kiro_crew.history import _safe_key
from kiro_crew.testing.wait import async_wait_until

JOB_ID = "report1"
KEY = f"cron:{JOB_ID}"


def _ts(i: int) -> str:
    return f"2026-11-01T00:00:{i:02d}.000000+00:00"


def _write_transcript(tmp_path: Any, count: int, **meta: Any) -> None:
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-11-01T00:00:00", **meta})]
    lines += [
        json.dumps(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}", "ts": _ts(i)}
        )
        for i in range(count)
    ]
    (tmp_path / f"{_safe_key(KEY)}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    # The tab's workspace is resolved from the agent config, which is not under test.
    monkeypatch.setattr(cron_inject, "cron_tab_workspace", lambda job: "")
    return st


def _job(on_tab_close: str) -> CronJob:
    return CronJob(id=JOB_ID, name="queue report", message="report", on_tab_close=on_tab_close)


async def _detail(state: Any, query: str = "") -> dict[str, Any]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/cron-{JOB_ID}{query}")
        assert resp.status == 200
        return await resp.json()


class TestReopenAfterClose:
    @pytest.mark.asyncio
    async def test_fresh_reopens_empty_and_keeps_the_rows(self, state: Any, tmp_path: Any) -> None:
        _write_transcript(tmp_path, 6, closed=True, closed_id="c1", person_close_id="c1")

        await cron_inject.ensure_cron_slot(state, _job("fresh"))

        slot = state.get_slot(f"cron-{JOB_ID}")
        assert slot.linked_session_key == KEY
        assert slot.messages == []
        assert state.conversation_log.get_metadata(KEY)[DISPLAY_FLOOR_KEY] == _ts(5)
        assert len(state.conversation_log.read_messages(KEY)) == 6
        body = await _detail(state, "?limit=50")
        assert body["messages"] == []
        assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_resume_reopens_with_the_rows(self, state: Any, tmp_path: Any) -> None:
        _write_transcript(tmp_path, 6, closed=True, closed_id="c1", person_close_id="c1")

        await cron_inject.ensure_cron_slot(state, _job("resume"))

        slot = state.get_slot(f"cron-{JOB_ID}")
        assert [m["content"] for m in slot.messages] == [f"m{i}" for i in range(6)]
        assert DISPLAY_FLOOR_KEY not in state.conversation_log.get_metadata(KEY)

    @pytest.mark.asyncio
    async def test_resume_does_not_bring_back_rows_a_fresh_reopen_hid(
        self, state: Any, tmp_path: Any
    ) -> None:
        _write_transcript(tmp_path, 6, closed=True, closed_id="c1", **{DISPLAY_FLOOR_KEY: _ts(3)})

        await cron_inject.ensure_cron_slot(state, _job("resume"))

        assert [m["content"] for m in state.get_slot(f"cron-{JOB_ID}").messages] == ["m4", "m5"]

    @pytest.mark.asyncio
    async def test_fresh_after_a_close_the_person_did_not_make_hydrates_as_before(
        self, state: Any, tmp_path: Any
    ) -> None:
        # The idle sweep's archive stamps the close but is not the person's.
        _write_transcript(tmp_path, 4, closed=True, closed_id="c1")

        await cron_inject.ensure_cron_slot(state, _job("fresh"))

        assert len(state.get_slot(f"cron-{JOB_ID}").messages) == 4
        assert DISPLAY_FLOOR_KEY not in state.conversation_log.get_metadata(KEY)

    @pytest.mark.asyncio
    async def test_fresh_after_a_close_with_no_id_hydrates_as_before(
        self, state: Any, tmp_path: Any
    ) -> None:
        # An older build's close, or the stamp a failed resume restores, has no id.
        _write_transcript(tmp_path, 4, closed=True)

        await cron_inject.ensure_cron_slot(state, _job("fresh"))

        assert len(state.get_slot(f"cron-{JOB_ID}").messages) == 4
        assert DISPLAY_FLOOR_KEY not in state.conversation_log.get_metadata(KEY)

    def test_a_close_replaced_since_the_read_writes_no_floor(
        self, state: Any, tmp_path: Any
    ) -> None:
        _write_transcript(tmp_path, 4, closed=True, closed_id="c2", person_close_id="c2")
        log = state.conversation_log
        real = log.get_metadata_status

        def stale_read(key: str) -> tuple[dict, bool]:
            meta, readable = real(key)
            return {**meta, "closed_id": "c1", cron_inject.PERSON_CLOSE_KEY: "c1"}, readable

        log.get_metadata_status = stale_read  # type: ignore[method-assign]
        assert cron_inject.start_fresh_cron_tab(log, JOB_ID) is False
        log.get_metadata_status = real  # type: ignore[method-assign]
        assert DISPLAY_FLOOR_KEY not in log.get_metadata(KEY)


class TestThePersonsClose:
    @pytest.mark.asyncio
    async def test_closing_the_tab_marks_the_close_as_the_persons(
        self, state: Any, tmp_path: Any
    ) -> None:
        _write_transcript(tmp_path, 4)
        await cron_inject.ensure_cron_slot(state, _job("fresh"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(f"/api/chat/slots/cron-{JOB_ID}")
            assert resp.status == 200

        meta = state.conversation_log.get_metadata(KEY)
        assert meta["closed"] is True
        assert meta[cron_inject.PERSON_CLOSE_KEY] == meta["closed_id"]


def _on_disk(state: Any) -> list[str]:
    return [m["content"] for m in state.conversation_log.read_messages(KEY)]


async def _run_lands(state: Any, result: str) -> None:
    """A run's result reaches the tab and, off the loop, the transcript."""
    cron_inject.inject_cron_result_to_dashboard(
        state, _job("fresh"), result, include_prompt=False, history=None
    )
    await async_wait_until(lambda: any(result in row for row in _on_disk(state)), off_loop=True)


class TestRewindInAFlooredTab:
    """A rewind rewrites the tab's window; the rows the floor hides are not in it."""

    async def _fresh_reopen(self, state: Any, tmp_path: Any) -> list[str]:
        _write_transcript(tmp_path, 6)
        await cron_inject.ensure_cron_slot(state, _job("fresh"))
        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await client.delete(f"/api/chat/slots/cron-{JOB_ID}")).status == 200
        await cron_inject.ensure_cron_slot(state, _job("fresh"))
        return [f"m{i}" for i in range(6)]

    async def _resume_after_a_fresh_reopen(self, state: Any, tmp_path: Any) -> list[str]:
        _write_transcript(tmp_path, 6, closed=True, closed_id="c1", **{DISPLAY_FLOOR_KEY: _ts(3)})
        await cron_inject.ensure_cron_slot(state, _job("resume"))
        return [f"m{i}" for i in range(4)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reopen", ["_fresh_reopen", "_resume_after_a_fresh_reopen"])
    async def test_a_rewind_keeps_the_hidden_rows(
        self, state: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, reopen: str
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock())
        state.sessions._session_map.get = MagicMock(return_value="")
        hidden = await getattr(self, reopen)(state, tmp_path)
        slot = state.get_slot(f"cron-{JOB_ID}")
        # Slot detail's idle reconcile leaves them out of the tab's window.
        await _detail(state)
        assert not any(m["content"] in hidden for m in slot.messages)
        await _run_lands(state, "run result")
        reply = slot.append("user", "follow-up", "msg msg-u")
        assert await save_slot_off_loop(state, slot, best_effort=False)
        # The next run lands before the tab's next save.
        await _run_lands(state, "next run result")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/cron-{JOB_ID}/rewind",
                json={"ts": reply["ts"], "content": "edited follow-up"},
            )
            assert resp.status == 200
        if slot.task:
            slot.task.cancel()

        assert _on_disk(state)[: len(hidden)] == hidden


def _restored_floored_slot(state: Any, tmp_path: Any) -> Any:
    _write_transcript(tmp_path, 10, **{DISPLAY_FLOOR_KEY: _ts(5)})
    slot = state.get_or_create_slot(name=f"cron-{JOB_ID}")
    slot.linked_session_key = KEY
    # As the restore after a restart loads it: every row, with its own ``ts``.
    for row in state.conversation_log.read_messages(KEY):
        slot.append(
            row["role"], row["content"], "msg", ts=row["ts"], broadcast=False, mint_mid=False
        )
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return slot


class TestTabHistoryStopsAtTheFloor:
    """A restored tab's window still holds old rows; the history read never serves them."""

    @pytest.fixture()
    def floored_slot(self, state: Any, tmp_path: Any) -> Any:
        return _restored_floored_slot(state, tmp_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["", "?limit=50"])
    async def test_newest_page_starts_after_the_floor(
        self, state: Any, floored_slot: Any, query: str
    ) -> None:
        body = await _detail(state, query)

        assert [m["content"] for m in body["messages"]] == ["m6", "m7", "m8", "m9"]
        assert body["has_more"] is False
        assert body["next_before"] == 6
        assert body["total"] == 10

    @pytest.mark.asyncio
    async def test_a_cursor_read_offers_nothing_older(self, state: Any, floored_slot: Any) -> None:
        body = await _detail(state, "?limit=50&cursor=1")

        assert [m["content"] for m in body["messages"]] == ["m6", "m7", "m8", "m9"]
        assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_an_older_page_below_the_floor_is_empty(
        self, state: Any, floored_slot: Any
    ) -> None:
        body = await _detail(state, "?limit=3&before=4")

        assert body["messages"] == []
        assert body["has_more"] is False


async def _resume(state: Any) -> dict[str, Any]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/cron-{JOB_ID}/resume", json={"key": KEY})
        assert resp.status == 200
        return await resp.json()


class TestResumeStopsAtTheFloor:
    """Resuming the tab serves the page the tab's history read serves."""

    @pytest.mark.asyncio
    async def test_resuming_the_open_tab(self, state: Any, tmp_path: Any) -> None:
        _restored_floored_slot(state, tmp_path)

        body = await _resume(state)

        assert [m["content"] for m in body["messages"]] == ["m6", "m7", "m8", "m9"]
        assert body["has_more"] is False
        assert body["next_before"] == 6

    @pytest.mark.asyncio
    async def test_resuming_the_closed_tab(self, state: Any, tmp_path: Any) -> None:
        # The line a closed job tab leaves: linked to its job, closed, with a floor.
        _write_transcript(
            tmp_path,
            10,
            linked_session_key=KEY,
            closed=True,
            closed_id="c1",
            # Set, so the reopen's compare-and-clear never ties the file mtime (Windows tick).
            closed_at=1_700_000_000.0,
            **{DISPLAY_FLOOR_KEY: _ts(5)},
        )

        body = await _resume(state)

        assert body["history_key"] == KEY
        assert [m["content"] for m in body["messages"]] == ["m6", "m7", "m8", "m9"]
        assert body["has_more"] is False
        assert body["next_before"] == 6


class TestTheSetting:
    def test_only_resume_or_fresh_is_accepted(self) -> None:
        with pytest.raises(ValueError, match="on_tab_close"):
            build_job(name="j", message="m", every_secs=3600, on_tab_close="later")

    def test_fresh_round_trips_and_resume_is_not_written(self) -> None:
        fresh = _job_from_record(job_record(_job("fresh")))
        assert fresh.on_tab_close == "fresh"
        assert "on_tab_close" not in job_record(_job("resume"))
