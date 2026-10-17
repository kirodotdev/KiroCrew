"""Tests for the dashboard module."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from kiro_crew.dashboard.state import (
    DashboardState,
    _fmt_duration,
    _load_notifications,
    _maybe_trim_notifications,
    _persist_notification,
)
from kiro_crew.notifications.bus import payload_from_legacy


class TestDashboard:
    def test_fmt_duration_minutes(self) -> None:
        assert _fmt_duration(125) == "2m 5s"

    def test_fmt_duration_hours(self) -> None:
        assert _fmt_duration(3661) == "1h 1m"

    def test_fmt_duration_zero(self) -> None:
        assert _fmt_duration(0) == "0m 0s"

    def test_state_init(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=3),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        assert state.sessions.count == 3
        assert state.messages_received == 0

    def test_state_init_with_slack_client(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=MagicMock(),
            owner_id="U123",
        )
        assert state.slack_client is not None
        assert state.owner_id == "U123"


class TestNotificationPersistence:
    def test_persist_and_load(self, monkeypatch, tmp_path) -> None:
        """Notifications are persisted to JSONL and loaded on restart."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _persist_notification({"kind": "cron", "title": "Job A", "body": "result"})
        _persist_notification({"kind": "subagent", "title": "Sub B", "body": "done"})

        loaded = _load_notifications()
        assert len(loaded) == 2
        assert loaded[0]["title"] == "Job A"
        assert loaded[1]["title"] == "Sub B"

    def test_load_empty(self, monkeypatch, tmp_path) -> None:
        """Loading from nonexistent file returns empty list."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        assert _load_notifications() == []

    def test_load_redacts_preexisting_rows(self, monkeypatch, tmp_path) -> None:
        """Rows persisted before delivery-time redaction are redacted at load."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        path = tmp_path / "notifications.jsonl"
        secret = "AKIAIOSFODNN7EXAMPLE"
        row = {
            "kind": "cron",
            "title": "Job",
            "body": f"leaked key {secret}",
            "ts": "2026-01-01T00:00:00+00:00",
            "meta_note": f"nested {secret}",
        }
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")

        loaded = _load_notifications()
        assert len(loaded) == 1
        assert secret not in loaded[0]["body"]
        assert secret not in loaded[0]["meta_note"]
        # ts is exempt from redaction and must survive verbatim
        assert loaded[0]["ts"] == "2026-01-01T00:00:00+00:00"

    def test_load_skips_wrong_shape_row_without_discarding_history(
        self, monkeypatch, tmp_path
    ) -> None:
        """A valid-JSON row with an unexpected shape (normalize/redact raises)
        is skipped per-line; surrounding good rows survive."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        path = tmp_path / "notifications.jsonl"
        lines = [
            json.dumps({"kind": "cron", "title": "Before", "body": "ok"}),
            json.dumps([1, 2, 3]),  # valid JSON, wrong shape — normalize_note raises
            json.dumps({"kind": "cron", "title": "After", "body": "ok"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        loaded = _load_notifications()
        assert [n["title"] for n in loaded] == ["Before", "After"]

    def test_load_corrupted_lines_skipped(self, monkeypatch, tmp_path) -> None:
        """Corrupted JSON lines are skipped during load."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        path = tmp_path / "notifications.jsonl"
        lines = [
            json.dumps({"kind": "cron", "title": "Good", "body": "ok"}),
            "this is not json",
            json.dumps({"kind": "cron", "title": "Also good", "body": "ok"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        loaded = _load_notifications()
        assert len(loaded) == 2
        assert loaded[0]["title"] == "Good"
        assert loaded[1]["title"] == "Also good"

    def test_trim_large_file(self, monkeypatch, tmp_path) -> None:
        """File is trimmed when exceeding 2x max notifications."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._MAX_PERSISTED_NOTIFICATIONS", 5)
        path = tmp_path / "notifications.jsonl"
        # Write 11 lines (> 2 * 5)
        lines: list[str] = []
        for i in range(11):
            lines.append(json.dumps({"kind": "cron", "title": f"n{i}", "body": "x"}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        _maybe_trim_notifications(path)

        remaining = path.read_text(encoding="utf-8").splitlines()
        assert len(remaining) == 5
        # Should keep the last 5
        assert json.loads(remaining[0])["title"] == "n6"
        assert json.loads(remaining[-1])["title"] == "n10"

    def test_notify_persists(self, monkeypatch, tmp_path) -> None:
        """DashboardState.notify() persists to disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state.notify("cron", "Test Job", "Result text")

        # Check in-memory
        assert len(state._notification_log) == 1
        assert state._notification_log[0]["title"] == "Test Job"

        # Check on disk
        loaded = _load_notifications()
        assert len(loaded) == 1
        assert loaded[0]["title"] == "Test Job"
        assert "ts" in loaded[0]  # timestamp added

    def test_bus_sink_is_redacting_deliver_note(self, monkeypatch, tmp_path) -> None:
        """Wiring guarantee for Phase 2 app producers: the production bus sink
        IS the redacting _deliver_note, so an app push through
        notification_bus.push() gets its title/body/meta redacted there --
        producers (e.g. the push endpoint) rely on this and do not
        pre-redact."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        note = state.notification_bus.push(
            payload_from_legacy("cron", "key AKIAIOSFODNN7EXAMPLE via bus", "b")
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in note["title"]

    def test_deliver_note_redacts_nested_strings(self, monkeypatch, tmp_path) -> None:
        """Redaction descends into nested structures like action labels.

        The ``actions`` field is a list of dicts whose string values can
        carry LLM-derived content; a top-level-only scan would let secrets
        in nested fields reach SSE clients and JSONL on disk.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state._deliver_note(
            {
                "ts": "2026-07-12T00:00:00Z",
                "kind": "cron",
                "title": "key AKIAIOSFODNN7EXAMPLE in title",
                "body": "b",
                "actions": [
                    {"id": "a1", "label": "key AKIAIOSFODNN7EXAMPLE in label"},
                ],
                "extra": {"nested": ["key AKIAIOSFODNN7EXAMPLE in list"]},
            }
        )

        note = state._notification_log[0]
        # Top-level strings still redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in note["title"]
        # Nested strings inside lists of dicts redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in note["actions"][0]["label"]
        assert note["actions"][0]["id"] == "a1"
        # Arbitrary nesting (dict -> list) redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in note["extra"]["nested"][0]
        # Persisted JSONL is clean too
        loaded = _load_notifications()
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(loaded[0])

    def test_state_loads_existing_on_init(self, monkeypatch, tmp_path) -> None:
        """DashboardState.__init__ loads existing notifications from disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Pre-persist some notifications
        _persist_notification({"kind": "cron", "title": "Old", "body": "data"})
        _persist_notification({"kind": "cron", "title": "Old2", "body": "data2"})

        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        # Should have loaded existing notifications
        assert len(state._notification_log) == 2
        assert state._notification_log[0]["title"] == "Old"
        assert state._notification_log[1]["title"] == "Old2"

    def test_deliver_note_offloads_persist_on_running_loop(
        self, monkeypatch, tmp_path
    ) -> None:
        """On a running event loop, persistence goes through the single-worker
        executor (never blocking the loop), rows land in delivery order, and a
        later in-memory mutation (e.g. ack) does not leak into the snapshot
        that was handed to the executor."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.state import _notification_io_executor

        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )

        async def scenario() -> None:
            state._deliver_note({"ts": "t1", "kind": "cron", "title": "First", "body": "b"})
            state._deliver_note({"ts": "t2", "kind": "cron", "title": "Second", "body": "b"})
            # Mutate the in-memory note AFTER delivery (ack semantics); the
            # executor received a snapshot, so disk must not see this flag.
            state._notification_log[0]["acked"] = True
            # Fence: a no-op on the same single-worker executor runs only
            # after both pending persist jobs (FIFO) have completed.
            await asyncio.get_running_loop().run_in_executor(
                _notification_io_executor(), lambda: None
            )

        asyncio.run(scenario())

        rows = [
            json.loads(line)
            for line in (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [r["title"] for r in rows] == ["First", "Second"]
        assert "acked" not in rows[0]

    def test_deliver_note_persists_inline_without_loop(
        self, monkeypatch, tmp_path
    ) -> None:
        """Without a running loop (sync callers), persist happens inline so
        the row is on disk immediately after _deliver_note returns."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state._deliver_note({"ts": "t1", "kind": "cron", "title": "Sync", "body": "b"})
        rows = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1
        assert json.loads(rows[0])["title"] == "Sync"

    def test_delete_after_push_cannot_resurrect_row(self, monkeypatch, tmp_path) -> None:
        """A delete issued immediately after a push must win on disk: both the
        queued append and the rewrite run on the same single-worker executor
        in submission order, so the rewrite (submitted second) lands last and
        the deleted row cannot be resurrected by the still-queued append."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )

        async def scenario() -> bool:
            state._deliver_note({"ts": "t1", "kind": "cron", "title": "Doomed", "body": "b"})
            # Delete before the queued append has necessarily run.
            return await state.delete_notification("t1")

        assert asyncio.run(scenario()) is True
        content = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8")
        assert "Doomed" not in content

    def test_ack_persists_durably_before_return(self, monkeypatch, tmp_path) -> None:
        """ack_notification awaits the executor rewrite, so the acked flag is
        on disk by the time the coroutine returns."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state.broadcast_ws = MagicMock()

        async def scenario() -> bool:
            state._deliver_note({"ts": "t1", "kind": "cron", "title": "A", "body": "b"})
            return await state.ack_notification("t1")

        assert asyncio.run(scenario()) is True
        rows = [
            json.loads(line)
            for line in (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert rows[0]["acked"] is True

    def test_clear_broadcasts_ws_event_before_the_rewrite(self, monkeypatch, tmp_path) -> None:
        """clear_notifications must broadcast `notifications_clear` so every
        connected dashboard view drops its copy of the list — otherwise a
        second window/tab keeps stale items and a stale bell badge. The
        broadcast is emitted at the instant memory empties, BEFORE the awaited
        rewrite, so a note delivered during that await cannot be discarded by
        a clear frame arriving after its own delivery frame."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        order: list[str] = []
        state.broadcast_ws = MagicMock(side_effect=lambda *a, **k: order.append("broadcast"))

        real_rewrite = state._rewrite_notifications_async

        async def tracked_rewrite() -> None:
            await real_rewrite()
            order.append("rewrite")

        monkeypatch.setattr(state, "_rewrite_notifications_async", tracked_rewrite)

        async def scenario() -> None:
            state._deliver_note({"ts": "t1", "kind": "cron", "title": "A", "body": "b"})
            await state.clear_notifications()

        asyncio.run(scenario())
        assert state._notification_log == []
        assert state._unread_count == 0
        state.broadcast_ws.assert_called_once_with("notifications_clear", {})
        assert order == ["broadcast", "rewrite"]

    def test_a_note_delivered_during_the_clear_rewrite_is_not_dropped(
        self, monkeypatch, tmp_path
    ) -> None:
        """A note that arrives while the clear's rewrite is in flight is kept
        by the backend (its append lands after the empty-snapshot rewrite on
        the same ordered executor), so the clear frame must NOT sequence after
        that note's own delivery frame — otherwise every client discards a
        notification the backend still holds, with no recovery until a
        refetch. Pins the broadcast order by observing the frames a client
        would see."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        frames: list[str] = []
        state.broadcast_ws = MagicMock(side_effect=lambda kind, _data: frames.append(kind))
        # The delivery sink's own client fan-out, recorded on the same tape so
        # the relative order of the two frames is observable.
        monkeypatch.setattr(
            state, "_broadcast", lambda _note: frames.append("notification"), raising=False
        )

        real_rewrite = state._rewrite_notifications_async

        async def rewrite_with_concurrent_delivery() -> None:
            # Deliver mid-await: exactly the window the rewrite's executor
            # round-trip opens on the loop.
            state._deliver_note({"ts": "late", "kind": "cron", "title": "Late", "body": "b"})
            await real_rewrite()

        monkeypatch.setattr(state, "_rewrite_notifications_async", rewrite_with_concurrent_delivery)

        asyncio.run(state.clear_notifications())

        # The backend kept the late note, so the client must too: the clear
        # frame has to precede the note's delivery frame.
        assert [n["ts"] for n in state._notification_log] == ["late"]
        assert frames == ["notifications_clear", "notification"]

    def test_clear_of_empty_log_is_idempotent(self, monkeypatch, tmp_path) -> None:
        """Clearing an already-empty list is a no-op, never an error: the
        broadcast still fires (idempotent on the client) and the file rewrite
        leaves an empty log."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state.broadcast_ws = MagicMock()

        async def scenario() -> None:
            await state.clear_notifications()
            await state.clear_notifications()

        asyncio.run(scenario())
        assert state._notification_log == []
        assert state.broadcast_ws.call_count == 2

    def test_ack_all_rewrite_not_overtaken_by_queued_append(
        self, monkeypatch, tmp_path
    ) -> None:
        """ack-all's rewrite goes through the same ordered executor as the
        delivery append: acked state on disk cannot be clobbered by a
        still-queued unacked append snapshot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state.broadcast_ws = MagicMock()

        async def scenario() -> None:
            state._deliver_note({"ts": "t1", "kind": "cron", "title": "A", "body": "b"})
            # Ack-all immediately: rewrite is submitted after the append and
            # must land after it on disk.
            for n in state._notification_log:
                n["acked"] = True
            await state._rewrite_notifications_async()

        asyncio.run(scenario())
        rows = [
            json.loads(line)
            for line in (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 1
        assert rows[0]["acked"] is True
