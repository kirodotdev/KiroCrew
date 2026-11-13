"""Mochi watchlist service: idle state, spawn degradation, reminder and expiry passes,
archive, and the serialized write queue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import watchlist_file as wf
from kiro_crew.apps.builtins.mochi.watchlist_service import (
    _ARCHIVE_FILE,
    _WATCHLIST_FILE,
    MAX_SPAWN_FAILURES,
    SPAWN_COOLDOWN_MS,
    SPAWN_TIMEOUT_MS,
    WatchlistService,
)

NOW = 1_700_000_000_000
HOUR = 3_600_000


class _Callbacks:
    """Records every spawn and direct notify; ``ok=False`` makes each spawn fail."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.prompts: list[str] = []
        self.direct: list[str] = []

    async def spawn_agent(self, prompt: str) -> None:
        self.prompts.append(prompt)
        if not self.ok:
            raise RuntimeError("spawn failed")

    def force_replan(self) -> None:
        pass

    def on_notify_direct(self, summary: str) -> None:
        self.direct.append(summary)


def _svc(tmp: Path, cb: _Callbacks, on_write=None) -> WatchlistService:
    return WatchlistService(callbacks=cb, data_dir=str(tmp), on_write_complete=on_write)


def _store(tmp: Path, items: list[dict[str, Any]]) -> None:
    wf.write_atomic(str(tmp / _WATCHLIST_FILE), {"version": 1, "items": items})


def _read(tmp: Path) -> list[dict[str, Any]]:
    return wf.read_watchlist(str(tmp / _WATCHLIST_FILE), now_ms=NOW)["items"]


def _item(**changes: Any) -> dict[str, Any]:
    item = wf.create_watch_item(
        {"kind": "url", "label": "docs", "target": "https://example.com/d"}, now_ms=NOW
    )
    item.update(changes)
    return item


class TestIdleAndSpawnLock:
    def test_idle_pauses_and_resume_clears_it(self, tmp_path):
        svc = _svc(tmp_path, _Callbacks())
        svc.on_idle(NOW)
        assert svc.is_paused
        svc.on_resume()
        assert not svc.is_paused

    @pytest.mark.asyncio
    async def test_a_failed_spawn_holds_the_lock_until_it_is_cleared(self, tmp_path):
        svc = _svc(tmp_path, _Callbacks(ok=False))
        assert await svc._try_spawn_agent("p", NOW) is False
        assert svc.spawn_failures == 1
        assert svc.is_spawn_locked(NOW + 1000)
        assert not svc.is_spawn_locked(NOW + SPAWN_TIMEOUT_MS)
        svc.clear_spawn_lock()
        assert not svc.is_spawn_locked(NOW + 1000)


class TestSpawnDegradation:
    @pytest.mark.asyncio
    async def test_the_third_failure_degrades_to_direct_notify(self, tmp_path):
        cb = _Callbacks(ok=False)
        svc = _svc(tmp_path, cb)
        for i in range(MAX_SPAWN_FAILURES):
            assert await svc._try_spawn_agent(f"p{i}", NOW + i) is False
        assert svc.is_degraded
        assert cb.direct == [f"p{MAX_SPAWN_FAILURES - 1}"]

    @pytest.mark.asyncio
    async def test_degraded_mode_skips_the_spawn_inside_the_cooldown(self, tmp_path):
        cb = _Callbacks(ok=False)
        svc = _svc(tmp_path, cb)
        for i in range(MAX_SPAWN_FAILURES):
            await svc._try_spawn_agent(f"p{i}", NOW + i)
        assert await svc._try_spawn_agent("late", NOW + MAX_SPAWN_FAILURES) is False
        assert "late" not in cb.prompts
        assert cb.direct[-1] == "late"

    @pytest.mark.asyncio
    async def test_a_spawn_after_the_cooldown_recovers_and_resets_the_count(self, tmp_path):
        cb = _Callbacks(ok=False)
        svc = _svc(tmp_path, cb)
        for i in range(MAX_SPAWN_FAILURES):
            await svc._try_spawn_agent(f"p{i}", NOW + i)
        cb.ok = True
        recovered_at = NOW + MAX_SPAWN_FAILURES + SPAWN_COOLDOWN_MS + 1
        assert await svc._try_spawn_agent("back", recovered_at) is True
        assert svc.spawn_failures == 0
        assert not svc.is_degraded


