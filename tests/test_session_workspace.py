"""Tests for session_workspace and context_summarizer."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def fake_config_dir(tmp_path):
    with patch("kiro_crew.session_workspace.config_dir", return_value=tmp_path):
        yield tmp_path


def test_workspace_dir_creates_directory(fake_config_dir):
    from kiro_crew.session_workspace import workspace_dir

    d = workspace_dir("test-session")
    assert d.exists()
    assert d.name == "test-session"


def test_write_and_read_result(fake_config_dir):
    from kiro_crew.session_workspace import read_result, write_result

    write_result("s1", "abc", "hello world")
    assert read_result("s1", "abc") == "hello world"


def test_append_result(fake_config_dir):
    from kiro_crew.session_workspace import append_result, read_result

    append_result("s1", "abc", "chunk1")
    append_result("s1", "abc", "chunk2")
    assert read_result("s1", "abc") == "chunk1chunk2"


def test_read_missing_returns_empty(fake_config_dir):
    from kiro_crew.session_workspace import read_result

    assert read_result("s1", "nonexistent") == ""


def test_list_results(fake_config_dir):
    from kiro_crew.session_workspace import list_results, write_result

    write_result("s1", "abc", "result1")
    write_result("s1", "def", "result2")
    results = list_results("s1")
    assert len(results) == 2
    assert results[0]["agent_id"] == "abc"
    assert results[1]["agent_id"] == "def"


def test_history_roundtrip(fake_config_dir):
    from kiro_crew.session_workspace import append_history, load_history

    append_history("s1", {"role": "user", "content": "hello"})
    append_history("s1", {"role": "assistant", "content": "hi"})
    entries = load_history("s1")
    assert len(entries) == 2
    assert entries[0]["content"] == "hello"


def test_cleanup(fake_config_dir):
    from kiro_crew.session_workspace import cleanup, workspace_dir, write_result

    write_result("s1", "abc", "data")
    assert workspace_dir("s1").exists()
    cleanup("s1")
    assert not (fake_config_dir / "sessions" / "s1").exists()


def test_invalid_session_id_rejected(fake_config_dir):
    from kiro_crew.session_workspace import workspace_dir

    with pytest.raises(ValueError):
        workspace_dir("../../../etc")


def test_invalid_agent_id_rejected(fake_config_dir):
    from kiro_crew.session_workspace import write_result

    with pytest.raises(ValueError):
        write_result("s1", "../../etc/passwd", "evil")
