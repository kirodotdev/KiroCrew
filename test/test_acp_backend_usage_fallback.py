"""Usage analytics on a host with no kiro-cli transcripts.

An absent kiro sessions directory is the NORMAL state on another ACP backend, not
an error to report forever. Before this, the endpoint answered a permanent
200-carrying-an-error that was never cached, so it re-parsed on every poll and the
Usage page showed nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import usage


@pytest.fixture()
def shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the shard reader at a temp usage/tokens directory."""
    root = tmp_path / "usage" / "tokens"
    root.mkdir(parents=True)

    def _shards_in_window(days: int) -> list[Path]:
        return sorted(root.glob("*.jsonl"))

    monkeypatch.setattr(usage, "_shards_in_window", _shards_in_window)
    return root


def _write(root: Path, day: str, rows: list[dict]) -> None:
    lines = [json.dumps({"_type": "tokens", **row}) for row in rows]
    (root / f"{day}.jsonl").write_text("\n".join(lines) + "\n")


class TestOwnRecordsFallback:
    def test_counts_one_session_per_slot(self, shards: Path) -> None:
        _write(shards, "2026-08-10", [{"slot": "a"}, {"slot": "a"}, {"slot": "b"}])
        result = usage._sessions_from_own_records()
        assert result["total_sessions"] == 2
        assert result["total_messages"] == 3

    def test_a_slot_spanning_days_is_one_session(self, shards: Path) -> None:
        """Counting per active day would inflate sessions for long conversations."""
        _write(shards, "2026-08-10", [{"slot": "a"}])
        _write(shards, "2026-08-11", [{"slot": "a"}])
        result = usage._sessions_from_own_records()
        assert result["total_sessions"] == 1

    def test_a_session_is_attributed_to_its_earliest_day(self, shards: Path) -> None:
        _write(shards, "2026-08-11", [{"slot": "a"}])
        _write(shards, "2026-08-10", [{"slot": "a"}])
        history = {
            h["date"]: h["sessions"] for h in usage._sessions_from_own_records()["daily_history"]
        }
        assert history["2026-08-10"] == 1
        assert history.get("2026-08-11", 0) == 0

    def test_tool_calls_are_always_zero(self, shards: Path) -> None:
        """A token shard records no tool calls; reporting a guess would mislead."""
        _write(shards, "2026-08-10", [{"slot": "a"}])
        result = usage._sessions_from_own_records()
        assert result["total_tool_calls"] == 0
        assert all(h["tool_calls"] == 0 for h in result["daily_history"])

    def test_malformed_and_foreign_rows_are_skipped(self, shards: Path) -> None:
        path = shards / "2026-08-10.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"_type": "tokens", "slot": "a"}),
                    "not json",
                    json.dumps({"_type": "other", "slot": "b"}),
                    json.dumps({"_type": "tokens"}),  # no slot
                    json.dumps({"_type": "tokens", "slot": ""}),
                    "",
                ]
            )
            + "\n"
        )
        result = usage._sessions_from_own_records()
        assert result["total_sessions"] == 1

    def test_no_shards_yields_zeroes_not_an_error(self, shards: Path) -> None:
        result = usage._sessions_from_own_records()
        assert result["total_sessions"] == 0
        assert "error" not in result

    def test_shape_matches_the_kiro_path(self, shards: Path) -> None:
        """The frontend reads one shape; a missing key would break the page."""
        _write(shards, "2026-08-10", [{"slot": "a"}])
        result = usage._sessions_from_own_records()
        for key in (
            "total_sessions",
            "total_messages",
            "total_tool_calls",
            "all_time_sessions",
            "daily_history",
            "today",
            "this_week",
        ):
            assert key in result
        for key in ("sessions", "messages", "tool_calls"):
            assert key in result["today"]
            assert key in result["this_week"]

    def test_this_week_window(self, shards: Path) -> None:
        recent = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _write(shards, recent, [{"slot": "new"}])
        _write(shards, old, [{"slot": "old"}])
        result = usage._sessions_from_own_records()
        assert result["this_week"]["sessions"] == 1
        assert result["total_sessions"] == 2


class TestFallbackSelection:
    def test_kiro_host_still_reports_the_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """On a kiro host an absent directory genuinely means something is wrong."""
        monkeypatch.setattr(usage, "_sessions_dir", lambda: tmp_path / "gone")
        monkeypatch.setattr(usage, "_configured_acp_backend", lambda: "")
        assert usage._parse_sessions() == {"error": "No sessions directory"}

    def test_other_backend_uses_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shards: Path
    ) -> None:
        monkeypatch.setattr(usage, "_sessions_dir", lambda: tmp_path / "gone")
        monkeypatch.setattr(usage, "_configured_acp_backend", lambda: "codex")
        _write(shards, "2026-08-10", [{"slot": "a"}])
        result = usage._parse_sessions()
        assert "error" not in result
        assert result["total_sessions"] == 1

    def test_backend_reader_fails_closed(self) -> None:
        assert usage._configured_acp_backend() in ("", "codex", "claude", "kas")
