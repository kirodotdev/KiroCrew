"""The dashboard's inject-on-trigger toggle on a SKILL.md saved with a BOM."""

from __future__ import annotations

from kiro_crew.skills import SkillsLoader


def test_the_toggle_edits_a_bom_skill_and_keeps_the_mark(tmp_path) -> None:
    skill = tmp_path / "bom"
    skill.mkdir()
    md = skill / "SKILL.md"
    md.write_bytes("\ufeff---\nname: bom\ndescription: has bom\n---\nbody\n".encode())
    loader = SkillsLoader(skills_path=tmp_path, install_builtins=False)

    assert loader.set_inject_on_trigger("bom", False) is True
    off = md.read_bytes().decode("utf-8")
    assert (
        off == "\ufeff---\nname: bom\ndescription: has bom\ninject_on_trigger: false\n---\nbody\n"
    )

    assert loader.set_inject_on_trigger("bom", True) is True
    assert (
        md.read_bytes().decode("utf-8") == "\ufeff---\nname: bom\ndescription: has bom\n---\nbody\n"
    )
