"""Per-day credits on the Usage tab's Daily History rows.

The Daily History table is built from transcripts (sessions / messages / tool
calls per day) while credits are billed per turn into the usage shards. These
tests pin the join: ``daily_credits`` reads the shards with the same row guard
``slot_spend`` uses, and ``_parse_sessions`` carries that sum onto every
history row as ``credits`` so the column reconciles with the Billing card.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers.usage import _parse_sessions, daily_credits


def _noon(days_ago: int) -> datetime:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _write_shard(shard_dir: Path, day: str, records: list[object]) -> None:
    (shard_dir / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _row(ts: datetime, credits: object, slot: str = "chat-1-1700000000") -> dict[str, object]:
    return {"_type": "tokens", "ts": ts.isoformat(), "slot": slot, "credits": credits}


@pytest.fixture
def shard_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "tokens"
    d.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", d)
    return d


class TestDailyCredits:
    def test_empty_dir_is_empty(self, shard_dir: Path) -> None:
        assert daily_credits() == {}

    def test_missing_dir_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path / "absent")
        assert daily_credits() == {}

    def test_sums_per_local_day(self, shard_dir: Path) -> None:
        today, yesterday = _noon(0), _noon(1)
        _write_shard(shard_dir, _day(yesterday), [_row(yesterday, 1.25), _row(yesterday, 2)])
        _write_shard(shard_dir, _day(today), [_row(today, 0.5)])
        assert daily_credits() == {_day(yesterday): 3.25, _day(today): 0.5}

    def test_day_key_follows_the_row_not_the_shard_name(self, shard_dir: Path) -> None:
        # A shard can span midnight; the row's own local day is what buckets it.
        today, yesterday = _noon(0), _noon(1)
        _write_shard(shard_dir, _day(today), [_row(yesterday, 4.0)])
        assert daily_credits() == {_day(yesterday): 4.0}

    @pytest.mark.parametrize(
        "credits",
        [None, "3", True, math.nan, math.inf, -math.inf, [1], {"v": 1}],
        ids=["absent", "string", "bool", "nan", "inf", "-inf", "list", "dict"],
    )
    def test_non_finite_or_non_numeric_credits_are_not_counted(
        self, shard_dir: Path, credits: object
    ) -> None:
        today = _noon(0)
        _write_shard(shard_dir, _day(today), [_row(today, credits), _row(today, 1.0)])
        assert daily_credits() == {_day(today): 1.0}

    def test_int_and_float_credits_both_count(self, shard_dir: Path) -> None:
        today = _noon(0)
        _write_shard(shard_dir, _day(today), [_row(today, 2), _row(today, 0.75)])
        assert daily_credits() == {_day(today): 2.75}

    def test_an_int_wider_than_a_double_is_dropped_not_raised(self, shard_dir: Path) -> None:
        # ``math.isfinite`` on such an int raises OverflowError instead of
        # answering; the row must be skipped like any other unusable value.
        today = _noon(0)
        path = shard_dir / f"{_day(today)}.jsonl"
        huge = "1" + "0" * 400
        path.write_text(
            f'{{"_type": "tokens", "ts": "{today.isoformat()}", "credits": {huge}}}\n'
            + json.dumps(_row(today, 1.0))
            + "\n",
            encoding="utf-8",
        )
        assert daily_credits() == {_day(today): 1.0}

    def test_a_row_that_would_overflow_the_day_total_is_dropped(self, shard_dir: Path) -> None:
        # Two finite values whose sum is not: the payload must never carry
        # Infinity, so the second row is dropped and the first one stands.
        today = _noon(0)
        big = 1.5e308
        _write_shard(shard_dir, _day(today), [_row(today, big), _row(today, big), _row(today, 1.0)])
        assert daily_credits() == {_day(today): big + 1.0}
        assert math.isfinite(daily_credits()[_day(today)])

    def test_unlistable_shard_dir_is_empty_not_a_crash(
        self, shard_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_shard(shard_dir, _day(_noon(0)), [_row(_noon(0), 1.0)])
        monkeypatch.setattr(
            Path, "iterdir", lambda self: (_ for _ in ()).throw(PermissionError("denied"))
        )
        assert daily_credits() == {}

    def test_non_token_rows_and_garbage_lines_are_skipped(self, shard_dir: Path) -> None:
        today = _noon(0)
        path = shard_dir / f"{_day(today)}.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"_type": "context", "ts": today.isoformat(), "credits": 9}),
                    "not json",
                    json.dumps([1, 2, 3]),
                    json.dumps(_row(today, 1.5)),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        assert daily_credits() == {_day(today): 1.5}

    def test_rows_older_than_the_window_are_dropped(self, shard_dir: Path) -> None:
        # The shard is inside the window by name; the ROW is older than the
        # per-row cutoff and must still be excluded (shards span midnight).
        today = _noon(0)
        old = datetime.now().astimezone() - timedelta(
            days=usage_mod._SESSIONS_HISTORY_DAYS, hours=1
        )
        _write_shard(shard_dir, _day(today), [_row(old, 7.0), _row(today, 1.0)])
        assert daily_credits() == {_day(today): 1.0}

    def test_unparseable_timestamp_is_skipped(self, shard_dir: Path) -> None:
        today = _noon(0)
        bad = {"_type": "tokens", "ts": "yesterday-ish", "slot": "chat-1-1", "credits": 5}
        _write_shard(shard_dir, _day(today), [bad, _row(today, 1.0)])
        assert daily_credits() == {_day(today): 1.0}

    def test_no_slot_filter(self, shard_dir: Path) -> None:
        # Background and non-session slots are billed too; the Billing card's
        # total includes them, so the per-day figure must as well.
        today = _noon(0)
        _write_shard(
            shard_dir,
            _day(today),
            [
                _row(today, 1.0, slot="chat-1-1700000000"),
                _row(today, 2.0, slot="cron:job-1"),
                _row(today, 4.0, slot=""),
            ],
        )
        assert daily_credits() == {_day(today): 7.0}

    def test_unreadable_shard_costs_only_its_rows(self, shard_dir: Path) -> None:
        today, yesterday = _noon(0), _noon(1)
        _write_shard(shard_dir, _day(yesterday), [_row(yesterday, 3.0)])
        _write_shard(shard_dir, _day(today), [_row(today, 1.0)])
        real_open = Path.open

        def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == f"{_day(today)}.jsonl":
                raise OSError("boom")
            return real_open(self, *args, **kwargs)

        with patch.object(Path, "open", flaky_open):
            assert daily_credits() == {_day(yesterday): 3.0}


def _write_session(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")


class TestParseSessionsCarriesCredits:
    def test_history_row_carries_the_day_sum(self, tmp_path: Path, shard_dir: Path) -> None:
        today = _noon(0)
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(
            f,
            [{"kind": "Prompt", "timestamp": today.isoformat()}, {"kind": "AssistantMessage"}],
        )
        _write_shard(shard_dir, _day(today), [_row(today, 1.234), _row(today, 2.0)])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
        assert r["daily_history"] == [
            {
                "date": _day(today),
                "sessions": 1,
                "messages": 2,
                "tool_calls": 0,
                "credits": 3.23,  # 3.234 rounded to the Billing card's 2 decimals
            }
        ]

    def test_day_without_credits_reads_zero(self, tmp_path: Path, shard_dir: Path) -> None:
        today = _noon(0)
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt", "timestamp": today.isoformat()}])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
        assert [(h["date"], h["credits"]) for h in r["daily_history"]] == [(_day(today), 0.0)]

    def test_day_with_credits_but_no_transcript_gets_a_zero_session_row(
        self, tmp_path: Path, shard_dir: Path
    ) -> None:
        today, yesterday = _noon(0), _noon(1)
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt", "timestamp": today.isoformat()}])
        _write_shard(shard_dir, _day(yesterday), [_row(yesterday, 2.5, slot="cron:nightly")])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
        assert r["daily_history"] == [
            {
                "date": _day(yesterday),
                "sessions": 0,
                "messages": 0,
                "tool_calls": 0,
                "credits": 2.5,
            },
            {"date": _day(today), "sessions": 1, "messages": 1, "tool_calls": 0, "credits": 0.0},
        ]
        # The credit-only day does not inflate the session statistics.
        assert r["total_sessions"] == 1
        assert r["today"]["sessions"] == 1
