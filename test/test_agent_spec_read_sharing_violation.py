"""A Windows sharing violation on an agent spec is not an absent spec.

On Windows a read that lands while ``spec_files._atomic_json_write`` replaces
``kirocrew.json`` fails with ``PermissionError`` (WinError 32 or 33). POSIX
permits that read, so the fault is simulated here: ``open_fenced_for_read`` is
patched to raise the sharing violation a set number of times, then open the real
file.

The spec reader retries that window with the budget
:func:`kiro_crew.atomic_write.read_bytes_with_retry` uses, so
``_load_agent_config`` still returns the configured server. A spec that stays
locked past the budget still reads as ``None`` (the per-file contract callers
depend on), now with one WARNING naming it. POSIX keeps a single attempt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew import agent_discovery
from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.mcp_discovery import _load_agent_config

_LOGGER = "kiro_crew.agent_discovery"


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch home whose ``kirocrew.json`` declares one MCP server."""
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    agents = tmp_path / ".kiro" / "agents"
    agents.mkdir(parents=True)
    spec = {"name": "kirocrew", "mcpServers": {"locked-srv": {"command": "x"}}}
    (agents / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    return tmp_path


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def _lock_spec(monkeypatch: pytest.MonkeyPatch, *, times: int, winerror: int = 32) -> dict:
    """Make the spec's pinned open raise a sharing violation *times* times."""
    real_open = agent_discovery.open_fenced_for_read
    state = {"n": 0}

    def locked_open(resolved, **kwargs):  # type: ignore[no-untyped-def]
        if Path(resolved).name == AGENT_FILENAME:
            state["n"] += 1
            if state["n"] <= times:
                exc = PermissionError(13, "being used by another process", str(resolved))
                exc.winerror = winerror  # type: ignore[attr-defined]
                raise exc
        return real_open(resolved, **kwargs)

    monkeypatch.setattr(agent_discovery, "open_fenced_for_read", locked_open)
    return state


def _servers(user_home: Path) -> dict:
    return _load_agent_config(user_home=user_home).get("mcpServers", {})


@pytest.mark.parametrize("winerror", [32, 33])
def test_spec_locked_mid_replace_still_lists_its_servers(
    windows: None, user_home: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    state = _lock_spec(monkeypatch, times=1, winerror=winerror)

    assert "locked-srv" in _servers(user_home)
    assert state["n"] == 2


def test_spec_locked_for_most_of_the_budget_is_still_read(
    windows: None, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _lock_spec(monkeypatch, times=aw._REPLACE_MAX_ATTEMPTS - 1)

    assert "locked-srv" in _servers(user_home)
    assert state["n"] == aw._REPLACE_MAX_ATTEMPTS


def test_spec_locked_past_the_budget_warns_and_reads_as_none(
    windows: None,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _lock_spec(monkeypatch, times=aw._REPLACE_MAX_ATTEMPTS)
    spec = user_home / ".kiro" / "agents" / AGENT_FILENAME

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        result = agent_discovery._read_agent_spec(spec, operation="t", source="t")

    assert result is None
    assert state["n"] == aw._REPLACE_MAX_ATTEMPTS
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert repr(spec) in warnings[0].getMessage()
    assert "locked" in warnings[0].getMessage()


def test_spec_locked_on_the_event_loop_keeps_one_attempt(
    windows: None, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    state = _lock_spec(monkeypatch, times=1)

    assert "locked-srv" not in _servers(user_home)
    assert state["n"] == 1


def test_posix_permission_error_is_not_retried_or_warned(
    user_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    state = _lock_spec(monkeypatch, times=1)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert "locked-srv" not in _servers(user_home)

    assert state["n"] == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_absent_spec_still_reads_as_none_without_a_warning(
    windows: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / AGENT_FILENAME

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert agent_discovery._read_agent_spec(missing, operation="t", source="t") is None

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_strict_reader_gets_the_same_retry(
    windows: None, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lock_spec(monkeypatch, times=1)
    spec = user_home / ".kiro" / "agents" / AGENT_FILENAME

    data = agent_discovery.read_agent_spec_strict(spec, operation="t", source="t")

    assert "locked-srv" in data["mcpServers"]
