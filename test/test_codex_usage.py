"""Codex's 5-hour and weekly windows, read from the rollout logs under ``CODEX_HOME``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.usage as usage_mod
from conftest import requires_symlinks
from kiro_crew.dashboard.handlers.codex_usage import read_codex_rate_limits

#: 2100-01-01, a reset time that has not passed.
_FUTURE = 4102444800


def _token_count(stamp: str, primary: float, secondary: float, *, resets_at: int = _FUTURE) -> dict:
    return {
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": resets_at},
                "secondary": {
                    "used_percent": secondary,
                    "window_minutes": 10080,
                    "resets_at": _FUTURE,
                },
            },
        },
    }


def _rollout(home: Path, day: str, name: str, lines: list[object], mtime: float) -> Path:
    path = home / "sessions" / day / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_the_newest_block_wins_and_bad_lines_are_skipped(codex_home: Path) -> None:
    _rollout(codex_home, "2026/11/05", "old", [_token_count("2026-11-05T10:00:00Z", 90, 95)], 1000)
    newest = _rollout(
        codex_home,
        "2026/11/06",
        "new",
        [
            _token_count("2026-11-06T09:00:00Z", 20, 60),
            _token_count("2026-11-06T10:00:00Z", 30, 63),
            '{"payload": {"rate_limits": ',
            {"timestamp": "2026-11-06T10:00:01Z", "type": "event_msg", "payload": {}},
        ],
        2000,
    )

    reading = read_codex_rate_limits()

    assert reading == {
        "five_hour": {
            "used_percent": 30,
            "resets_at": _FUTURE,
            "window_minutes": 300,
            "stale": False,
        },
        "weekly": {
            "used_percent": 63,
            "resets_at": _FUTURE,
            "window_minutes": 10080,
            "stale": False,
        },
        "captured_at": "2026-11-06T10:00:00Z",
        "source_mtime": newest.lstat().st_mtime,
    }


def test_a_resumed_session_in_an_older_day_directory_wins(codex_home: Path) -> None:
    for index in range(5):
        stamp = f"2026-11-07T0{index}:00:00Z"
        _rollout(codex_home, "2026/11/07", f"new-{index}", [_token_count(stamp, 1, 1)], 100 + index)
    _rollout(
        codex_home, "2026/11/01", "resumed", [_token_count("2026-11-07T09:00:00Z", 90, 95)], 200
    )

    reading = read_codex_rate_limits()

    assert reading is not None
    assert reading["five_hour"]["used_percent"] == 90


def test_no_logs_is_no_data(codex_home: Path) -> None:
    assert read_codex_rate_limits() is None


def test_a_window_past_its_reset_is_stale(codex_home: Path) -> None:
    _rollout(
        codex_home,
        "2026/11/06",
        "a",
        [_token_count("2026-11-06T10:00:00Z", 10, 63, resets_at=1)],
        1,
    )

    reading = read_codex_rate_limits()

    assert reading is not None
    assert reading["five_hour"]["stale"] is True
    assert reading["weekly"]["stale"] is False


@requires_symlinks
def test_a_log_linked_to_the_credential_file_is_not_read(codex_home: Path) -> None:
    credential = codex_home / "auth.json"
    credential.write_text(json.dumps(_token_count("2026-11-06T10:00:00Z", 10, 63)) + "\n")
    link = codex_home / "sessions" / "2026" / "11" / "06" / "rollout-link.jsonl"
    link.parent.mkdir(parents=True)
    link.symlink_to(credential)

    assert read_codex_rate_limits() is None


@pytest.mark.asyncio
async def test_the_usage_route_carries_the_reading(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rollout(codex_home, "2026/11/06", "a", [_token_count("2026-11-06T10:00:00Z", 10, 63)], 1)

    async def _snapshot() -> tuple[dict, bool]:
        return usage_mod._empty_session_summary(), False

    monkeypatch.setattr(usage_mod, "_CACHE", {})
    monkeypatch.setattr(usage_mod, "_CACHE_TS", 0.0)
    monkeypatch.setattr(usage_mod, "_usage_sessions_snapshot", _snapshot)
    monkeypatch.setattr(usage_mod, "get_usage_cache", lambda: {})
    app = web.Application()
    app.router.add_get("/api/usage/kiro", usage_mod.api_kiro_usage)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/usage/kiro")
        data = await response.json()

    assert response.status == 200
    assert data["codex"]["five_hour"]["used_percent"] == 10
    assert data["codex"]["weekly"]["used_percent"] == 63
