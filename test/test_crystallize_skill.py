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


def test_crystallize_gates_on_recurrence_like_the_auto_pass():
    """crystallize must apply the SAME criterion as automatic skill detection.

    Both paths stage into ``auto/.pending/``, so two different bars for what
    qualifies means the queue's contents depend on which path proposed them.
    ``_run_skill_detection`` gates on recurrence; this file used to gate on a
    "non-trivial, reusable procedure" -- complexity, which an elaborate one-off
    satisfies -- and excluded only small things (a trivial one-shot answer, a
    one-off failure), never large one-off ones. Whitespace is normalized because
    the file is hard-wrapped and a line break would otherwise split a phrase.
    """
    from pathlib import Path

    import kiro_crew

    body = (
        Path(kiro_crew.__file__).parent / "builtin_skills" / "crystallize" / "SKILL.md"
    ).read_text(encoding="utf-8")
    text = " ".join(body.split())

    assert "non-trivial, reusable procedure" not in text, (
        "the complexity criterion is the defect the recurrence gate replaced -- "
        "an elaborate one-off satisfies it truthfully"
    )
    assert (
        "recurrence test" in text.lower()
    ), "crystallize must apply the recurrence test, not just describe reusability"
    assert "DIFFERENT target" in text, (
        "naming a different future target is what separates a repeatable method "
        "from a finished task"
    )
    assert (
        "Effort is not evidence of recurrence" in text
    ), "without this, a long difficult one-off session still reads as skill-worthy"
    # crystallize is user-invoked, so it must not inherit the auto pass's
    # "prefer null when uncertain" default -- the human already asked. What it
    # DOES need is that the asking alone does not qualify a one-off.
    assert (
        "Being asked does not make a one-off reusable" in text
    ), "an explicit user request must not be treated as evidence of recurrence"
    # ...but stating that must not become a refusal. The invoker of crystallize
    # IS the reviewer the auto pass is protecting, so declining their request
    # makes them argue past their own tool to capture something they judged
    # worth keeping. Surface the verdict, then let them decide.
    assert "the call is the user's, not yours" in text, (
        "crystallize must defer the final decision to the user rather than "
        "applying the auto pass's refusal default to an explicit request"
    )
    assert "Never silently decline a direct request" in text, (
        "a one-off verdict must be surfaced for the user to overrule, not "
        "turned into a silent refusal"
    )
    for shape in ("one-time audit", "migration", "now answered"):
        assert shape in text, (
            f"crystallize must name {shape!r} as a do-not-crystallize shape, "
            f"matching the auto pass's return-null list"
        )
