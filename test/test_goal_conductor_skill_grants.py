"""The goal-conductor skill names every verb the conductor spec auto-approves.

The skill's "Reads and creates do not prompt" list is what a conductor reads to
know which calls run unattended. A verb granted in ``agent.py`` but missing from
that list (``work_ledger_rebuild``, ``session_status``) reads as one that prompts.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import agent

SKILL = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "goal-conductor"
    / "SKILL.md"
)


def _verbs(grants: tuple[str, ...]) -> list[str]:
    return [g.rsplit("/", 1)[-1] for g in grants]


def test_every_granted_dashboard_and_work_verb_is_named() -> None:
    text = SKILL.read_text(encoding="utf-8")
    granted = _verbs(agent._CONDUCTOR_DASHBOARD_GRANTS) + _verbs(
        agent._LEDGER_CONDUCTOR_WORK_GRANTS
    )
    missing = [v for v in granted if f"`{v}`" not in text]
    assert not missing, f"goal-conductor SKILL.md does not name granted verbs: {missing}"


def test_ledger_rebuild_is_named_as_the_cache_dirty_recovery() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "`cache_dirty`" in text
    assert "kirocrew-work::work_ledger_rebuild" in text
