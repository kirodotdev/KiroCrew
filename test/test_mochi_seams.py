"""The three orchestration seams the owner loop must drive.

Each of these was fully ported yet had ZERO callers, so the behavior was
silently absent at runtime — the differential tests passed because they exercise
the modules directly, never the wiring. These tests pin the wiring itself.

* reminder cadence  — reminders never fired at all
* power events      — the pet kept polling and spawning with the lid shut
* pinned fs.watch   — a changed pinned file was never marked as changed
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import watchlist_file as wf
from kiro_crew.apps.builtins.mochi.pinned_files_service import PinnedFilesService
from kiro_crew.apps.builtins.mochi.watchlist_service import (
    GUARD_INTERVAL_MS,
    REMINDER_INTERVAL_MS,
    WatchlistService,
)

NOW = 1_700_000_000_000


class _Callbacks:
    """Records spawns instead of running an agent."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.spawn_ok = True

    def spawn_agent(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        fut: asyncio.Future[None] = asyncio.Future()
        if self.spawn_ok:
            fut.set_result(None)
        else:
            fut.set_exception(RuntimeError("spawn failed"))
        return fut

    def force_replan(self) -> None:
        pass

    def on_notify_direct(self, summary: str) -> None:
        pass


def _service(tmp_path: Path, cb: _Callbacks) -> WatchlistService:
    return WatchlistService(callbacks=cb, data_dir=str(tmp_path))


def _wl_path(tmp_path: Path) -> Path:
    """The path the service itself computes from data_dir."""
    from kiro_crew.apps.builtins.mochi.watchlist_service import _WATCHLIST_FILE

    return tmp_path / _WATCHLIST_FILE


def _write_reminder(path: Path, *, trigger_at: str, label: str = "standup") -> None:
    item = wf.create_watch_item(
        {"kind": "reminder", "label": label, "target": label, "triggerAt": trigger_at},
        now_ms=NOW,
    )
    wf.write_atomic(str(path), {"version": 1, "items": [item]})


class TestReminderCadence:
    """The 60s reminderTimer the fork deleted and the port never called."""

    def test_interval_matches_the_original(self) -> None:
        assert REMINDER_INTERVAL_MS == 60_000
        assert GUARD_INTERVAL_MS == 5 * 60_000

    @pytest.mark.asyncio
    async def test_due_reminder_spawns_one_agent(self, tmp_path: Path) -> None:
        cb = _Callbacks()
        svc = _service(tmp_path, cb)
        _write_reminder(_wl_path(tmp_path), trigger_at="2023-11-14T22:00:00.000Z")

        await svc.check_reminders(NOW)

        assert len(cb.prompts) == 1, "one agent handles ALL due reminders, not one each"
        prompt = cb.prompts[0]
        assert "standup" in prompt
        assert "mochi-remind" in prompt, "must load the reminder skill"
        assert "perform_pet_action" in prompt

    @pytest.mark.asyncio
    async def test_no_due_reminder_spawns_nothing(self, tmp_path: Path) -> None:
        cb = _Callbacks()
        svc = _service(tmp_path, cb)
        _write_reminder(_wl_path(tmp_path), trigger_at="2099-01-01T00:00:00.000Z")

        await svc.check_reminders(NOW)

        assert cb.prompts == []

    @pytest.mark.asyncio
    async def test_paused_service_skips_reminders(self, tmp_path: Path) -> None:
        """Idle (locked screen / asleep) must not fire reminders."""
        cb = _Callbacks()
        svc = _service(tmp_path, cb)
        _write_reminder(_wl_path(tmp_path), trigger_at="2023-11-14T22:00:00.000Z")

        svc.on_idle(NOW)
        await svc.check_reminders(NOW)

        assert cb.prompts == []

    @pytest.mark.asyncio
    async def test_prompt_forbids_delegation(self, tmp_path: Path) -> None:
        """The original hard-forbids spawn_run/cron_add — a reminder agent that
        delegates could fan out unboundedly."""
        cb = _Callbacks()
        svc = _service(tmp_path, cb)
        _write_reminder(_wl_path(tmp_path), trigger_at="2023-11-14T22:00:00.000Z")

        await svc.check_reminders(NOW)

        prompt = cb.prompts[0]
        for banned in ("spawn_run", "update_plan", "cron_add", "execute_bash"):
            assert banned in prompt, f"{banned} must be named in the forbidden list"


class TestPowerEventSeam:
    """lock-screen / suspend must pause the autonomous core."""

    def test_lock_and_suspend_pause_then_resume(self) -> None:
        from kiro_crew.apps.builtins.mochi.idle_manager import IdleManager

        paused: list[bool] = []

        class _Cb:
            def on_pause(self) -> None:
                paused.append(True)

            def on_resume(self) -> None:
                paused.append(False)

        mgr = IdleManager(_Cb())

        mgr.handle_power_event("lock-screen")
        assert paused == [True], "locking the screen must pause"

        mgr.handle_power_event("unlock-screen")
        assert paused == [True, False], "unlocking must resume"

    def test_unknown_event_is_ignored(self) -> None:
        from kiro_crew.apps.builtins.mochi.idle_manager import IdleManager

        seen: list[str] = []

        class _Cb:
            def on_pause(self) -> None:
                seen.append("idle")

            def on_resume(self) -> None:
                seen.append("resume")

        mgr = IdleManager(_Cb())

        mgr.handle_power_event("not-a-real-event")

        assert seen == []


class TestPinsSurviveTheOtherWriter:
    """The MCP process and the gateway service both write the pin file.

    The service persists its WHOLE in-memory list, so a pin the agent wrote
    straight to disk (from the separate MCP process) was silently deleted by the
    service's next mutation. Both sides now take `pins_mutation` and re-read
    inside it, which is what makes the two agree.
    """

    def _svc(self, tmp_path: Path) -> PinnedFilesService:
        return PinnedFilesService(str(tmp_path), lambda channel, *args: None)

    def _write_disk_pin(self, tmp_path: Path, path: str) -> None:
        """What the MCP tool does: a locked read-modify-write on the file."""
        from kiro_crew.apps.builtins.mochi.pinned_files_service import (
            DATA_FILE_NAME,
            pins_mutation,
        )

        file_path = tmp_path / DATA_FILE_NAME
        with pins_mutation(str(file_path)):
            data = json.loads(file_path.read_text()) if file_path.exists() else {"pins": []}
            data.setdefault("pins", []).append({"path": path, "pinnedAt": NOW})
            file_path.write_text(json.dumps(data))

    def test_a_pin_written_by_the_other_process_is_not_dropped(self, tmp_path: Path) -> None:
        svc = self._svc(tmp_path)
        a = str(tmp_path / "a.md")
        b = str(tmp_path / "b.md")
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")

        assert svc.add_pin(a, now_ms=NOW)
        # The agent pins something from the MCP process while the service holds
        # only `a` in memory.
        self._write_disk_pin(tmp_path, b)
        # Any further service mutation used to persist [a] and erase b.
        svc.mark_seen(a)

        paths = {p["path"] for p in svc.get_pins()}
        assert paths == {a, b}, "the other process's pin must survive"

    def test_a_removal_by_the_other_process_is_not_resurrected(self, tmp_path: Path) -> None:
        from kiro_crew.apps.builtins.mochi.pinned_files_service import (
            DATA_FILE_NAME,
            pins_mutation,
        )

        svc = self._svc(tmp_path)
        a = str(tmp_path / "a.md")
        (tmp_path / "a.md").write_text("a")
        assert svc.add_pin(a, now_ms=NOW)

        file_path = tmp_path / DATA_FILE_NAME
        with pins_mutation(str(file_path)):
            file_path.write_text(json.dumps({"version": 1, "pins": []}))

        # The service must not write its stale copy back over the empty list.
        svc.mark_seen(a)
        assert svc.get_pins() == []


class TestPinnedFileChangePoll:
    """Stands in for the original's fs.watch (no stdlib watcher in Python)."""

    def _svc(self, tmp_path: Path, events: list[tuple[str, str]]) -> PinnedFilesService:
        return PinnedFilesService(
            str(tmp_path),
            lambda channel, *args: events.append((channel, str(args))),
        )

    def test_first_poll_records_baseline_without_reporting(self, tmp_path: Path) -> None:
        """A freshly pinned file is not 'changed' just because we saw it."""
        events: list[tuple[str, str]] = []
        target = tmp_path / "notes.md"
        target.write_text("hello")
        svc = self._svc(tmp_path, events)
        svc._watched.add(str(target))

        svc.poll_file_changes(NOW)
        svc.tick(NOW + 10_000)

        assert events == [], "baseline sighting must not fire an update"

    def test_content_change_is_detected(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        target = tmp_path / "notes.md"
        target.write_text("hello")
        svc = self._svc(tmp_path, events)
        # An update is only broadcast for a path that is actually PINNED
        # (_process_watch_event), so register the pin, not just the watch.
        svc._pins.append({"path": str(target)})
        svc._watched.add(str(target))
        svc.poll_file_changes(NOW)

        target.write_text("hello world")  # size differs -> caught even on same mtime tick
        svc.poll_file_changes(NOW + 1_000)
        svc.tick(NOW + 2_000)

        assert events, "a changed pinned file must produce an event"

    def test_deletion_is_detected(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        target = tmp_path / "notes.md"
        target.write_text("hello")
        svc = self._svc(tmp_path, events)
        svc._watched.add(str(target))
        svc.poll_file_changes(NOW)

        target.unlink()
        svc.poll_file_changes(NOW + 1_000)
        svc.tick(NOW + 2_000)

        assert events, "a deleted pinned file must produce an event"

    def test_unchanged_file_stays_quiet(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        target = tmp_path / "notes.md"
        target.write_text("hello")
        svc = self._svc(tmp_path, events)
        svc._watched.add(str(target))

        for i in range(5):
            svc.poll_file_changes(NOW + i * 1_000)
        svc.tick(NOW + 10_000)

        assert events == [], "polling must be idempotent for an untouched file"


class TestAPinIsNotAPassiveBookmark:
    """A pinned path is POLLED, so pinning a credential file is a side channel.

    Nothing reads the contents — but the runtime stats every pinned path and
    surfaces `updatedAt`, so a pin on `~/.ssh/id_rsa` reports when that file
    changed. Both creation paths therefore clear the same gate every other file
    surface uses (`security.is_sensitive_path`), rather than each inventing one.
    """

    def test_the_service_refuses_a_sensitive_path(self, tmp_path: Path) -> None:
        svc = PinnedFilesService(str(tmp_path), lambda channel, *args: None)
        home = Path.home()
        assert svc.add_pin(str(home / ".ssh" / "id_rsa"), now_ms=NOW) is False
        assert svc.get_pins() == []

    def test_the_service_still_accepts_an_ordinary_file(self, tmp_path: Path) -> None:
        svc = PinnedFilesService(str(tmp_path), lambda channel, *args: None)
        target = tmp_path / "notes.md"
        target.write_text("hello")
        assert svc.add_pin(str(target), now_ms=NOW) is True

    def test_the_mcp_tool_refuses_a_sensitive_path(self, tmp_path: Path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.mochi import mcp_server

        monkeypatch.setattr(mcp_server, "_data_dir", lambda: tmp_path)
        out = mcp_server._tool_pin_file({"path": str(Path.home() / ".aws" / "credentials")})
        assert "not allowed" in out
        # Nothing was persisted, so the poller never learns the path exists.
        assert (
            not (tmp_path / "pinned-files.json").exists()
            or "credentials" not in (tmp_path / "pinned-files.json").read_text()
        )


class TestPinPersistFailureIsReportedNotSwallowed:
    """A failed write (e.g. a full data home) must NOT be reported as a successful
    pin/unpin — the mutation would 'succeed' now but revert on the next restart,
    and in-memory would diverge from disk. add_pin/remove_pin return False and
    roll the in-memory list back to the durable on-disk state.
    """

    def test_add_pin_persist_failure_returns_false_and_rolls_back(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import pinned_files_service as pfs

        svc = PinnedFilesService(str(tmp_path), lambda channel, *args: None)
        target = tmp_path / "notes.md"
        target.write_text("hi")

        def _boom(*a, **k):
            raise OSError("No space left on device")

        monkeypatch.setattr(pfs, "atomic_write", _boom)
        assert svc.add_pin(str(target), now_ms=NOW) is False
        # Rolled back to the (empty) durable state — no phantom pin in memory.
        assert svc.get_pins() == []

    def test_remove_pin_persist_failure_returns_false_and_keeps_pin(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import pinned_files_service as pfs

        svc = PinnedFilesService(str(tmp_path), lambda channel, *args: None)
        target = tmp_path / "notes.md"
        target.write_text("hi")
        assert svc.add_pin(str(target), now_ms=NOW) is True  # really persisted

        def _boom(*a, **k):
            raise OSError("No space left on device")

        monkeypatch.setattr(pfs, "atomic_write", _boom)
        assert svc.remove_pin(str(target)) is False
        # The pin survives: the failed write left the file intact, and the reload
        # restored it in memory — no success reported for an unpin that reverts.
        assert [p["path"] for p in svc.get_pins()] == [str(target)]


class TestPinnedToleratesMalformedEntries:
    """A stray non-dict entry persisted on disk must NOT crash a valid unpin.
    The rollback snapshot did `dict(p)` unconditionally, so `dict(1)` raised
    TypeError -> HTTP 500 on an otherwise-fine operation.
    """

    def test_unpin_survives_a_scalar_entry_in_the_list(self, tmp_path):
        from kiro_crew.apps.builtins.mochi.pinned_files_service import DATA_FILE_NAME

        target = tmp_path / "a.md"
        target.write_text("hi")
        (tmp_path / DATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "pins": [1, {"path": str(target)}]})
        )
        svc = PinnedFilesService(str(tmp_path), lambda channel, *args: None)
        # Would raise TypeError before the fix; must cleanly remove the real pin.
        assert svc.remove_pin(str(target)) is True
        assert [p for p in svc.get_pins() if isinstance(p, dict)] == []


class TestStatsPersistFailureKeepsDirty:
    """A failed stats write must NOT clear the dirty flag — otherwise the pending
    update is dropped and never retried, disappearing after restart.
    """

    def test_flush_retains_dirty_when_save_fails(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import stats_service as ss
        from kiro_crew.apps.builtins.mochi.stats_service import StatsService

        svc = StatsService(str(tmp_path))
        svc.mark_dirty(NOW)

        def _boom(*a, **k):
            raise OSError("No space left on device")

        monkeypatch.setattr(ss, "atomic_write", _boom)
        assert svc.save() is False
        svc.flush(NOW)
        # Still dirty -> the next flush retries instead of silently losing the data.
        assert svc._dirty is True


class TestMalformedStatsDoNotThrow:
    """parse_stats_json promises "any corruption yields defaults, never a throw".
    A persisted value whose ``messages``/``busiestDay`` is a truthy non-dict (a
    list/string/number from schema drift or hand-editing) must fall back to the
    nested defaults, NOT raise TypeError from ``**value`` — a throw here breaks
    on_startup and leaves Mochi enabled without its owner loop."""

    def test_truthy_non_dict_nested_value_falls_back(self):
        import json as _json

        from kiro_crew.apps.builtins.mochi.stats_service import (
            create_default_stats,
            parse_stats_json,
        )

        raw = _json.dumps({"messages": [1, 2], "busiestDay": "oops"})
        stats = parse_stats_json(raw, NOW)  # must not raise
        defaults = create_default_stats(NOW)
        assert stats["messages"] == defaults["messages"]
        assert stats["busiestDay"] == defaults["busiestDay"]


class TestWatchlistInputsAreValidated:
    """The watchlist add/update items are typed and bounded, so a mistyped field
    (e.g. `label: {}`) is rejected at MCP dispatch instead of being persisted and
    crashing the renderer."""

    def _schema(self):
        from kiro_crew.apps.builtins.mochi import mcp_server

        return mcp_server._INPUT_SCHEMAS["update_watchlist"]

    def test_a_mistyped_label_is_rejected(self):
        from kiro_crew.validation import ValidationError, validate_mcp_tool_arguments

        with pytest.raises(ValidationError):
            validate_mcp_tool_arguments({"add": [{"label": {}}]}, self._schema())

    def test_an_unknown_field_is_rejected(self):
        from kiro_crew.validation import ValidationError, validate_mcp_tool_arguments

        with pytest.raises(ValidationError):
            validate_mcp_tool_arguments({"add": [{"bogus": "x"}]}, self._schema())

    def test_a_well_formed_add_and_update_pass(self):
        from kiro_crew.validation import validate_mcp_tool_arguments

        validate_mcp_tool_arguments(
            {
                "add": [{"label": "watch me", "kind": "url", "checkIntervalMins": 60}],
                "update": [{"id": "abc", "status": "done"}],
                "cancel": ["x"],
                "remove": ["y"],
            },
            self._schema(),
        )

    def test_update_requires_an_id(self):
        from kiro_crew.validation import ValidationError, validate_mcp_tool_arguments

        with pytest.raises(ValidationError):
            validate_mcp_tool_arguments({"update": [{"status": "done"}]}, self._schema())


class TestResetIsNotResurrectedByAnInFlightPoll:
    """A concurrent reset that clears the queue must win: an in-flight poll's
    locked merge-write must NOT write its pre-reset snapshot back (which would
    resurrect the removed tasks). When the fresh read is None, it writes nothing.
    """

    def test_merge_write_returns_when_the_queue_was_reset(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import queue_file as qf
        from kiro_crew.apps.builtins.mochi.queue_poller import QueuePoller

        class _CB:
            async def spawn_agent(self, prompt: str) -> str:  # pragma: no cover
                return ""

            def get_due_watch_items(self):  # pragma: no cover
                return []

        poller = QueuePoller(str(tmp_path / "q.json"), _CB(), clock=lambda: 0)
        # Simulate: the queue was reset/deleted concurrently → fresh read is None.
        monkeypatch.setattr(qf, "read_queue", lambda p: None)
        wrote: list = []
        monkeypatch.setattr(qf, "write_queue_atomic", lambda p, d: wrote.append(d))

        stale_snapshot = {"tasks": [{"id": "ghost", "done": False}]}
        poller._locked_merge_write(stale_snapshot)

        assert wrote == [], "a reset must not be overwritten by the stale pre-reset snapshot"
