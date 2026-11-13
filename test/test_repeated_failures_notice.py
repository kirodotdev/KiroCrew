"""The opt-in weekly repeated-failures bell note (dashboard/repeated_failures_notice.py)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.dashboard import repeated_failures_notice as notice
from kiro_crew.notifications.bus import SYSTEM_CHANNELS

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home of its own, with one recurring failure in the log."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(notice, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(notice, "_recurring_count", lambda now: 1)
    return tmp_path


def _write_config(home: Path, memory: dict) -> None:
    (home / "config.json").write_text(json.dumps({"memory": memory}), encoding="utf-8")


def _state(home: Path) -> Path:
    return home / notice.STATE_FILENAME


class TestOffByDefault:
    def test_no_key_sends_nothing_and_writes_nothing(self, home: Path) -> None:
        _write_config(home, {})

        assert notice.prepare_notice(NOW) is False
        assert not _state(home).exists()

    def test_a_quoted_true_reads_as_off_with_a_warning(
        self, home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(home, {"repeated_failures_notice": "true"})

        with caplog.at_level(logging.WARNING):
            assert notice.prepare_notice(NOW) is False
        assert not _state(home).exists()
        assert any("repeated_failures_notice" in r.getMessage() for r in caplog.records)


class TestOncePerWeek:
    def test_sends_once_then_waits_a_week(self, home: Path) -> None:
        _write_config(home, {"repeated_failures_notice": True})

        assert notice.prepare_notice(NOW) is True
        assert notice.prepare_notice(NOW + timedelta(days=1)) is False
        assert notice.prepare_notice(NOW + timedelta(days=6, hours=23)) is False
        assert notice.prepare_notice(NOW + timedelta(days=7)) is True

    def test_nothing_recurring_sends_nothing_and_keeps_the_week_open(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(home, {"repeated_failures_notice": True})
        monkeypatch.setattr(notice, "_recurring_count", lambda now: 0)

        assert notice.prepare_notice(NOW) is False
        assert not _state(home).exists()

    def test_restart_reads_the_last_send_from_disk(self, home: Path) -> None:
        _write_config(home, {"repeated_failures_notice": True})
        assert notice.prepare_notice(NOW) is True

        # A new gateway process holds nothing in memory: only the file remains.
        stored = json.loads(_state(home).read_text(encoding="utf-8"))
        assert datetime.fromisoformat(stored["last_sent"]) == NOW
        assert notice.prepare_notice(NOW + timedelta(days=2)) is False


class TestABadStateFile:
    @pytest.mark.parametrize(
        "content",
        ["not json", "[]", '{"last_sent": 5}', '{"last_sent": "2026-11-01T00:00:00"}'],
    )
    def test_reads_as_not_sent_yet_with_a_warning_and_is_repaired(
        self, home: Path, caplog: pytest.LogCaptureFixture, content: str
    ) -> None:
        _write_config(home, {"repeated_failures_notice": True})
        _state(home).write_text(content, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger=notice.__name__):
            assert notice.prepare_notice(NOW) is True
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        # Repaired by that send, so the next day does not send again.
        assert notice.prepare_notice(NOW + timedelta(days=1)) is False

    def test_a_file_that_is_not_utf8_reads_as_not_sent_yet_and_is_repaired(
        self, home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(home, {"repeated_failures_notice": True})
        _state(home).write_bytes('{"last_sent": "x"}'.encode("utf-16"))

        with caplog.at_level(logging.WARNING, logger=notice.__name__):
            assert notice.prepare_notice(NOW) is True
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert notice.prepare_notice(NOW + timedelta(days=1)) is False

    def test_a_time_in_the_future_does_not_hold_the_note_back(self, home: Path) -> None:
        _write_config(home, {"repeated_failures_notice": True})
        later = (NOW + timedelta(days=400)).isoformat()
        _state(home).write_text(json.dumps({"last_sent": later}), encoding="utf-8")

        assert notice.prepare_notice(NOW) is True

    def test_a_state_file_that_cannot_be_written_sends_nothing(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(home, {"repeated_failures_notice": True})

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(notice, "atomic_write", refuse)

        # Persist before publish: no note on any check while the write fails.
        assert notice.prepare_notice(NOW) is False
        assert notice.prepare_notice(NOW + timedelta(days=1)) is False


class _FakeState:
    def __init__(self) -> None:
        self.notes: list[dict] = []

    def notify(self, kind: str, title: str, body: str, **kwargs: object) -> None:
        self.notes.append({"kind": kind, "title": title, "body": body, **kwargs})


class TestTheNotifier:
    def test_checks_after_the_boot_delay_then_daily_and_sends_one_note(self, home: Path) -> None:
        _write_config(home, {"repeated_failures_notice": True})
        clock = [1000.0]
        notifier = notice.RepeatedFailuresNotifier(monotonic=lambda: clock[0], utcnow=lambda: NOW)
        state = _FakeState()

        async def tick() -> None:
            notifier.maybe_check(state)  # type: ignore[arg-type]
            task = notifier._task
            if task is not None:
                await asyncio.wait_for(task, 60)

        async def run() -> None:
            await tick()
            assert state.notes == []  # still inside the boot delay
            clock[0] += notice.FIRST_CHECK_DELAY_SECS
            await tick()
            assert len(state.notes) == 1
            clock[0] += notice.CHECK_INTERVAL_SECS
            await tick()
            assert len(state.notes) == 1  # a day later, same week

        asyncio.run(run())
        note = state.notes[0]
        assert note["channel"] == notice.NOTE_CHANNEL
        assert note["url"] == notice.NOTE_URL
        assert note["i18n"] == {"id": notice.NOTE_COPY_ID}

    def test_the_channel_is_a_system_channel(self) -> None:
        assert notice.NOTE_CHANNEL in SYSTEM_CHANNELS


def test_the_stored_english_is_the_catalog_english() -> None:
    """The bell renders the copy id; the OS banner and the feed read the stored text."""
    root = Path(__file__).resolve().parents[1] / "website" / "src" / "i18n" / "locales"
    copy = json.loads((root / "en.manual.json").read_text(encoding="utf-8"))
    note = copy["components"]["notifications"]["systemNote"]
    assert note[f"{notice.NOTE_COPY_ID}_title"] == notice.NOTE_TITLE
    assert note[f"{notice.NOTE_COPY_ID}_body"] == notice.NOTE_BODY
