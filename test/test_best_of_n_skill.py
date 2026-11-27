"""The ``best-of-n`` skill keeps the rules that make it safe to run.

The skill is text an agent follows, so each rule that gates spending or protects
the person's repository is pinned here by one assertion naming the rule, and a
rewrite that drops a sentence fails with the rule's name. No model is called and
no git runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.skills import _SHORT_DESC_CHARS, SkillsLoader
from kiro_crew.skills_body_reads import KAS_SKILL_READ_CAPACITY
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

BUILTIN = Path(kiro_crew.__file__).resolve().parent / "builtin_skills"
SKILL = BUILTIN / "best-of-n" / "SKILL.md"


def _body() -> str:
    return SKILL.read_text(encoding="utf-8")


def _front() -> dict:
    return SkillsLoader._parse_frontmatter(SKILL)


def test_front_matter_names_the_skill_and_stays_off_the_auto_inject_path() -> None:
    front = _front()
    assert front["name"] == "best-of-n"
    assert front["triggers"], "a skill with no triggers is found only by name"
    assert front["inject_on_trigger"] == "false", "a skill that spends credits is never injected"


def test_the_index_line_carries_the_routing_sentence() -> None:
    desc = " ".join(_front()["description"].split())
    assert desc.startswith("Use when the person asks to run one coding task on several models")
    assert len(desc) <= _SHORT_DESC_CHARS, "the routing sentence must fit the index line"
    assert SkillsLoader._short_desc(desc) == desc


def test_the_body_fits_one_read() -> None:
    assert len(_body()) < KAS_SKILL_READ_CAPACITY


@pytest.mark.parametrize(
    ("needle", "rule"),
    [
        ("`spawn_run`", "fans out through one spawn_run call"),
        ("`isolate`", "names the isolate field"),
        ('`"worktree"`', "isolates each worker in a git worktree"),
        ("`cwd`", "passes the repository as cwd"),
        ("`include_memory`", "withholds memory so workers do not share framing"),
        ("`agent.subagent_cwd_allowed_roots`", "names the setting that decides the folder"),
        ("`kas_subagents`", "says a per-run model is not taken under the engine's sub-agents"),
        ("never to an entry of `tasks`", "keeps isolate off the task entries"),
        ("uncommitted changes are not copied", "tells the person uncommitted work is not copied"),
        ("list the models the backend offers", "lists the models instead of copying a list"),
        ("three spread across different vendors", "proposes three across vendors"),
        ("at most four", "caps the count at four"),
        ("typed yes", "waits for a typed yes before spending"),
        ("`rate_multiplier`", "shows each model's cost"),
        ("never push", "never pushes"),
        ("never remove a worktree", "never removes a worktree"),
        ("never commit, stash or reset", "never commits or stashes for the person"),
        ("never widen it", "never widens the allowed roots"),
        ("drop a worker that failed", "carries on without a failed worker"),
        ("only after the person names the winner", "applies only the picked attempt"),
    ],
)
def test_the_body_keeps_the_rule(needle: str, rule: str) -> None:
    flat = " ".join(_body().lower().split())
    assert needle in flat, f"the skill no longer says it {rule}"


@pytest.mark.parametrize(
    "command",
    ["git push", "git worktree remove", "rm -rf", "git stash", "--force", "git reset"],
)
def test_the_body_never_names_a_command_it_must_not_run(command: str) -> None:
    assert command not in _body(), f"the skill names {command!r}, which it must never run"


@pytest.mark.parametrize(
    "text",
    [
        "run this on three models side by side",
        "best of n on this task",
        "try this on several models and let me compare",
    ],
)
def test_a_request_to_compare_models_selects_the_skill(text: str) -> None:
    score, negated = trigger_score(_front()["triggers"], words_of(text))
    assert not negated
    assert score >= MIN_TRIGGER_OVERLAP, f"{text!r} scored {score}"
