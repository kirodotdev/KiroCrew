"""Tests for context_management module."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_config(tmp_path):
    with patch("kiro_crew.context_management.config_dir", return_value=tmp_path):
        yield tmp_path


def test_cap_result_file_no_truncation(tmp_path):
    from kiro_crew.context_management import cap_result_file

    p = tmp_path / "small.md"
    p.write_text("short content")
    assert cap_result_file(p) is False
    assert p.read_text() == "short content"


def test_cap_result_file_truncates(tmp_path):
    from kiro_crew.context_management import RESULT_FILE_MAX_BYTES, cap_result_file

    p = tmp_path / "big.md"
    p.write_bytes(b"x" * (RESULT_FILE_MAX_BYTES + 10000))
    assert cap_result_file(p) is True
    assert p.stat().st_size <= RESULT_FILE_MAX_BYTES + 200  # marker overhead
    content = p.read_text()
    assert "truncated" in content


def test_cap_streaming_text_short():
    from kiro_crew.context_management import cap_streaming_text

    assert cap_streaming_text("short") == "short"


def test_cap_streaming_text_long():
    from kiro_crew.context_management import STREAMING_TEXT_MAX_CHARS, cap_streaming_text

    text = "a" * (STREAMING_TEXT_MAX_CHARS + 1000)
    result = cap_streaming_text(text)
    assert len(result) <= STREAMING_TEXT_MAX_CHARS + 20
    assert result.startswith("…(truncated)")


def test_cap_history():
    from kiro_crew.context_management import HISTORY_MAX_ENTRIES, cap_history

    entries = [{"i": i} for i in range(HISTORY_MAX_ENTRIES + 100)]
    result = cap_history(entries)
    assert len(result) == HISTORY_MAX_ENTRIES
    assert result[0]["i"] == 100  # oldest kept


def test_check_session_budget_under(tmp_path):
    from kiro_crew.context_management import check_session_budget

    (tmp_path / "agent-a.md").write_text("small")
    assert check_session_budget(tmp_path) is False


def test_check_session_budget_over(tmp_path):
    from kiro_crew.context_management import SESSION_MAX_BYTES, check_session_budget

    (tmp_path / "agent-a.md").write_bytes(b"x" * (SESSION_MAX_BYTES + 1))
    assert check_session_budget(tmp_path) is True


def test_evict_completed_agents():
    from kiro_crew.context_management import evict_completed_agents

    agents = {}
    for i in range(60):
        agents[f"a{i}"] = SimpleNamespace(done=True, started=float(i))
    evicted = evict_completed_agents(agents, max_retained=50)
    assert evicted == 10
    assert len(agents) == 50
    assert "a0" not in agents  # oldest evicted
    assert "a59" in agents  # newest kept


def test_evict_skips_running():
    from kiro_crew.context_management import evict_completed_agents

    agents = {
        "running": SimpleNamespace(done=False, started=0.0),
        "done1": SimpleNamespace(done=True, started=1.0),
    }
    evicted = evict_completed_agents(agents, max_retained=1)
    assert evicted == 0  # only 1 completed, within limit


def test_cleanup_stale_sessions(tmp_config):
    import time

    from kiro_crew.context_management import cleanup_stale_sessions

    sessions_dir = tmp_config / "sessions"
    sessions_dir.mkdir()
    old = sessions_dir / "old-session"
    old.mkdir()
    (old / "history.jsonl").write_text("{}")
    # Make it old
    import os

    old_time = time.time() - 86400 * 10
    os.utime(old / "history.jsonl", (old_time, old_time))

    new = sessions_dir / "new-session"
    new.mkdir()
    (new / "history.jsonl").write_text("{}")

    cleaned = cleanup_stale_sessions()
    assert cleaned == 1
    assert not old.exists()
    assert new.exists()
