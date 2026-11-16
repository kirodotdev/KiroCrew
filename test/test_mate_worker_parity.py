# SPDX-License-Identifier: Apache-2.0
"""A Bot's worker carries the work protocol ``kirocrew-worker`` carries, or says why not.

Two agents work one item for a conductor: ``kirocrew-worker`` (Conductor mode and
Ultra Mode workers) and ``kirocrew-mate-worker`` (a Bot's worker). They are prompted
from different places (``agent_parts/spec_prompts.py`` and ``crewmate_prompts.json``
``worker.principles``), so a protocol rule that lands on one is easy to leave off
the other. These tests fail when the worker prompt gains a section that the Bot's
worker is not told and that :data:`ALLOWLIST` does not name with a reason.

The rule they hold (``docs/system-specs/modules/crew-mode.md``, The two workers stay
in step): a work-protocol rule both need is written ONCE, in
``_LEDGER_WORKER_RULES``. ``_WORKER_SYSTEM_PROMPT`` is assembled from it
(``_WORKER_PROMPT_SECTIONS``) and ``worker.principles`` names it as a placeholder
(``mate_worker.mate_worker_prompt()``). The assembled bytes of ``_WORKER_SYSTEM_PROMPT``
are pinned by ``test_agent_refactor_contracts.py``. The mount and grants both workers
need for ``work_brief`` and ``work_report`` are held by
``test_mate_work_ledger_mount.py``.

The direction is one way: the Bot's worker carries much ``kirocrew-worker`` does not
(the missing-capability line, the blocked-command hand-off, its worktree branch),
and that is not drift.
"""

from __future__ import annotations

from test_mate_conductor_parity import _sentences, _words

from kiro_crew import agent, crewmate_prompts

#: Every section of ``_WORKER_SYSTEM_PROMPT`` a Bot's worker does not carry, with the
#: reason. Anything else must be a shared rule.
ALLOWLIST: dict[str, str] = {
    "role": "names kirocrew-worker; a Bot's worker has its own role line naming its Bot",
    "not_bound": "a Bot also hands work over with no ledger item (no ledger, a cron, a spawn), so its worker gets its own not_bound line",
    "wake_lead": "says the report is the ONLY thing that wakes the conductor; a Bot's worker's turn end also wakes its Bot with a [Worker report]",
    "silent_turn": "says a turn that ends with no report wakes nobody; a Bot's worker's turn end wakes its Bot with a [Worker report]",
    "summary_language": "names the goal board as where the person reads summary; the Bot's worker piece names only the language",
    "all_tools": "a Bot's worker runs the Bot's kit, not every default tool, and says what it lacks with the missing-capability line",
    "sealed_crew_paths": "a Bot's worker runs on the default contract, config/prompt.md, which states the same sealed-path rule",
}

#: The Bot piece the shared rules are rendered into.
_MATE_PIECE = "worker.principles"
#: Its one placeholder that is not a shared rule.
_OWN_PLACEHOLDERS = frozenset({"missing_prefix"})

_WHERE = (
    "A work-protocol change lands on both workers, or it lands in ALLOWLIST in "
    "test/test_mate_worker_parity.py with a one-line reason "
    "(docs/system-specs/modules/crew-mode.md, The two workers stay in step)."
)


def _mate_worker_prompt() -> str:
    from kiro_crew.mate_worker import mate_worker_prompt

    return mate_worker_prompt()


def _unclassified(sections: tuple[tuple[str, str], ...]) -> list[str]:
    rules = agent._LEDGER_WORKER_RULES
    return [name for name, _t in sections if name not in rules and name not in ALLOWLIST]


def _stale(sections: tuple[tuple[str, str], ...]) -> list[str]:
    names = {name for name, _t in sections}
    rules = agent._LEDGER_WORKER_RULES
    return sorted(name for name in ALLOWLIST if name not in names or name in rules)


def test_the_worker_prompt_is_its_sections_and_every_rule_is_one_of_them() -> None:
    sections = agent._WORKER_PROMPT_SECTIONS
    assert agent._WORKER_SYSTEM_PROMPT == "".join(text for _n, text in sections)
    names = [name for name, _t in sections]
    assert len(names) == len(set(names)), f"duplicate section names: {names}"
    rules = agent._LEDGER_WORKER_RULES
    for name, text in sections:
        if name in rules:
            assert text is rules[name], f"section {name!r} restates its rule instead of naming it"
    unused = sorted(set(rules) - set(names))
    assert unused == [], f"rules kirocrew-worker never carries: {unused}"


