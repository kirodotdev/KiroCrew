"""Consumer coverage for the ``skills-custom`` seeded-home fixture."""

from __future__ import annotations

from kiro_crew.skills import _BUILTIN_SKILLS_DIR, SkillsLoader, _iter_skill_files
from kiro_crew.testing.fixtures import seeded_home

_DESCRIPTION = (
    "Classify a red test as a real regression, a flake, or an environment "
    "problem before patching."
)
_TRIGGERS = "flaky test, red shard, rerun failed, triage failure"


def test_skills_custom_fixture_drives_live_and_pending_readers() -> None:
    with seeded_home("skills-custom") as home:
        pending_dir = home / "skills" / "auto" / ".pending" / "flaky-triage"
        meta_file = pending_dir / ".meta.json"
        assert pending_dir.is_dir()
        assert meta_file.is_file()

        loader = SkillsLoader(skills_path=home / "skills", install_builtins=False)
        live = {entry["key"]: entry for entry in loader.list_skills()}
        builtin_names = {name for name, _path in _iter_skill_files(_BUILTIN_SKILLS_DIR)}

        assert set(live) == {"release-notes"}
        assert live["release-notes"]["name"] == "release-notes"
        assert "auto/flaky-triage" not in live
        assert set(live).isdisjoint(builtin_names)

        pending = loader.list_pending_skills()
        assert [entry["slug"] for entry in pending] == ["flaky-triage"]
        assert pending[0]["name"] == "auto/flaky-triage"
        assert pending[0]["description"] == _DESCRIPTION
        assert pending[0]["triggers"] == _TRIGGERS
        assert pending[0]["source"] == "crystallize"
        assert pending[0]["has_scripts"] is False

        detail = loader.get_pending_skill("flaky-triage")
        assert detail is not None
        assert detail["scripts"] == []
        assert detail["meta"]["slug"] == "flaky-triage"
        assert detail["meta"]["scripts"] == []

        assert loader.approve_pending_skill("flaky-triage") == "auto/flaky-triage"
        assert loader.list_pending_skills() == []
        approved = {entry["key"] for entry in loader.list_skills()}
        assert approved == {"release-notes", "auto/flaky-triage"}
        assert approved.isdisjoint(builtin_names)

    with seeded_home("skills-custom") as home:
        loader = SkillsLoader(skills_path=home / "skills", install_builtins=False)
        assert loader.dismiss_pending_skill("flaky-triage") is True
        assert loader.list_pending_skills() == []
        assert {entry["key"] for entry in loader.list_skills()} == {"release-notes"}
