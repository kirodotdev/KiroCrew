"""``needs_human`` means one of five asks, and the conductor checks the claim.

A plain design choice tagged ``needs_human`` would make the conductor relay the
worker's options to the person instead of taking the recommendation. Two texts
prevent that: the worker prompt (and the ``work_report`` schema it calls) limit
``needs_human`` to the five ask cases, stated once in ``ASK_CORE`` and pointed at as
"(a)-(e)" everywhere else, and the goal-conductor skill says the reason is the
worker's claim, checked against those cases before anyone is asked.
"""

from pathlib import Path

from skill_script_helpers import skill_text

from kiro_crew.agent_materialization.worker_agent import worker_prompt
from kiro_crew.context_assembly.output_contract import (
    _CRITICAL_RULES_VARIANTS,
    ASK_CORE,
    WORKER_ASK_TAIL,
)
from kiro_crew.mcp_work import _list_tools

_SKILL = (
    Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/goal-conductor/SKILL.md"
)

# One marker per ask case, each a word the case cannot be stated without.
_FIVE_ASKS = ("credentials", "spend", "someone else's work", "preference", "said they will decide")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _report_description() -> str:
    (tool,) = [t for t in _list_tools() if t["name"] == "work_report"]
    return _flat(tool["description"])


def test_the_worker_prompt_and_schema_limit_needs_human_to_the_five_asks():
    prompt = _flat(worker_prompt())
    for case in _FIVE_ASKS:
        assert case in ASK_CORE, case
    assert _flat(ASK_CORE + WORKER_ASK_TAIL) in prompt
    assert "Set `needs_human` only for (a)-(e)." in prompt
    start = prompt.index("`needs_human` only for an ask case (a)-(e)")
    rule = prompt[start : start + 400]
    assert "A choice you can recommend is `question` WITHOUT `needs_human`" in rule
    assert "recommendation and why" in rule
    # The schema points at the cases the CRITICAL RULES state, rather than copying them.
    schema = _report_description()
    assert "'needs_human' only for an ask case (a)-(e) in your CRITICAL RULES" in schema
    for variant in _CRITICAL_RULES_VARIANTS:
        assert ASK_CORE in variant
    assert "without needs_human, naming your recommendation and why" in schema


def test_the_conductor_skill_checks_needs_human_instead_of_trusting_it():
    skill = _flat(skill_text(_SKILL))
    start = skill.index("**`needs_human` is the worker's claim, not a fact: check it.**")
    rule = skill[start : start + 1000]
    assert "only for (a)-(e) in your rules" in rule
    assert "record `action=decide`, `session_send` it to the worker" in rule
    # The nearby cases: a plain design choice is decided, a branch delete and a
    # token are asked.
    assert "a plain design choice" in rule and "is yours" in rule
    assert "deletes another contributor's branch is (c)" in rule
    assert "needs a token only the person holds is (a)" in rule
    assert "you cannot answer a worker's question unattended" not in skill