def test_every_worker_section_is_shared_or_allowlisted() -> None:
    assert _unclassified(agent._WORKER_PROMPT_SECTIONS) == [], (
        f"kirocrew-worker prompt sections a Bot's worker neither carries nor allowlists: "
        f"{_unclassified(agent._WORKER_PROMPT_SECTIONS)}. A rule both need goes into "
        f"_LEDGER_WORKER_RULES in src/kiro_crew/agent_parts/spec_prompts.py and its "
        f"placeholder into {_MATE_PIECE} in src/kiro_crew/crewmate_prompts.json. {_WHERE}"
    )


def test_a_planted_worker_only_section_is_caught() -> None:
    planted = (*agent._WORKER_PROMPT_SECTIONS, ("new_rule", "**Always do the new thing.**\n\n"))
    assert _unclassified(planted) == ["new_rule"]


def test_no_allowlist_entry_is_stale() -> None:
    assert _stale(agent._WORKER_PROMPT_SECTIONS) == [], (
        f"ALLOWLIST entries that name no worker-only section: "
        f"{_stale(agent._WORKER_PROMPT_SECTIONS)}"
    )


def test_a_stale_allowlist_entry_is_caught() -> None:
    without_role = tuple(s for s in agent._WORKER_PROMPT_SECTIONS if s[0] != "role")
    assert _stale(without_role) == ["role"]


def test_every_shared_rule_reaches_the_bot_worker() -> None:
    text = _mate_worker_prompt()
    missing = sorted(
        name for name, rule in agent._LEDGER_WORKER_RULES.items() if rule.strip() not in text
    )
    assert missing == [], (
        f"shared work-protocol rules a Bot's worker is not told: {missing}. Add each as "
        f"a {{placeholder}} to {_MATE_PIECE} in src/kiro_crew/crewmate_prompts.json. {_WHERE}"
    )


def test_the_bot_piece_names_only_shared_rules() -> None:
    unknown = sorted(
        crewmate_prompts.placeholders(_MATE_PIECE)
        - set(agent._LEDGER_WORKER_RULES)
        - _OWN_PLACEHOLDERS
    )
    assert unknown == [], f"{_MATE_PIECE} placeholders that name no shared rule: {unknown}"


def _copies(piece: str) -> list[str]:
    flat = _words(piece)
    return sorted(
        f"{name}: {sentence[:60]!r}"
        for name, text in agent._WORKER_PROMPT_SECTIONS
        for sentence in _sentences(text)
        if sentence in flat
    )


def test_no_worker_sentence_is_copied_into_the_bot_piece() -> None:
    """A copy reads the same today and nothing keeps it so. The shared rules reach the
    Bot's worker by placeholder; a worker-only sentence a Bot needs is a shared rule."""
    copies = _copies(crewmate_prompts.text(_MATE_PIECE))
    assert copies == [], (
        f"{_MATE_PIECE} copies kirocrew-worker text: {copies}. Move the rule into "
        f"_LEDGER_WORKER_RULES and name it as a placeholder. {_WHERE}"
    )


def test_the_copy_check_sees_a_reworded_copy() -> None:
    """Positive control: a worker-only sentence with its line breaks moved is still
    caught."""
    planted = (
        "A turn of yours that ends having reported nothing wakes nobody — it is not read "
        "as a result, and nothing is inferred from it."
    )
    assert _copies(planted)


def test_the_bot_worker_is_told_its_turn_end_also_reaches_its_bot() -> None:
    """The wake rule is split: what is true for both is shared, and the Bot's worker
    hears that its reply reaches its Bot too, never that a silent turn wakes nobody."""
    text = _mate_worker_prompt()
    assert "Your Bot is the conductor the rules below name." in text
    assert "Your Bot also reads your reply when a turn of yours ends" in text
    # A Bot's worker with no item still hands its Bot the plan, in its reply.
    assert "open your reply with those five plan lines" in text
    for name in ("wake_lead", "silent_turn"):
        section = dict(agent._WORKER_PROMPT_SECTIONS)[name].strip()
        assert section not in text, name


def test_both_workers_carry_the_same_coding_rules() -> None:
    """kirocrew-worker and a crewmate's worker read one text, so they cannot drift."""
    from kiro_crew.agent_materialization.worker_agent import worker_prompt
    from kiro_crew.context_assembly.output_contract import ASK_CORE, WORKER_ASK_TAIL

    rules = crewmate_prompts.text("worker.coding_rules")
    assert "smallest complete change" in rules
    assert "`shortcut: <the limit>, <when to upgrade>`" in rules
    # Both end with the coding rules, then the shared ask core and worker tail.
    tail = f"{rules}\n\n{ASK_CORE}{WORKER_ASK_TAIL}"
    assert worker_prompt().startswith(agent._WORKER_SYSTEM_PROMPT.rstrip())
    assert worker_prompt().endswith(tail)
    assert _mate_worker_prompt().endswith(tail)
