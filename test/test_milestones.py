"""The milestones table: counting, streaks, backfill, manual marks and the read model."""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import milestones as m

_REAL_POLICY_PERMITS = m._policy_permits


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(m, "_catalog_cache", None)
    # No platform context is installed in a unit test; the policy itself is
    # covered by the tests that restore the real check.
    _floor_monkeypatch.setattr(m, "_policy_permits", lambda: True)
    return tmp_path


@pytest.fixture(autouse=True)
def _quiet_probes(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # The first hook flush runs the backfill probes; keep them off the real
    # audit log and config unless a test sets its own.
    _floor_monkeypatch.setattr(m, "_probe", lambda _signal: False)


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(m, "emit_counter", lambda name, attrs: seen.append((name, dict(attrs))))
    return seen


def _day(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    monkeypatch.setattr(m, "local_today", lambda: today)


def _items(snap: dict[str, Any], group: str) -> dict[str, dict[str, Any]]:
    [g] = [g for g in snap["groups"] if g["id"] == group]
    return {item["id"]: item for item in g["items"]}


class TestCounting:
    def test_record_counts_once_per_event_and_never_goes_down(self) -> None:
        m.record("widgets")
        st = m.load()
        assert st["counters"]["widgets"]["count"] == 1
        first = st["counters"]["widgets"]["first_seen"]
        assert st["reached"]["widgets"]["source"] == "event"
        m.record("widgets")
        st = m.load()
        assert st["counters"]["widgets"]["count"] == 2
        assert st["counters"]["widgets"]["first_seen"] == first

    def test_usage_tiers_are_reached_at_their_threshold_and_stay(self) -> None:
        for _ in range(9):
            m.record("chats-opened")
        assert "chats-opened:10" not in m.load()["reached"]
        m.record("chats-opened")
        st = m.load()
        assert st["reached"]["chats-opened:10"]["source"] == "event"
        item = _items(m.snapshot(), "sessions")["chats-opened"]
        assert item["value"] == 10 and item["tiers"] == [10, 100, 1000]
        assert set(item["tiers_reached_at"]) == {"10"}

    def test_unknown_id_records_nothing(self) -> None:
        m.record("not-a-milestone")
        assert m.load()["counters"] == {}

    def test_reached_metric_says_starter_within_a_day_of_a_click(self, metrics: list) -> None:
        m.apply_now(m.op_starter_clicked("widgets", "empty_state"))
        m.record("widgets")
        reached = [a for n, a in metrics if n == m.M_REACHED]
        assert reached == [{"group": "features", "milestone": "widgets", "via": "starter"}]
        assert (
            m.M_STARTER_CLICKED,
            {"surface": "empty_state", "milestone": "widgets", "send": "send"},
        ) in metrics

    def test_queued_ops_coalesce_into_one_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saves: list[int] = []
        real = m._save
        monkeypatch.setattr(m, "_save", lambda st: (saves.append(1), real(st)))
        with m._pending_lock:
            m._pending.extend([m._op_count("turns-sent")] * 5)
        m._flush()
        assert len(saves) == 1
        assert m.load()["counters"]["turns-sent"]["count"] == 5

    def test_a_write_failure_never_raises_into_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_st: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(m, "_save", boom)
        m.record("widgets")  # must not raise
        m.record_user_turn(mate_thread=True, crew_mode=True)

    def test_a_policy_denial_drops_hook_ops_with_no_write_and_no_metric(
        self, monkeypatch: pytest.MonkeyPatch, metrics: list
    ) -> None:
        asked: list[str] = []

        def deny(scope: str, *_a: object, **_k: object) -> bool:
            asked.append(scope)
            return False

        monkeypatch.setattr(m, "_policy_permits", _REAL_POLICY_PERMITS)
        monkeypatch.setattr("kiro_crew.platform.context.installed_context", lambda: object())
        monkeypatch.setattr("kiro_crew.edition_locks.policy_permits", deny)
        m.record("widgets")
        m.record_user_turn(mate_thread=True, crew_mode=True)
        assert asked and set(asked) == {"capabilities.milestones"}
        assert not m._path().exists()
        assert metrics == []
        assert m._pending == []

    def test_no_installed_context_counts_nothing_and_never_composes_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hook flush runs on a worker thread; it must not build and memoize
        # the process-wide default context while another thread installs one.
        def boom() -> None:
            raise AssertionError("composed a context")

        monkeypatch.setattr(m, "_policy_permits", _REAL_POLICY_PERMITS)
        monkeypatch.setattr("kiro_crew.platform.context.installed_context", lambda: None)
        monkeypatch.setattr("kiro_crew.platform.context.current_context", boom)
        assert m._policy_permits() is False
        m.record("widgets")
        assert not m._path().exists()

    def test_a_policy_permit_keeps_counting(
        self, monkeypatch: pytest.MonkeyPatch, metrics: list
    ) -> None:
        monkeypatch.setattr(m, "_policy_permits", _REAL_POLICY_PERMITS)
        monkeypatch.setattr("kiro_crew.platform.context.installed_context", lambda: object())
        monkeypatch.setattr("kiro_crew.edition_locks.policy_permits", lambda *_a, **_k: True)
        m.record("widgets")
        m.record_user_turn(mate_thread=True, crew_mode=True)
        st = m.load()
        assert st["counters"]["widgets"]["count"] == 1
        assert st["counters"]["turns-sent"]["count"] == 1
        assert any(n == m.M_REACHED for n, _ in metrics)

    def test_a_client_write_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_st: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(m, "_save", boom)
        with pytest.raises(OSError):
            m.apply_now(m.op_mark_done("slack"))

    def test_file_is_owner_only_and_holds_no_content(self) -> None:
        m.record("widgets")
        m.record_user_turn(mate_thread=False, crew_mode=False)
        path = m._path()
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        data = json.loads(path.read_text())
        assert set(data) == {
            "version",
            "counters",
            "streak",
            "reached",
            "skipped",
            "noticed",
            "backfilled",
            "starter_clicked",
        }
        # Only ids, numbers, dates and source labels.
        for value in json.dumps(data).split('"')[1::2]:
            assert len(value) <= 20, value

    def test_a_hand_edited_file_degrades_instead_of_failing(self) -> None:
        m._path().write_text(json.dumps({"counters": {"widgets": 7}, "streak": [], "reached": 3}))
        m.record("widgets")
        assert m.load()["counters"]["widgets"]["count"] == 1


class TestReadFailures:
    @pytest.mark.parametrize(
        "content",
        [b"not json\n", b"\xff", b"[]", b"null", b"[" * 2000 + b"]" * 2000],
        ids=["non-json", "non-utf8", "array", "null", "too-deep"],
    )
    def test_corrupt_file_is_not_overwritten(
        self, content: bytes, monkeypatch: pytest.MonkeyPatch, metrics: list
    ) -> None:
        path = m._path()
        today = date(2026, 10, 1)
        empty_snapshot = m.snapshot(today=today)
        path.write_bytes(content)
        monkeypatch.setattr(m, "_probe", lambda signal: signal != "mate_missing")
        monkeypatch.setattr(m, "_count_chat_transcripts", lambda: 12)

        m.record("widgets")
        assert path.read_bytes() == content
        assert m._pending == []
        with pytest.raises((ValueError, RecursionError)):
            m.apply_now(m.op_mark_done("slack"))
        assert path.read_bytes() == content
        m.ensure_backfilled()
        assert path.read_bytes() == content
        assert m.snapshot(today=today) == empty_snapshot
        assert metrics == []

    @pytest.mark.parametrize("error", [PermissionError, OSError, RecursionError])
    def test_unreadable_file_is_not_overwritten(
        self, error: type[Exception], monkeypatch: pytest.MonkeyPatch, metrics: list
    ) -> None:
        m.record("widgets")
        path = m._path()
        content = path.read_bytes()
        metrics.clear()
        read_text = Path.read_text

        def fail_read(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == path:
                raise error("state unreadable")
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_read)
        monkeypatch.setattr(m, "_probe", lambda _signal: True)
        monkeypatch.setattr(m, "_count_chat_transcripts", lambda: 12)
        assert m.load() == m._empty()
        m.record("widgets")
        assert path.read_bytes() == content
        with pytest.raises(error, match="state unreadable"):
            m.apply_now(m.op_mark_done("slack"))
        assert path.read_bytes() == content
        m.ensure_backfilled()
        assert path.read_bytes() == content
        assert metrics == []


class TestStreak:
    def test_same_day_next_day_and_a_missed_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _day(monkeypatch, date(2026, 10, 1))
        m.record_active_day()
        m.record_active_day()
        assert m.load()["streak"] == {"current": 1, "best": 1, "last_day": "2026-10-01"}
        _day(monkeypatch, date(2026, 10, 2))
        m.record_active_day()
        _day(monkeypatch, date(2026, 10, 3))
        m.record_active_day()
        assert m.load()["streak"]["current"] == 3
        assert "streak-best:3" in m.load()["reached"]
        # A missed day: shown as 0 on read, without a write, and best stays.
        snap = m.snapshot(today=date(2026, 10, 5))
        assert snap["streak"] == {"current": 0, "best": 3}
        _day(monkeypatch, date(2026, 10, 5))
        m.record_active_day()
        st = m.load()
        assert st["streak"]["current"] == 1 and st["streak"]["best"] == 3
        assert "streak-best:3" in st["reached"]
        # Yesterday still counts as current on read.
        assert m.snapshot(today=date(2026, 10, 6))["streak"]["current"] == 1

    def test_midnight_is_the_users_midnight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime, timezone

        # 03:00 UTC is still the previous day in Los Angeles, already today in Tokyo.
        fixed = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return fixed.astimezone(tz) if tz else fixed

        monkeypatch.setattr(m, "datetime", _Clock)
        with patch(
            "kiro_crew.config.loader_snapshots.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            assert m.local_today() == date(2026, 10, 1)
        with patch(
            "kiro_crew.config.loader_snapshots.published_config_timezone", return_value="Asia/Tokyo"
        ):
            assert m.local_today() == date(2026, 10, 2)

    def test_streak_survives_a_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _day(monkeypatch, date(2026, 10, 1))
        m.record_active_day()
        # A restart is a fresh process: nothing in memory, only the file.
        monkeypatch.setattr(m, "_pending", [])
        _day(monkeypatch, date(2026, 10, 2))
        m.record_active_day()
        assert m.load()["streak"]["current"] == 2

    def test_user_turn_counts_messages_streak_mate_and_crew_mode(self) -> None:
        m.record_user_turn(mate_thread=True, crew_mode=True)
        st = m.load()
        assert {k: v["count"] for k, v in st["counters"].items()} == {
            "turns-sent": 1,
            "mate": 1,
            "crew-mode": 1,
        }
        assert st["streak"]["current"] == 1
        m.record_user_turn(mate_thread=False, crew_mode=False)
        assert m.load()["counters"]["mate"]["count"] == 1


class TestBackfill:
    def _probes(self, monkeypatch: pytest.MonkeyPatch, fire: set[str]) -> list[str]:
        calls: list[str] = []

        def probe(signal: str) -> bool:
            calls.append(signal)
            return signal in fire

        monkeypatch.setattr(m, "_probe", probe)
        return calls

    def test_backfill_runs_once_credits_backfill_and_shows_no_notice(
        self, monkeypatch: pytest.MonkeyPatch, _home: Path, metrics: list
    ) -> None:
        calls = self._probes(monkeypatch, {"artifacts_nonempty", "sel_directive_ok:monitor_watch"})
        sessions = _home / "sessions"
        (sessions / "archive").mkdir(parents=True)
        header = json.dumps({"_type": "metadata", "memory_mode": "persistent"}) + "\n"
        for i in range(11):
            (sessions / f"dashboard_chat-{i}-1.jsonl").write_text(header, encoding="utf-8")
        (sessions / "dashboard_chat-0-1.jsonl.lock").write_text(header, encoding="utf-8")
        (sessions / "archive" / "dashboard_chat-0-1__2026.jsonl").write_text(
            header, encoding="utf-8"
        )
        (sessions / "archive" / "dashboard_chat-99-1__2026.jsonl").write_text(
            header, encoding="utf-8"
        )
        (sessions / "slack_123.jsonl").write_text(header, encoding="utf-8")

        m.ensure_backfilled()
        st = m.load()
        assert st["backfilled"] is True
        assert st["reached"]["widgets"]["source"] == "backfill"
        assert st["reached"]["pr-watch"]["source"] == "backfill"
        assert st["counters"]["chats-opened"]["count"] == 12
        assert st["reached"]["chats-opened:10"]["source"] == "backfill"
        assert "turns-sent" not in st["counters"] and st["streak"]["best"] == 0
        assert m.snapshot()["notices"] == []
        assert {a["via"] for n, a in metrics if n == m.M_REACHED} == {"backfill"}

        before = len(calls)
        m.ensure_backfilled()
        assert len(calls) == before

    @pytest.mark.parametrize(
        "header",
        [
            b'{"_type":"metadata","memory_mode":"incognito"}\n',
            b'{"_type":"metadata","memory_mode":"temporary"}\n',
            b'{"_type":"metadata","memory_mode":"unknown"}\n',
            b"not json\n",
            b"\n",
            b'{"_type":"metadata","padding":"' + b"x" * 65536 + b'"}\n',
        ],
        ids=["incognito", "temporary", "unknown", "malformed", "empty", "oversized"],
    )
    def test_backfill_skips_restricted_or_unknown_headers(self, _home: Path, header: bytes) -> None:
        sessions = _home / "sessions"
        sessions.mkdir()
        # A later metadata-shaped row cannot override the first line's mode.
        (sessions / "dashboard_chat-1-1.jsonl").write_bytes(
            header + b'{"_type":"metadata","memory_mode":"persistent"}\n'
        )
        assert m._count_chat_transcripts() == 0

    def test_backfill_accepts_a_legacy_header_in_any_segment(self, _home: Path) -> None:
        sessions = _home / "sessions"
        (sessions / "archive").mkdir(parents=True)
        (sessions / "dashboard_chat-1-1.jsonl").write_bytes(b"unreadable\n")
        (sessions / "archive" / "dashboard_chat-1-1__2026.jsonl").write_bytes(
            b'{"_type":"metadata"}\n'
        )
        assert m._count_chat_transcripts() == 1

    def test_backfill_reads_only_a_bounded_first_line(self, _home: Path) -> None:
        from io import BytesIO

        from kiro_crew.history_derivation import TRANSCRIPT_HEADER_MAX_BYTES

        sessions = _home / "sessions"
        sessions.mkdir()
        path = sessions / "dashboard_chat-1-1.jsonl"
        path.touch()
        header = b'{"_type":"metadata"}\n'

        class HeaderOnly(BytesIO):
            def readline(self, size: int = -1) -> bytes:
                assert self.tell() == 0
                assert size == TRANSCRIPT_HEADER_MAX_BYTES
                return super().readline(size)

            def __exit__(self, *args: object) -> None:
                assert self.tell() == len(header)
                super().__exit__(*args)

        with patch.object(
            m, "open", return_value=HeaderOnly(header + b"private body"), create=True
        ):
            assert m._count_chat_transcripts() == 1

    def test_a_hook_before_any_read_applies_the_backfill_first(
        self, monkeypatch: pytest.MonkeyPatch, _home: Path, metrics: list
    ) -> None:
        self._probes(monkeypatch, {"artifacts_nonempty"})
        sessions = _home / "sessions"
        sessions.mkdir()
        header = json.dumps({"_type": "metadata", "memory_mode": "persistent"}) + "\n"
        for i in range(10):
            (sessions / f"dashboard_chat-{i}-1.jsonl").write_text(header, encoding="utf-8")

        m.record("widgets")
        st = m.load()
        assert st["backfilled"] is True
        assert st["reached"]["widgets"]["source"] == "backfill"
        assert st["counters"]["widgets"]["count"] == 2
        assert "widgets" in st["noticed"]
        assert st["reached"]["chats-opened:10"]["source"] == "backfill"
        notices = m.snapshot()["notices"]
        assert "widgets" not in notices and "chats-opened:10" not in notices
        assert {a["via"] for n, a in metrics if n == m.M_REACHED} == {"backfill"}

    def test_a_probe_failure_on_a_hook_flush_still_counts_and_retries_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(_signal: str) -> bool:
            raise OSError("audit log unreadable")

        monkeypatch.setattr(m, "_probe", broken)
        m.record("turns-sent")
        st = m.load()
        assert st["counters"]["turns-sent"]["count"] == 1
        assert st["backfilled"] is False
        self._probes(monkeypatch, set())
        m.record("turns-sent")
        st = m.load()
        assert st["counters"]["turns-sent"]["count"] == 2
        assert st["backfilled"] is True

    def test_backfill_never_lowers_an_event_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._probes(monkeypatch, set())
        for _ in range(3):
            m.record("chats-opened")
        m.ensure_backfilled()
        assert m.load()["counters"]["chats-opened"]["count"] == 3


class TestManual:
    def test_mark_done_writes_manual_without_touching_the_counter(self, metrics: list) -> None:
        m.apply_now(m.op_mark_done("slack"))
        st = m.load()
        assert st["reached"]["slack"]["source"] == "manual"
        assert "slack" not in st["counters"]
        assert m.snapshot()["notices"] == []
        m.record("slack")
        st = m.load()
        assert st["reached"]["slack"]["source"] == "manual"
        assert st["counters"]["slack"]["count"] == 1
        assert (
            m.M_REACHED,
            {"group": "features", "milestone": "slack", "via": "manual"},
        ) in metrics

    def test_mark_all_done_marks_open_and_skipped_feature_rows_only(self, metrics: list) -> None:
        m.record("widgets")
        m.apply_now(m.op_skip("mate", "empty_state"))
        m.apply_now(m.op_mark_all_done())
        snap = m.snapshot()
        features = _items(snap, "features")
        assert all(item["state"] == "done" for item in features.values())
        assert features["widgets"]["source"] == "event"
        assert features["mate"]["source"] == "manual"
        assert snap["recommended"] == []
        assert _items(snap, "sessions")["chats-opened"]["tiers_reached_at"] == {}
        assert [a for n, a in metrics if n == m.M_FEATURES_COMPLETED] == [{"via": "mixed"}]
        assert (m.M_MARK_ALL_DONE, {}) in metrics

    def test_skip_all_keeps_rows_open_in_the_panel(self) -> None:
        m.record("widgets")
        m.apply_now(m.op_skip_all())
        snap = m.snapshot()
        features = _items(snap, "features")
        assert features["widgets"]["state"] == "done"
        assert {i["state"] for k, i in features.items() if k != "widgets"} == {"skipped"}
        assert snap["recommended"] == []
        assert snap["features"] == {"done": 1, "total": 7}


class TestReadModel:
    def test_missing_mate_is_unavailable_and_not_recommended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(m, "_probe", lambda signal: signal == "mate_missing")
        snap = m.snapshot()
        assert _items(snap, "features")["mate"]["state"] == "unavailable"
        assert snap["features"]["total"] == 6
        assert "mate" not in snap["recommended"]

    def test_recommended_is_the_next_three_feature_rows_by_order(self) -> None:
        for _ in range(100):
            m.record("turns-sent")
        m.record("widgets")
        m.apply_now(m.op_skip("scheduled-task", "empty_state"))
        snap = m.snapshot()
        assert snap["recommended"] == ["mate", "parallel-chats", "slack"]
        assert _items(snap, "features")["mate"]["state"] == "open"
        assert snap["features"]["total"] == 7
        assert set(snap["recommended"]) <= {r.id for r in m.feature_rows()}

    def test_notices_show_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock(start=1000.0)
        clock.install(monkeypatch, m)
        m.record("widgets")
        clock.advance(1.0)
        for _ in range(10):
            m.record("chats-opened")
        assert m.snapshot()["notices"] == ["widgets", "chats-opened:10"]
        m.apply_now(m.op_notice_shown("widgets"))
        m.apply_now(m.op_notice_shown("made-up-key"))
        assert m.snapshot()["notices"] == ["chats-opened:10"]
        assert "made-up-key" not in m.load()["noticed"]


class TestCatalogValidation:
    def _valid(self, row: m.Milestone) -> bool:
        return m._row_is_valid(row)

    def test_shipped_catalog_is_all_valid(self) -> None:
        assert len(m.catalog()) == len(m.CATALOG)
        assert [r.id for r in m.feature_rows()] == [
            "widgets",
            "scheduled-task",
            "mate",
            "parallel-chats",
            "slack",
            "pr-watch",
            "crew-mode",
        ]

    def test_send_defaults_to_prefill(self) -> None:
        assert m.Starter(kind="prompt").send == "prefill"
        sends = {r.id for r in m.feature_rows() if r.starter and r.starter.send == "send"}
        assert sends == {"widgets", "parallel-chats"}

    def test_off_list_send_row_is_dropped(self) -> None:
        row = m.Milestone(
            id="slack",
            group="features",
            order=1,
            doc="slack-integration.md",
            starter=m.Starter(kind="prompt", send="send"),
        )
        assert not self._valid(row)

    @pytest.mark.parametrize(
        "row",
        [
            m.Milestone(
                id="x", group="sessions", order=1, tiers=(10,), starter=m.Starter(kind="prompt")
            ),
            m.Milestone(id="x", group="sessions", order=1, tiers=(1,)),
            m.Milestone(id="x", group="turns", order=1, tiers=(10, 5)),
            m.Milestone(id="x", group="features", order=1, doc="artifacts.md"),
            m.Milestone(
                id="x",
                group="features",
                order=1,
                doc="internal.md",
                starter=m.Starter(kind="prompt"),
            ),
            m.Milestone(
                id="x",
                group="features",
                order=1,
                doc="artifacts.md",
                tiers=(1, 2),
                starter=m.Starter(kind="prompt"),
            ),
            m.Milestone(
                id="x",
                group="features",
                order=1,
                doc="artifacts.md",
                starter=m.Starter(kind="route", route="https://evil.example"),
            ),
        ],
    )
    def test_invalid_rows_are_dropped(self, row: m.Milestone) -> None:
        assert not self._valid(row)


class TestProbes:
    @pytest.mark.parametrize(
        "outcome",
        ["denied", "failed", "allowed", "ok", "invoked", "granted", "approved", "auto_approved"],
    )
    @pytest.mark.parametrize("terminal", ["completed", "success"])
    def test_sel_event_ok_requires_terminal_success(self, outcome: str, terminal: str) -> None:
        from kiro_crew import feature_videos as fv

        rows = [{"operation": "cron_add", "outcome": outcome}]
        with patch.object(fv, "sel") as sel:
            sel.return_value.recent.return_value = rows
            assert fv.probe_fires("sel_event_seen:cron_add") is True
            assert fv.probe_fires("sel_event_ok:cron_add") is False
            rows.append({"operation": "cron_add", "outcome": terminal})
            assert fv.probe_fires("sel_event_ok:cron_add") is True

    def test_sel_directive_ok_needs_the_applied_directive_row(self) -> None:
        from kiro_crew import feature_videos as fv

        rows = [
            {"operation": "monitor_start", "source": "mcp", "outcome": "completed"},
            {"operation": "monitor_start", "source": "mcp-directive", "outcome": "denied"},
        ]
        with patch.object(fv, "sel") as sel:
            sel.return_value.recent.return_value = rows
            assert fv.probe_fires("sel_directive_ok:monitor_start") is False
            rows.append(
                {"operation": "monitor_start", "source": "mcp-directive", "outcome": "success"}
            )
            assert fv.probe_fires("sel_directive_ok:monitor_start") is True

    def test_channel_connected_needs_all_three_slack_credentials(self) -> None:
        from kiro_crew import feature_videos as fv

        creds = {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"}
        with patch.object(fv.KiroCrewConfig, "load") as load:
            load.return_value.load_credentials.return_value = creds
            assert fv.probe_fires("channel_connected:slack") is False
            from kiro_crew.config.loader import CRED_OWNER_ID

            creds[CRED_OWNER_ID] = "U1"
            assert fv.probe_fires("channel_connected:slack") is True
            assert fv.probe_fires("channel_connected:discord") is False

    def test_mate_thread_has_user_turn_needs_a_human_row(self, _home: Path) -> None:
        from kiro_crew import feature_videos as fv

        binding = {"slot_key": "member-mate"}
        path = _home / "sessions" / "dashboard_member-mate.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"_type": "metadata", "memory_mode": "persistent"})
            + "\n"
            + json.dumps({"role": "user", "content": "seed", "meta": {}})
            + "\n",
            encoding="utf-8",
        )
        with patch("kiro_crew.members.read_dm_binding", return_value=binding):
            assert fv.probe_fires("mate_thread_has_user_turn") is False
            with path.open("a") as fh:
                fh.write(
                    json.dumps({"role": "user", "content": "hi", "meta": {"human": True}}) + "\n"
                )
            assert fv.probe_fires("mate_thread_has_user_turn") is True
        with patch("kiro_crew.members.read_dm_binding", return_value=None):
            assert fv.probe_fires("mate_thread_has_user_turn") is False

    @pytest.mark.parametrize(
        "header",
        [
            b'{"_type":"metadata","memory_mode":"incognito"}\n',
            b'{"_type":"metadata","memory_mode":"temporary"}\n',
            b'{"_type":"metadata","memory_mode":"unknown"}\n',
            b"not json\n",
            b"\n",
            b'{"_type":"metadata","padding":"' + b"x" * 65536 + b'"}\n',
        ],
        ids=["incognito", "temporary", "unknown", "malformed", "empty", "oversized"],
    )
    def test_mate_thread_rejects_restricted_or_unknown_headers(
        self, _home: Path, header: bytes
    ) -> None:
        from kiro_crew import feature_videos as fv

        path = _home / "sessions" / "dashboard_member-mate.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(header + b'{"role":"user","meta":{"human":true}}\n')
        with patch(
            "kiro_crew.members.read_dm_binding",
            return_value={"slot_key": "member-mate"},
        ):
            assert fv.probe_fires("mate_thread_has_user_turn") is False
