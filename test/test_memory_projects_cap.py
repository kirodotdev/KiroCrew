"""Active Projects past the startup cap: say so on write, and say how much was cut.

Session startup injects ``projects.md`` cut at ``_MEMORY_PROJECTS_CAP``. Before,
an oversize write succeeded silently and the injected text ended in a bare
``…[truncated]``, so nobody could tell the file had outgrown what sessions see.
"""

from __future__ import annotations

import logging

from kiro_crew.context_assembly.budget import _MEMORY_PROJECTS_CAP
from kiro_crew.memory import MemoryStore


def _store(tmp_path) -> MemoryStore:
    store = MemoryStore(workspace=tmp_path / "ws")
    store.init()
    return store


def test_oversize_write_logs_a_warning_and_keeps_the_file_whole(tmp_path, caplog):
    store = _store(tmp_path)
    content = "# Active Projects\n" + "p" * (_MEMORY_PROJECTS_CAP + 500)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.memory"):
        assert store.write_projects(content) is True

    assert store.read_projects() == content + "\n"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("startup cap" in w and str(_MEMORY_PROJECTS_CAP) in w for w in warnings)


def test_write_within_the_cap_logs_nothing(tmp_path, caplog):
    store = _store(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.memory"):
        store.write_projects("# Active Projects\n- one project\n")

    assert not [r for r in caplog.records if "startup cap" in r.getMessage()]


def test_overflow_counts_the_chars_past_the_cap():
    from kiro_crew.memory import projects_cap_overflow

    assert projects_cap_overflow("x" * _MEMORY_PROJECTS_CAP) == 0
    assert projects_cap_overflow("x" * (_MEMORY_PROJECTS_CAP + 7)) == 7


def test_truncation_marker_names_how_many_chars_were_cut(tmp_path):
    store = _store(tmp_path)
    store.write_projects("# Active Projects\n" + "p" * 300)

    payload = store.get_context(query="", projects_cap=100, include_activity=True)

    section = payload[payload.index("## Active Projects") :]
    assert "…[truncated] (" in section
    omitted = int(section.split("…[truncated] (", 1)[1].split(" chars omitted)", 1)[0])
    assert omitted == len("# Active Projects\n" + "p" * 300 + "\n") - 100