class TestReminderPass:
    @pytest.mark.asyncio
    async def test_a_held_lock_skips_the_next_reminder_pass(self, tmp_path):
        cb = _Callbacks()
        svc = _svc(tmp_path, cb)
        reminder = wf.create_watch_item(
            {
                "kind": "reminder",
                "label": "standup",
                "target": "standup",
                "triggerAt": wf._iso(NOW - 5 * 60_000),
            },
            now_ms=NOW,
        )
        reminder["notes"] = "bring slides"
        _store(tmp_path, [reminder])
        await svc.check_reminders(NOW)
        await svc.check_reminders(NOW + 1000)
        assert len(cb.prompts) == 1
        assert '"standup"' in cb.prompts[0]
        assert "5 min late" in cb.prompts[0]
        assert "— note: bring slides" in cb.prompts[0]

    @pytest.mark.asyncio
    async def test_three_failed_passes_mark_the_due_reminder_done(self, tmp_path):
        cb = _Callbacks(ok=False)
        svc = _svc(tmp_path, cb)
        reminder = wf.create_watch_item(
            {
                "kind": "reminder",
                "label": "standup",
                "target": "standup",
                "triggerAt": wf._iso(NOW - 60_000),
            },
            now_ms=NOW,
        )
        _store(tmp_path, [reminder])
        for _ in range(MAX_SPAWN_FAILURES):
            svc.clear_spawn_lock()
            await svc.check_reminders(NOW)
        assert cb.direct, "the third failure notifies directly"
        assert [item["status"] for item in _read(tmp_path)] == ["done"]


class TestGuard:
    @pytest.mark.asyncio
    async def test_an_idle_guard_pass_changes_nothing(self, tmp_path):
        cb = _Callbacks()
        svc = _svc(tmp_path, cb)
        _store(tmp_path, [_item(maxWatchDurationHours=1)])
        svc.on_idle(NOW)
        await svc.guard(NOW + 2 * HOUR)
        assert cb.prompts == []
        assert [item["status"] for item in _read(tmp_path)] == ["watching"]

    @pytest.mark.asyncio
    async def test_an_expired_item_is_marked_and_the_agent_is_told(self, tmp_path):
        cb = _Callbacks()
        svc = _svc(tmp_path, cb)
        _store(tmp_path, [_item(maxWatchDurationHours=1, lastResult="price flat")])
        await svc.guard(NOW + 2 * HOUR)
        assert [item["status"] for item in _read(tmp_path)] == ["expired"]
        assert len(cb.prompts) == 1
        assert '- "docs" (watched for 2 hours, last status: price flat)' in cb.prompts[0]
        assert "[options: Re-watch <label> | OK]" in cb.prompts[0]

    @pytest.mark.asyncio
    async def test_an_expired_item_is_left_alone_while_the_lock_is_held(self, tmp_path):
        cb = _Callbacks(ok=False)
        svc = _svc(tmp_path, cb)
        _store(tmp_path, [_item(maxWatchDurationHours=1)])
        await svc._try_spawn_agent("earlier", NOW + 2 * HOUR - 60_000)
        await svc.guard(NOW + 2 * HOUR)
        assert [item["status"] for item in _read(tmp_path)] == ["watching"]

    @pytest.mark.asyncio
    async def test_completed_items_archive_only_after_a_day(self, tmp_path):
        svc = _svc(tmp_path, _Callbacks())
        old = _item(status="done", completedAt=wf._iso(NOW - 25 * HOUR))
        recent = _item(status="done", completedAt=wf._iso(NOW - HOUR))
        _store(tmp_path, [old, recent])
        await svc.guard(NOW)
        assert [item["id"] for item in _read(tmp_path)] == [recent["id"]]
        archived = wf.read_archive(str(tmp_path / _ARCHIVE_FILE), now_ms=NOW)["items"]
        assert [item["id"] for item in archived] == [old["id"]]


class TestExpiredPrompt:
    def test_an_item_without_a_status_reads_unknown(self, tmp_path):
        svc = _svc(tmp_path, _Callbacks())
        prompt = svc._build_expired_prompt(
            [{"label": "a", "createdAt": wf._iso(NOW), "lastResult": ""}], NOW + HOUR
        )
        assert prompt.startswith("You are a background agent.")
        assert '- "a" (watched for 1 hours, last status: unknown)' in prompt
        assert "Notify the user each item has been paused." in prompt


class TestWriteQueue:
    @pytest.mark.asyncio
    async def test_the_completion_hook_fires_after_each_write(self, tmp_path):
        calls: list[int] = []
        svc = _svc(tmp_path, _Callbacks(), on_write=lambda: calls.append(1))
        await svc.enqueue_write(lambda: None)
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_a_failing_write_rejects_only_its_own_future(self, tmp_path):
        svc = _svc(tmp_path, _Callbacks())
        marks: list[int] = []

        def boom() -> None:
            raise ValueError("disk full")

        failed = svc.enqueue_write(boom)
        later = svc.enqueue_write(lambda: marks.append(1))
        with pytest.raises(ValueError, match="disk full"):
            await failed
        await later
        assert marks == [1]
