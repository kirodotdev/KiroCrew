"""Tests for context_management module."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_config(tmp_path):
    with patch("kiro_crew.context_management.config_dir", return_value=tmp_path):
        yield tmp_path


def test_cap_result_bytes_keeps_an_answer_within_the_budget():
    from kiro_crew.context_management import cap_result_bytes

    assert cap_result_bytes(b"short content") == b"short content"


@pytest.mark.parametrize(
    "text",
    [
        "".join(chr(0x4E00 + (i % 2000)) for i in range(200_000)),  # 3-byte CJK
        ("a" * 99 + "\u2014") * 5_100,  # an em-dash every 100 characters
        "x" * 530_000,
        "\U0001f600" * 150_000,  # 4-byte emoji
    ],
    ids=["cjk", "em-dash", "ascii", "emoji"],
)
def test_cap_result_bytes_cuts_bytes_at_character_boundaries(text):
    """The cap budgets bytes, so it cuts bytes: the output fits the cap, decodes
    cleanly, keeps an exact prefix and suffix of the original with nothing
    repeated, and its marker counts the bytes actually dropped."""
    import re

    from kiro_crew.context_management import RESULT_FILE_MAX_BYTES, cap_result_bytes

    original = text.encode("utf-8")
    capped = cap_result_bytes(original)
    capped.decode("utf-8")  # no torn character at either seam
    assert len(capped) <= RESULT_FILE_MAX_BYTES
    match = re.search(rb"\n\n\[\.\.\.truncated ([\d,]+) bytes\.\.\.\]\n\n", capped)
    assert match is not None
    head, tail = capped[: match.start()], capped[match.end() :]
    assert original.startswith(head) and original.endswith(tail)
    assert int(match.group(1).replace(b",", b"")) == len(original) - len(head) - len(tail)


def test_cap_result_file_caps_a_streamed_file_as_the_bytes_cap_would(tmp_path, monkeypatch):
    """A partial that streamed to disk is capped in place to exactly what the
    in-memory cap gives, reading only the two ends it keeps."""
    import kiro_crew.context_management as cm

    original = ("a" * 99 + "\u2014") * 6_000
    path = tmp_path / "result.txt"
    path.write_bytes(original.encode("utf-8"))
    reads: list[int] = []
    real_open = type(path).open

    def _counting_open(self, *a, **kw):
        handle = real_open(self, *a, **kw)
        real_read = handle.read

        def _read(n=-1):
            reads.append(n)
            return real_read(n)

        handle.read = _read
        return handle

    monkeypatch.setattr(type(path), "open", _counting_open)
    assert cm.cap_result_file(path) is True
    cap_reads = list(reads)
    assert cap_reads and all(0 <= n <= cm.RESULT_FILE_MAX_BYTES for n in cap_reads), cap_reads
    assert path.read_bytes() == cm.cap_result_bytes(original.encode("utf-8"))


def test_cap_result_file_leaves_a_small_or_missing_file_alone(tmp_path):
    from kiro_crew.context_management import cap_result_file

    small = tmp_path / "small.txt"
    small.write_text("short content", encoding="utf-8")
    assert cap_result_file(small) is False
    assert small.read_text(encoding="utf-8") == "short content"
    assert cap_result_file(tmp_path / "missing.txt") is False
    assert not (tmp_path / "missing.txt").exists()


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
