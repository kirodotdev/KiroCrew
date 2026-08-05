"""Tests for session_health log scanner."""

from __future__ import annotations

import datetime
from pathlib import Path

from kiro_crew.dashboard import session_health


def _ts_from_file(path: Path) -> str:
    """Return HH:MM:SS derived from file mtime — immune to clock skew."""
    return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M:%S")


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")
    import os
    os.utime(path, None)


def test_dead_provider_is_not_flagged(tmp_path: Path) -> None:
    """`Session X has dead provider — removing stale entry` is a healthy self-cleanup
    log line, not a stall. The session manager cleaned up a crashed provider; the
    next request cold-starts a fresh session transparently. Must NOT appear in
    the stalled set."""
    log = tmp_path / "gateway.log"
    _write_log(log, [""])  # touch to get mtime
    ts = _ts_from_file(log)
    _write_log(log, [
        f"{ts} WARNING kiro_crew.session: Session chat-2-1776999999 has dead provider — removing stale entry",
    ])
    result = session_health.compute_session_health(log_path=log, now=log.stat().st_mtime)
    assert "chat-2-1776999999" not in result
    assert result == {}


def test_detects_prompt_stuck(tmp_path: Path) -> None:
    log = tmp_path / "gateway.log"
    _write_log(log, [""])  # touch to get mtime
    ts = _ts_from_file(log)
    _write_log(log, [
        f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-9-1776732990: Prompt error: {{'code': -32603, 'message': 'Internal error', 'data': 'Prompt already in progress'}}",
    ])
    result = session_health.compute_session_health(log_path=log, now=log.stat().st_mtime)
    assert result["chat-9-1776732990"]["reason"] == "prompt_stuck"


def test_detects_prompt_stuck_from_formatted_message(tmp_path: Path) -> None:
    """The current log shape: class name present, backend text absent.

    Regression guard. chat_runner's message now comes from _format_acp_error,
    which rewrites "prompt already in progress" into user-facing prose. With
    only the legacy text pattern this line matched nothing and the stall went
    undetected, so the UI kept showing the slot as working. The class-name
    pattern is what carries the signal, and it cannot drift when the copy does.
    """
    log = tmp_path / "gateway.log"
    _write_log(log, [""])  # touch to get mtime
    ts = _ts_from_file(log)
    _write_log(log, [
        f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-7-1776999111: "
        "[AcpPromptBusy] I'm still processing a previous request. Please wait a "
        "moment and try again.",
    ])
    result = session_health.compute_session_health(log_path=log, now=log.stat().st_mtime)
    assert result["chat-7-1776999111"]["reason"] == "prompt_stuck"


def test_other_acp_error_classes_are_not_prompt_stuck(tmp_path: Path) -> None:
    """The class-name pattern must not over-match sibling AcpError subclasses."""
    log = tmp_path / "gateway.log"
    _write_log(log, [""])  # touch to get mtime
    ts = _ts_from_file(log)
    _write_log(log, [
        f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-8-1776999222: "
        "[AcpError] Bedrock is throttling requests.",
    ])
    result = session_health.compute_session_health(log_path=log, now=log.stat().st_mtime)
    assert result == {}


def test_ignores_internal_background_sessions(tmp_path: Path) -> None:
    log = tmp_path / "gateway.log"
    _write_log(log, [""])  # touch to get mtime
    ts = _ts_from_file(log)
    _write_log(log, [
        f"{ts} WARNING kiro_crew.slack.gateway: Injected timeout error for subagent abc into slot _bg",
        f"{ts} WARNING kiro_crew.slack.gateway: Injected timeout error for subagent def into slot cron_367da8a3",
        f"{ts} WARNING kiro_crew.slack.gateway: Injected timeout error for subagent ghi into slot cron:daily_check",
    ])
    result = session_health.compute_session_health(log_path=log, now=log.stat().st_mtime)
    assert result == {}


def test_last_reason_wins_per_slot(tmp_path: Path) -> None:
    log = tmp_path / "gateway.log"
    _write_log(log, [""])  # touch to get mtime
    ts = _ts_from_file(log)
    _write_log(log, [
        f"{ts} WARNING kiro_crew.slack.gateway: Injected timeout error for subagent abc into slot chat-3-111",
        f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-3-111: Prompt already in progress",
    ])
    result = session_health.compute_session_health(log_path=log, now=log.stat().st_mtime)
    assert result["chat-3-111"]["reason"] == "prompt_stuck"


def test_skips_lines_outside_window(tmp_path: Path) -> None:
    log = tmp_path / "gateway.log"
    # Use a timestamp 20 minutes before file mtime — outside the 10-min window
    _write_log(log, [""])  # touch to get mtime
    mtime_dt = datetime.datetime.fromtimestamp(log.stat().st_mtime)
    old = (mtime_dt - datetime.timedelta(minutes=20)).strftime("%H:%M:%S")
    _write_log(log, [
        f"{old} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-4-222: Prompt already in progress",
    ])
    result = session_health.compute_session_health(log_path=log)
    assert result == {}


def test_returns_empty_when_log_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.log"
    assert session_health.compute_session_health(log_path=missing) == {}
