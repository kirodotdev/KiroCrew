"""``needs_human`` means one of five asks, and the conductor checks the claim.

A plain design choice tagged ``needs_human`` would make the conductor relay the
worker's options to the person instead of taking the recommendation. Two texts
prevent that: the worker prompt (and the ``work_report`` schema it calls) limit
``needs_human`` to the five ask cases, and the goal-conductor skill says the reason
is the worker's claim, checked against those cases before anyone is asked.
"""

from pathlib import Path

from kiro_crew.agent_parts.spec_prompts import _WORKER_SYSTEM_PROMPT
from kiro_crew.mcp_work import _list_tools

_SKILL = (
    Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/goal-conductor/SKILL.md"
)

# One marker per ask case, each a word the case cannot be stated without.
_FIVE_ASKS = ("credentials", "spend", "someone else's work", "preference", "said they want")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _report_description() -> str:
    (tool,) = [t for t in _list_tools() if t["name"] == "work_report"]
    return _flat(tool["description"])


def test_the_worker_prompt_and_schema_limit_needs_human_to_the_five_asks():
    prompt = _flat(_WORKER_SYSTEM_PROMPT)
    start = prompt.index("`needs_human` only for one of five asks")
    rule = prompt[start : start + 700]
    schema = _report_description()
    for case in _FIVE_ASKS:
        assert case in rule, case
        assert case in schema, case
    assert "A choice you can recommend is `question` WITHOUT `needs_human`" in rule
    assert "recommendation and why" in rule
    assert "without needs_human, naming your recommendation and why" in schema


def test_the_conductor_skill_checks_needs_human_instead_of_trusting_it():
    skill = _flat(_SKILL.read_text(encoding="utf-8"))
    start = skill.index("**`needs_human` is the worker's claim, not a fact: check it.**")
    rule = skill[start : start + 1400]
    for case in (
        "credentials",
        "spending money",
        "someone else's work",
        "preference",
        "said they want",
    ):
        assert case in rule, case
    assert "record `action=decide`, `session_send` it to the worker" in rule
    # The nearby cases: a plain design choice is decided, a branch delete and a
    # token are asked.
    assert "a plain design choice" in rule and "is yours" in rule
    assert "deletes another contributor's branch is ask 3" in rule
    assert "needs a token only the person holds is ask 1" in rule
    assert "you cannot answer a worker's question unattended" not in skill
