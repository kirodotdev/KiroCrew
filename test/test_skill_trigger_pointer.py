"""A skill can opt out of full-body injection on a trigger match.

Full-body injection stays the default, so forgetting the field cannot silently
downgrade a mandate to an agent-optional read. ``inject_on_trigger: false`` is the
per-skill statement that the skill is an offer, and it reduces the skill to a
pointer line. Every assertion here fails if the default flips or the opt-out stops
being honored.
"""

from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.context_blocks import split_blocks
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import _SHORT_DESC_CHARS, SkillsLoader

BODY_SENTINEL = "STEP ONE: pour the concrete before the rebar."
HINT_HEADER = "[Relevant skills for this message]"


def _write_skill(
    root: Path,
    name: str,
    *,
    triggers: str | None = "zebra quokka",
    always: bool = False,
    inject: str | None = None,
    description: str = "Lay a foundation",
    body: str = BODY_SENTINEL,
) -> Path:
    d = root / name
    d.mkdir(parents=True)
    fm = f"---\nname: {name}\ndescription: {description}\n"
    if triggers:
        fm += f"triggers: {triggers}\n"
    if always:
        fm += "always: true\n"
    if inject is not None:
        fm += f"inject_on_trigger: {inject}\n"
    (d / "SKILL.md").write_text(fm + f"---\n{body}")
    return d / "SKILL.md"


def _builder(tmp_path: Path, skills: SkillsLoader) -> ContextBuilder:
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=skills)


def _loader(skills_path: Path, *, cap: int = 3) -> SkillsLoader:
    """Build a loader with the trigger matcher enabled.

    ``max_triggered`` defaults to 0 (matcher off) and is snapshotted at
    construction, so every test in this file — whose whole subject is
    trigger-match delivery — must inject a positive cap via ``config=``.
    """
    return SkillsLoader(
        skills_path=skills_path,
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=cap)),
    )


