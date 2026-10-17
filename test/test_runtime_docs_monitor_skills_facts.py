"""Pin a few packaged-doc facts to the code constants they restate.

The pages under ``src/kiro_crew/docs`` ship in the wheel and running agents read
them, so a number that drifts from the code becomes wrong agent behavior. Each
check reads the constant from its own module rather than repeating it here.
"""

from __future__ import annotations

from pathlib import Path

DOCS = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_monitoring_default_agent_turns_row_matches_the_default() -> None:
    from kiro_crew.monitoring.models import DEFAULT_MONITOR_AGENT_TURNS

    # 0 is the "no limit" sentinel; the table must not print a positive cap.
    assert DEFAULT_MONITOR_AGENT_TURNS == 0
    assert "| Completed agent turns | 0 (no limit) |" in _doc("monitoring.md")


def test_monitor_loops_names_the_runtime_ceiling_and_its_bounds() -> None:
    from kiro_crew.monitoring.limits import (
        DEFAULT_RUNTIME_CEILING_SECS,
        MAX_RUNTIME_CEILING_SECS,
    )

    text = " ".join(_doc("monitor-loops.md").split())
    assert DEFAULT_RUNTIME_CEILING_SECS == 7 * 86_400
    assert MAX_RUNTIME_CEILING_SECS == 30 * 86_400
    assert "`monitoring.max_runtime_secs` (default 7 days, maximum 30 days)" in text


def test_monitor_loops_lists_the_session_start_stand_down() -> None:
    from kiro_crew.autonudge_service.model import SESSION_START_FAILURE_REASON

    assert f"`{SESSION_START_FAILURE_REASON}`" in _doc("monitor-loops.md")


def test_skill_search_read_capacity_matches_the_code() -> None:
    from kiro_crew.skills import SKILL_READ_CAPACITY

    assert SKILL_READ_CAPACITY == 99_000
    assert "about 99,000 bytes" in _doc("skills.md")


def test_remote_crew_links_to_repo_guides_are_absolute() -> None:
    # A relative ../../../docs link is dead inside an installed package.
    assert "../../../docs/" not in _doc("remote-crew.md")
