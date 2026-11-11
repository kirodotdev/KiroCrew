"""Session Activity counts the sessions of a harness that writes no kiro transcript.

``_parse_sessions`` counts kiro-cli and KAS sessions from their transcripts. A
Claude Code session writes neither, so it is counted from its per-turn usage rows
(``provider: claude_code``): one session per ``transcript`` key (else ``slot``),
filed under the day of its earliest row, two messages per turn (one when no
reply was delivered), no tool calls. Rows of a provider whose sessions write
transcripts are never counted twice, and a subagent is not a session.

One instant is frozen for the test data and for the parser's own "today", so a
run that crosses local midnight cannot file the two on different days.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.dashboard.handlers.usage_sessions as sessions_mod
import kiro_crew.dashboard.handlers.usage_store as store_mod
from kiro_crew.dashboard.handlers.usage import _parse_sessions
from kiro_crew.testing.clock import ManualClock

#: The one instant every test reads, as a LOCAL wall time (naive): the product
#: keys days by the local calendar, so pinning the wall time makes "today" the
#: same day in every zone a runner can be in.
FROZEN_LOCAL = datetime(2026, 10, 2, 12, 0)


def _noon(days_ago: int) -> datetime:
    return (FROZEN_LOCAL - timedelta(days=days_ago)).astimezone()


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _row(ts: datetime, provider: str, **keys: object) -> dict[str, object]:
    return {"_type": "tokens", "ts": ts.isoformat(), "provider": provider, "credits": 0.0, **keys}


def _write_shard(shard_dir: Path, day: str, rows: list[object]) -> None:
    (shard_dir / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


@pytest.fixture
def parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``_parse_sessions`` over one kiro-cli transcript today and the shard dir it returns."""
    shards = tmp_path / "tokens"
    shards.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shards)
    # One frozen clock on both modules: the shard window and the parser's own
    # "today" and cutoff must agree on what day it is.
    clock = ManualClock(start=FROZEN_LOCAL.timestamp())
    clock.install(monkeypatch, sessions_mod, time=True, datetime=True)
    clock.install(monkeypatch, store_mod, time=False, datetime=True)
    cli = tmp_path / "cli"
    cli.mkdir()
    transcript = cli / "s1.jsonl"
    transcript.write_text(
        json.dumps({"kind": "Prompt", "timestamp": _noon(0).isoformat()})
        + "\n"
        + json.dumps({"kind": "ToolResults"})
        + "\n",
        encoding="utf-8",
    )

    def run() -> dict:
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", cli),
            patch.object(
                usage_mod,
                "validate_file_path",
                side_effect=lambda p: p if os.path.isdir(p) else str(transcript),
            ),
        ):
            return _parse_sessions()

    return shards, run


def test_claude_code_sessions_join_every_total(parse) -> None:
    shards, run = parse
    today, yesterday = _noon(0), _noon(1)
    _write_shard(
        shards,
        _day(yesterday),
        [_row(yesterday, "claude_code", transcript="dashboard:chat-1-1", slot="chat-1-1")],
    )
    _write_shard(
        shards,
        _day(today),
        [
            _row(today, "claude_code", transcript="dashboard:chat-1-1", slot="chat-1-1"),
            _row(today, "claude_code", transcript="dashboard:chat-2-2", slot="chat-2-2"),
            _row(today, "claude_code", slot="cron:nightly"),
        ],
    )
    r = run()
    # chat-1 is filed under yesterday, its first day; chat-2 and the cron slot today.
    history = {
        h["date"]: (h["sessions"], h["messages"], h["tool_calls"]) for h in r["daily_history"]
    }
    assert history == {_day(yesterday): (1, 4, 0), _day(today): (3, 5, 1)}
    assert (r["total_sessions"], r["total_messages"], r["total_tool_calls"]) == (4, 9, 1)
    assert r["today"] == {"sessions": 3, "messages": 5, "tool_calls": 1}
    assert r["all_time_sessions"] == 4


def test_a_turn_with_no_reply_is_one_message_and_a_subagent_is_no_session(parse) -> None:
    shards, run = parse
    today = _noon(0)
    _write_shard(
        shards,
        _day(today),
        [
            _row(today, "claude_code", transcript="dashboard:chat-6-6", delivered=False),
            _row(today, "claude_code", transcript="dashboard:chat-6-6", delivered=True),
            _row(today, "claude_code", slot="subagent:abc123"),
        ],
    )
    r = run()
    # The kiro-cli transcript (1 session, 1 message) plus chat-6 (1 + 2 messages).
    assert (r["total_sessions"], r["total_messages"]) == (2, 4)


def test_rows_of_a_transcript_writing_provider_are_not_counted_again(parse) -> None:
    shards, run = parse
    today = _noon(0)
    _write_shard(
        shards,
        _day(today),
        [
            _row(today, "acp", slot="chat-3-3"),
            _row(today, "kas", transcript="dashboard:chat-4-4"),
            # Passes the text pre-check (it names claude_code) but not the field test.
            _row(today, "acp", slot="chat-7-7", model="claude_code-model"),
        ],
    )
    r = run()
    assert (r["total_sessions"], r["total_messages"]) == (1, 1)


def test_rows_outside_the_window_or_without_a_key_are_skipped(parse) -> None:
    shards, run = parse
    today, old = _noon(0), _noon(40)
    _write_shard(shards, _day(old), [_row(old, "claude_code", slot="chat-5-5")])
    _write_shard(shards, _day(today), [_row(today, "claude_code"), "not a row", {"_type": "x"}])
    r = run()
    assert r["total_sessions"] == 1


def test_a_slot_only_row_joins_the_transcript_session_of_its_chat(parse) -> None:
    shards, run = parse
    today, yesterday = _noon(0), _noon(1)
    # An older row names only the bare slot; a newer one the session key.
    _write_shard(shards, _day(yesterday), [_row(yesterday, "claude_code", slot="chat-8-8")])
    _write_shard(
        shards,
        _day(today),
        [_row(today, "claude_code", transcript="dashboard:chat-8-8", slot="chat-8-8")],
    )
    r = run()
    # The kiro-cli transcript plus ONE chat-8 session, filed under its first day.
    assert (r["total_sessions"], r["total_messages"]) == (2, 5)
    history = {h["date"]: h["sessions"] for h in r["daily_history"]}
    assert history == {_day(yesterday): 1, _day(today): 1}


def test_a_background_one_liner_is_no_turn_of_any_session(parse) -> None:
    shards, run = parse
    today = _noon(0)
    _write_shard(
        shards,
        _day(today),
        [
            # A folder suggestion borrows its chat's slot; a title uses the default.
            _row(today, "claude_code", slot="chat-9-9", surface="bg:folder_suggest"),
            _row(today, "claude_code", slot="_bg", surface="bg:title"),
            _row(today, "claude_code", slot="_poison_canary", surface="bg:canary"),
            _row(today, "claude_code", transcript="dashboard:chat-9-9", surface="dashboard"),
        ],
    )
    r = run()
    # The kiro-cli transcript plus chat-9's one real turn.
    assert (r["total_sessions"], r["total_messages"]) == (2, 3)