class TestDefaultIsInjection:
    """Forgetting the field must not silently stop a skill being delivered."""

    def test_absent_field_injects_the_full_body(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        loader = _loader(skills)

        msg, _ = _builder(tmp_path, loader).build_message("zebra quokka", is_new_session=False)

        assert BODY_SENTINEL in msg
        assert "[Skill: foundation]" in msg
        assert HINT_HEADER not in msg

    def test_absent_field_splits_as_body(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "plain")
        loader = _loader(skills)

        assert loader.split_triggered(["plain"]) == (["plain"], [])

    @pytest.mark.parametrize("value", ["true", "TRUE", "yes", "1", "", "garbage"])
    def test_only_an_explicit_false_opts_out(self, tmp_path: Path, value: str) -> None:
        """Anything that is not `false` means inject — the safe direction."""
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation", inject=value)
        loader = _loader(skills)

        assert loader.split_triggered(["foundation"]) == (["foundation"], [])

    @pytest.mark.parametrize("value", ["false", "FALSE", " False "])
    def test_false_is_case_and_space_insensitive(self, tmp_path: Path, value: str) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation", inject=value)
        loader = _loader(skills)

        assert loader.split_triggered(["foundation"]) == ([], ["foundation"])


class TestOptedOutSkill:
    def test_body_stays_out_of_the_message(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        path = _write_skill(skills, "foundation", inject="false")
        loader = _loader(skills)

        msg, _ = _builder(tmp_path, loader).build_message("zebra quokka", is_new_session=False)

        assert BODY_SENTINEL not in msg
        assert HINT_HEADER in msg
        assert str(path) in msg

    def test_hint_names_the_skill_and_its_path(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        path = _write_skill(skills, "foundation", inject="false")
        loader = _loader(skills)

        hint = loader.trigger_hint(["foundation"])

        assert "foundation" in hint
        assert str(path) in hint
        assert str(path.parent) in hint
        assert BODY_SENTINEL not in hint

    def test_hint_tells_the_agent_it_may_already_have_the_skill(self, tmp_path: Path) -> None:
        """The caveat is load-bearing, not decoration.

        Without it the agent re-reads a skill whose body is already in the
        replayed ACP history, spending a tool round-trip to put that body back
        into the window as tool output — worse than the injection it replaces.
        """
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation", inject="false")
        loader = _loader(skills)

        assert "already appears earlier in this conversation" in loader.trigger_hint(["foundation"])

    def test_long_description_is_truncated(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(
            skills, "verbose", inject="false", description="x" * (_SHORT_DESC_CHARS + 200)
        )
        loader = _loader(skills)

        hint = loader.trigger_hint(["verbose"])

        assert "x" * _SHORT_DESC_CHARS in hint
        assert "x" * (_SHORT_DESC_CHARS + 1) not in hint
        assert "…" in hint

    def test_unknown_name_yields_no_block(self, tmp_path: Path) -> None:
        loader = _loader(tmp_path / "skills")
        assert loader.trigger_hint(["does-not-exist"]) == ""

    def test_empty_selection_yields_no_block(self, tmp_path: Path) -> None:
        loader = _loader(tmp_path / "skills")
        assert loader.trigger_hint([]) == ""


class TestMixedAndUnchangedPaths:
    def test_a_mixed_match_emits_both_a_body_and_a_pointer(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "mandated")
        _write_skill(skills, "offered", inject="false", body="POINTER ONLY BODY")
        loader = _loader(skills)

        msg, _ = _builder(tmp_path, loader).build_message("zebra quokka", is_new_session=False)

        assert BODY_SENTINEL in msg
        assert "POINTER ONLY BODY" not in msg
        assert HINT_HEADER in msg

    def test_split_preserves_order_across_both_sides(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "a-ptr", inject="false")
        _write_skill(skills, "b-body")
        _write_skill(skills, "c-ptr", inject="false")
        loader = _loader(skills)

        assert loader.split_triggered(["a-ptr", "b-body", "c-ptr"]) == (
            ["b-body"],
            ["a-ptr", "c-ptr"],
        )

    def test_unknown_name_is_dropped_from_both_sides(self, tmp_path: Path) -> None:
        loader = _loader(tmp_path / "skills")
        assert loader.split_triggered(["ghost"]) == ([], [])

    def test_always_skill_keeps_its_full_body(self, tmp_path: Path) -> None:
        """Pinning is untouched — the matcher skips `always: true` entirely."""
        skills = tmp_path / "skills"
        _write_skill(skills, "pinned", triggers=None, always=True)
        loader = _loader(skills)

        assert BODY_SENTINEL in _builder(tmp_path, loader).build_session_context()

    def test_no_match_emits_nothing(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation", inject="false")
        loader = _loader(skills)

        msg, _ = _builder(tmp_path, loader).build_message(
            "unrelated wording", is_new_session=False
        )

        assert HINT_HEADER not in msg
        assert BODY_SENTINEL not in msg

    @pytest.mark.parametrize("cap", [0, 3])
    def test_max_triggered_zero_disables_the_path(self, tmp_path: Path, cap: int) -> None:
        """The floor lift is what makes the whole path switchable off."""
        skills = tmp_path / "skills"
        _write_skill(skills, "foundation")
        loader = _loader(skills)
        loader._max_triggered = cap

        msg, _ = _builder(tmp_path, loader).build_message("zebra quokka", is_new_session=False)

        assert (BODY_SENTINEL in msg) is (cap > 0)


class TestDeliveryIsAuditable:
    """An opted-out skill the agent declines to read leaves no other trace.

    Without the recorded split, "the skill stopped being followed" is
    indistinguishable from "the skill never matched".
    """

    def test_sel_metadata_separates_bodies_from_pointers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skills = tmp_path / "skills"
        _write_skill(skills, "mandated")
        _write_skill(skills, "offered", inject="false")
        loader = _loader(skills)

        captured: dict[str, str] = {}

        class _Sel:
            def log_tool_invocation(self, **kwargs: object) -> None:
                meta = kwargs.get("metadata")
                if isinstance(meta, dict):
                    captured.update(meta)

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: _Sel())

        loader.get_triggered_skills("zebra quokka")

        assert captured["bodies"] == "mandated"
        assert captured["pointers"] == "offered"


class TestBlockAttribution:
    def test_hint_is_attributed_to_skill_hint_not_the_preceding_block(self) -> None:
        """Mis-attribution would corrupt the measurement this knob is judged by."""
        prompt = f"[PROJECT] dir\n\n{HINT_HEADER}\n- **a**: d → `/p`\n"

        blocks = split_blocks(prompt)

        assert blocks.get("skill_hint", 0) > 0
        assert "loaded_skill" not in blocks
