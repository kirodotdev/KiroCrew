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


def _section(start: str, end: str) -> str:
    body = _body()
    head = body.index(start)
    return body[head : body.index(end, head)]


_STEP_5 = """## Step 5: keep one

Only after the person names the winner, and after they agree to a branch name,
create that branch in the main checkout and apply the winner's commits to it
with `git cherry-pick <base>..<winner head>`. On a conflict, stop and report it.
Never push.

The other worktrees stay where they are. Give their paths so the person can open
them. A clean worktree is removed by Kiro Crew when the run's record expires, and
one with commits is kept. Never remove a worktree or a folder yourself unless
the person asks.

"""


def test_step_5_is_the_text_that_gates_the_cherry_pick() -> None:
    assert _section("## Step 5", "## Refusals") == _STEP_5


@pytest.mark.parametrize(
    ("needle", "rule"),
    [
        ("`<mcwidget>`", "shows the comparison as one widget card"),
        ("the plain table above is the answer", "keeps the plain table off the dashboard"),
        ('`data-action="keep_attempt"`', "names the keep action"),
        ("""`data-payload='{"attempt":n}'`""", "sends the attempt's number only"),
        ("holds nothing else", "keeps the payload to the number"),
        ("gets no row and no button", "offers no button for an attempt with no commit"),
        ("goes into the card as escaped text", "escapes text that a model wrote"),
        ("only pre-fills the composer", "says a click does not send"),
        ("the person still presses send", "leaves the send to the person"),
        ("never say a click kept or applied an attempt", "never calls a click a keep"),
        ("never from the reply", "reads the worker from its own table"),
        ("go on to step 5 for the branch name", "still asks for the branch name"),
    ],
)
def test_the_card_keeps_the_rule(needle: str, rule: str) -> None:
    card = " ".join(_section("### The card", "## Step 5").lower().split())
    assert needle in card, f"the card no longer says it {rule}"


def test_the_card_sits_in_the_compare_step_and_adds_no_second_trigger() -> None:
    assert "keep_attempt" not in _section("## Step 0", "## Step 4")
    assert "keep_attempt" not in _section("## Step 5", "## Refusals")
    assert _front()["inject_on_trigger"] == "false"
