"""Phase-3 test: the crystallize builtin skill is installed + trigger-matches."""

from __future__ import annotations

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skills import SkillsLoader


def test_crystallize_builtin_present_and_triggers(tmp_path):
    # max_triggered defaults to 0 (matcher off) and is snapshotted at
    # construction, so the trigger assertions below need a positive cap.
    loader = SkillsLoader(
        skills_path=tmp_path / "skills",
        install_builtins=True,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
    )
    keys = {s["key"] for s in loader.list_skills()}
    assert "crystallize" in keys

    meta = next(s for s in loader.list_skills() if s["key"] == "crystallize")
    assert "reusable skill" in meta["description"].lower()

    # Trigger phrases the user would say.
    for phrase in ("crystallize this session", "create a skill from this", "make this reusable"):
        assert "crystallize" in loader.get_triggered_skills(phrase)
