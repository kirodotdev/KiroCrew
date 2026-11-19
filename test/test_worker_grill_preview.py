"""The ``worker_grill`` preview decides whether a conductor is told to offer a grill.

Off (the default), a conductor's turn carries no grill offer and no ``[GRILL:]``
format, and the goal-conductor skill keeps main's debate rule unchanged.
On, a conductor's dashboard turn carries the offer, the ``[GRILL:]`` line and the
pointer to the quick-chat protocol; any other agent's turn still carries nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import context as ctx
from kiro_crew import worker_grill_preview as grill
from kiro_crew.context_assembly import turn

SKILL_DIR = Path(grill.__file__).resolve().parent / "builtin_skills" / "goal-conductor"


def _cfg(previews: object) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews=previews, dynamic_dashboard_cards=False)
    )


def _guidance(monkeypatch, *, agent: str | None, previews: object) -> str:
    monkeypatch.setattr(ctx.live, "snapshot", lambda: _cfg(previews))
    return "".join(
        turn.interactive_guidance(
            interactive=True,
            session_key="dashboard:chat-1",
            agent=agent,
            minimal_context=False,
            producer_is_user_facing=True,
        )
    )


@pytest.mark.parametrize("agent", sorted(grill.GRILL_AGENTS))
@pytest.mark.parametrize("previews", [{}, {"worker_grill": False}, None, {"worker_grill": "1"}])
def test_off_a_conductor_turn_offers_no_discussion(monkeypatch, agent, previews) -> None:
    text = _guidance(monkeypatch, agent=agent, previews=previews)
    assert "[OPTIONS:" in text
    assert "[GRILL:" not in text
    assert "grill" not in text.lower()
    assert "worker-grill.md" not in text


@pytest.mark.parametrize("agent", sorted(grill.GRILL_AGENTS))
def test_on_a_conductor_turn_carries_the_offer_and_the_card_line(monkeypatch, agent) -> None:
    text = _guidance(monkeypatch, agent=agent, previews={"worker_grill": True})
    assert grill.GUIDANCE in text
    assert "Let workers grill it first" in text
    assert "[GRILL: {" in text
    assert "references/worker-grill.md" in text
    assert "may be in any language" in text
    assert "three labelled lines (question, must, not discussing)" in text
    assert "in whatever language it comes" in text and "never a second moderator" in text
    # The grill ends in a brief with a rerun choice, never in questions.
    assert "ends with a brief, never questions" in text
    assert "run another grill" in text
    # The card line comes with plain start/skip choices in the owner's language,
    # so the owner can answer on a surface that draws no discussion card.
    assert "[OPTIONS:] line of two choices in the owner's language" in text
    # The card's key line is a wire value the conductor dedupes on, via the item title.
    assert "`key: <value>` line" in text
    assert "never translated, matched only as a whole line, its value opaque" in text
    assert "end its item title with `grill key:<value>`" in text
    assert "ends in exactly" in text
    assert "open or closed" in text and "with no key line" in text


def test_the_example_minutes_stay_15() -> None:
    # The conductor always fills `minutes` from the example, so the example is the
    # real default; the trial showed 10 minutes was too tight.
    ref = (SKILL_DIR / "references" / "worker-grill.md").read_text(encoding="utf-8")
    guidance = re.search(r"\[GRILL: (\{.*\})\]", grill.GUIDANCE).group(1)
    example = re.search(r"^\s*\[GRILL: (\{.*\})\]$", ref, re.MULTILINE).group(1)
    assert json.loads(guidance)["minutes"] == 15
    assert json.loads(example)["minutes"] == 15


def test_the_protocol_keys_dedupe_and_reports_before_round_one() -> None:
    ref = (SKILL_DIR / "references" / "worker-grill.md").read_text(encoding="utf-8")
    start = ref.split("3. **Start.**", 1)[1].split("4. **Steer.**", 1)[0]
    assert "`key: <value>`" in start and "never translated" in start
    assert "`grill key:<value>`" in start and "ends in exactly" in start
    assert "open or closed" in start
    assert "same question already has an open grill item" in start
    assert "last 32 closed items" in start and "two moderators" in start
    thread = ref.split("1. **Thread.**", 1)[1].split("2. **Rounds.**", 1)[0]
    assert "Before round 1" in thread and "`discussion`" in thread
    stop = ref.split("6. **Stop**", 1)[1].split("7. **Settle.**", 1)[0]
    assert "start no new round; finish the round in flight" in stop
    assert "6 rounds" in stop


@pytest.mark.parametrize("agent", [None, "kirocrew", "kirocrew-worker"])
def test_on_a_non_conductor_turn_carries_nothing(monkeypatch, agent) -> None:
    text = _guidance(monkeypatch, agent=agent, previews={"worker_grill": True})
    assert "[GRILL:" not in text


def test_the_lts_lock_reads_the_preview_as_off(monkeypatch) -> None:
    from kiro_crew import edition_locks

    monkeypatch.setattr(edition_locks, "feature_previews", lambda _previews: {})
    assert grill.guidance_for("kirocrew-conductor", _cfg({"worker_grill": True})) == ""


def test_the_example_card_line_is_valid_and_within_the_contract() -> None:
    # The guidance's own example must be a card the dashboard would draw.
    line = re.search(r"\[GRILL: (\{.*\})\]", grill.GUIDANCE).group(1)
    card = json.loads(line)
    assert set(card) == {"q", "must", "skip", "who", "minutes"}
    assert 2 <= len(card["who"]) <= 3
    assert isinstance(card["minutes"], int) and 1 <= card["minutes"] <= 60
    ref = (SKILL_DIR / "references" / "worker-grill.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"^\s*\[GRILL: (\{.*\})\]$", ref, re.M).group(1))
    assert all(len(example[k]) <= 200 for k in ("q", "must", "skip"))
    assert 2 <= len(example["who"]) <= 3
    assert all(len(w["name"]) <= 20 and len(w["stance"]) <= 80 for w in example["who"])


def test_off_the_skill_keeps_mains_debate_offer() -> None:
    # With the preview off the conductor reads only the skill, so the skill keeps
    # main's rule for offering a debate; the grill offer lives in the guidance, and
    # the skill names it only as what replaces that rule when the guidance is there.
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    section = skill.split("### Worker debate", 1)[1].split("\n## ", 1)[0]
    assert "Offer a debate only for a costly, contested choice" in section
    assert '"When to offer a debate"' in section
    assert "references/worker-debate.md" in section
    debate = (SKILL_DIR / "references" / "worker-debate.md").read_text(encoding="utf-8")
    offer = debate.split("## When to offer a debate", 1)[1]
    assert "Offer one unasked only when the task is a complex analysis" in offer
    assert '"Worker grill preview is on" paragraph,\nit replaces the unasked offer' in section
    assert "[GRILL:" not in section
    assert (SKILL_DIR / "references" / "worker-debate.md").is_file()


def test_the_protocol_ends_in_a_brief_not_in_questions() -> None:
    ref = (SKILL_DIR / "references" / "worker-grill.md").read_text(encoding="utf-8")
    assert "artifacts.brief" in ref
    assert "moderator picked this for you" in ref
    assert "no `status=question`" in ref
    assert "`asking`" not in ref
    assert "work_ask_record" not in ref


def test_a_multi_line_start_and_its_pictures_reach_the_moderator() -> None:
    # The card writes a field's further lines starting with two spaces and puts its
    # pictures ahead of the opening line; the conductor must read both that way.
    ref = (SKILL_DIR / "references" / "worker-grill.md").read_text(encoding="utf-8")
    start = ref.split("3. **Start.**", 1)[1].split("**Card key.**", 1)[0]
    assert "start with two spaces" in start
    assert "each picture's absolute path as written" in start
    assert "two spaces continues the labelled line" in grill.GUIDANCE
    assert "each picture's absolute" in grill.GUIDANCE
