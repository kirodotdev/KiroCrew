"""Tests for kiro_crew.agent_metadata CRUD operations."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import agent_metadata


@pytest.fixture(autouse=True)
def _isolate_metadata_dir(tmp_path):
    """Redirect metadata_dir() to a temp directory for every test."""
    with patch.object(agent_metadata, "metadata_dir", return_value=tmp_path):
        yield tmp_path


def test_save_load_roundtrip(tmp_path):
    agent_metadata.save("test-agent", "# Test Agent\nHandles tests.")
    assert agent_metadata.load("test-agent") == "# Test Agent\nHandles tests."


def test_load_missing_returns_empty():
    assert agent_metadata.load("nonexistent") == ""


def test_delete_existing(tmp_path):
    agent_metadata.save("doomed", "bye")
    assert agent_metadata.delete("doomed") is True
    assert agent_metadata.load("doomed") == ""


def test_delete_missing():
    assert agent_metadata.delete("ghost") is False


def test_load_all(tmp_path):
    agent_metadata.save("alpha", "content-a")
    agent_metadata.save("beta", "content-b")
    result = agent_metadata.load_all()
    assert result == {"alpha": "content-a", "beta": "content-b"}


def test_load_all_empty():
    assert agent_metadata.load_all() == {}


def test_invalid_name_rejected():
    with pytest.raises(ValueError):
        agent_metadata.save("../etc/passwd", "evil")


def test_slash_in_name_rejected():
    with pytest.raises(ValueError):
        agent_metadata.load("foo/bar")


def test_empty_name_rejected():
    with pytest.raises(ValueError):
        agent_metadata.delete("")
