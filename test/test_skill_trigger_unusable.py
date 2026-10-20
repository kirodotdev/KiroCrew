"""Trigger matching never injects a skill the turn cannot use.

Four false matches put a skill nobody asked for into a turn (27k chars for
``computer-use``, 17k for a ``restart`` skill):

* an app name firing on the ordinary word: ``computer-use`` lists ``Word``
  (Microsoft Word), and "Reply with exactly the word: pong." matches it unless
  the trigger is marked case-exact (``=Word``);
* a skill for a feature that is off: a session with no ``computer_*`` tool gets
  the skill describing those tools;
* Kiro Crew's own words: a recovery continuation ("do NOT restart from scratch")
  fires a skill whose trigger is ``restart``;
* punctuation the matcher drops: ``missing $`` scores as the lone word
  ``missing`` and fires on any message that uses it.

Each case is pinned beside the nearby input that must still match: the name as
written, the feature on, the same word typed by a person, and the error text.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.skills_discovery import _BUILTIN_SKILLS_DIR
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, rank_triggered, trigger_score, words_of

#: The prompt the latency study sent as every new chat's first message.
PONG = "Reply with exactly the word: pong."
APP_REQUEST = "Open the quarterly report in Word and bold the title."
COMPUTER_USE = "[Skill: computer-use]"
RESTART_BODY = "RESTART PROCEDURE: drain the queue first."
_SHIPPED_PROMPT = _BUILTIN_SKILLS_DIR.parent / "config" / "prompt.md"


def _loader(skills: Path) -> SkillsLoader:
    # The matcher is off at the shipped cap of 0; every case here is about what it
    # matches once an operator turns it on.
    return SkillsLoader(
        skills_path=skills,
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
    )


def _shipped_computer_use(skills: Path) -> None:
    """The packaged skill, so the case tracks its real trigger list."""
    target = skills / "computer-use"
    target.mkdir(parents=True)
    shutil.copy(_BUILTIN_SKILLS_DIR / "computer-use" / "SKILL.md", target / "SKILL.md")


def _restart_skill(skills: Path) -> None:
    """The shape of the user skill a recovery turn fired: one-word ``restart``."""
    target = skills / "gateway-restart"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: gateway-restart\ndescription: Restart the gateway safely\n"
        f"triggers: restart, reload, restart gateway\n---\n{RESTART_BODY}\n",
        encoding="utf-8",
    )


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """``agent._computer_use_spec_gate``, which mounts ``kirocrew-computer``."""
    reading = {"open": True}
    monkeypatch.setattr("kiro_crew.agent._computer_use_spec_gate", lambda: reading["open"])
    return reading


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    # The shipped contract, so a session start takes its Computer Use reading.
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: _SHIPPED_PROMPT)
    skills = tmp_path / "skills"
    _shipped_computer_use(skills)
    _restart_skill(skills)
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=_loader(skills))


def _warm(builder: ContextBuilder, text: str, **kwargs) -> str:
    message, _ = builder.build_message(text, is_new_session=False, session_key="s", **kwargs)
    return message


class TestCaseExactPhrase:
    """A phrase prefixed with ``=`` matches exactly as written; nothing else changes."""

    def test_the_word_in_prose_does_not_fire_a_case_exact_name(self) -> None:
        assert trigger_score("=Word", words_of(PONG))[0] < MIN_TRIGGER_OVERLAP

    def test_the_name_as_written_still_fires_it(self) -> None:
        assert trigger_score("=Word", words_of(APP_REQUEST))[0] == 1.0

    def test_a_case_exact_phrase_needs_every_word_as_written(self) -> None:
        assert trigger_score("=System Events", words_of("ask System Events"))[0] == 1.0
        assert trigger_score("=System Events", words_of("ask system events"))[0] == 0.0

    @pytest.mark.parametrize(
        ("trigger", "text"),
        [("Word", PONG), ("word", APP_REQUEST), ("Playwright", "run the playwright tests")],
    )
    def test_an_unmarked_trigger_keeps_ignoring_case(self, trigger: str, text: str) -> None:
        # Only the author's `=` changes matching, so no existing trigger loses a match.
        assert trigger_score(trigger, words_of(text))[0] == 1.0

    def test_crew_routing_reads_the_same_rule(self) -> None:
        roster = [("office crew", "=Word, =Excel")]
        assert rank_triggered("count every word here", roster) == []
        assert [name for name, _ in rank_triggered("fix it in Excel", roster)] == ["office crew"]


def _shipped_triggers(skill_md: Path) -> str:
    return parse_frontmatter(skill_md.read_text(encoding="utf-8"), SKILL_LOADER).get("triggers", "")


class TestPunctuationInAPhrase:
    """The matcher scores ``\\w+`` words, so a phrase token with none in it vanishes."""

    def test_the_word_in_prose_does_not_fire_the_latex_error_skill(self) -> None:
        skill_md = _BUILTIN_SKILLS_DIR / "papyrus-diagnose-compilation" / "SKILL.md"
        triggers = _shipped_triggers(skill_md)
        prose = words_of("the warning is missing from the run report")
        assert trigger_score(triggers, prose)[0] < MIN_TRIGGER_OVERLAP
        assert trigger_score(triggers, words_of("! Missing $ inserted.")) == (1.0, False)

    def test_no_packaged_phrase_fires_on_one_of_its_words(self) -> None:
        phrases = [
            (skill_md.parent.name, phrase.strip())
            for skill_md in sorted(_BUILTIN_SKILLS_DIR.rglob("SKILL.md"))
            for phrase in _shipped_triggers(skill_md).split(",")
            if len(phrase.split()) > 1 and not phrase.strip().startswith("!")
        ]
        assert phrases
        fired_by_one_word = [
            (skill, phrase)
            for skill, phrase in phrases
            if any(
                trigger_score(phrase, words_of(token))[0] >= MIN_TRIGGER_OVERLAP
                for token in phrase.lstrip("=").split()
            )
        ]
        assert fired_by_one_word == []


class TestComputerUseSkill:
    """The shipped ``computer-use`` skill, matched through ``build_message``."""

    def test_the_pong_prompt_loads_no_computer_use_skill(self, builder, gate) -> None:
        assert COMPUTER_USE not in _warm(builder, PONG)

    def test_lowercase_word_in_prose_loads_none(self, builder, gate) -> None:
        assert COMPUTER_USE not in _warm(builder, "Count every word in this paragraph.")

    def test_word_the_app_still_loads_it_while_the_feature_is_on(self, builder, gate) -> None:
        assert COMPUTER_USE in _warm(builder, APP_REQUEST)

    def test_word_the_app_loads_nothing_while_the_feature_is_off(self, builder, gate) -> None:
        gate["open"] = False
        assert COMPUTER_USE not in _warm(builder, APP_REQUEST)

    @pytest.mark.parametrize(("first", "then"), [(False, True), (True, False)])
    def test_each_session_start_takes_its_own_reading_without_the_prompt_token(
        self, builder, gate, tmp_path, monkeypatch, first: bool, then: bool
    ) -> None:
        # An operator prompt with no Computer Use block never refreshes the
        # reading itself, so a reused session key must not keep its first one.
        prompt = tmp_path / "operator-prompt.md"
        prompt.write_text("You are the operator's own agent.\n", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: prompt)
        for open_, expected in ((first, first), (then, then)):
            gate["open"] = open_
            message, _ = builder.build_message(APP_REQUEST, is_new_session=True, session_key="s")
            assert (COMPUTER_USE in message) is expected

    def test_the_session_reading_holds_until_its_backend_restarts(self, builder, gate) -> None:
        # The agent prompt's Computer Use block is rendered from one reading per
        # session, taken when its backend spawned. A keystone flip with no reset
        # leaves that backend without the tools, so the skill follows that reading.
        gate["open"] = False
        builder.build_message("first turn", is_new_session=True, session_key="s")
        gate["open"] = True
        assert COMPUTER_USE not in _warm(builder, APP_REQUEST)

    def test_a_jev_pick_is_asked_with_the_same_text_and_unusable_set(
        self, builder, gate, monkeypatch
    ) -> None:
        # A pick replaces the match, so its menu must leave out what the matcher
        # leaves out (the menu side is pinned in test_decisions_points.py).
        from kiro_crew.decisions.points import skills_select

        seen: dict = {}

        def spy(_loader, text, _project, **kwargs):
            seen.update(kwargs, text=text)
            return None

        monkeypatch.setattr(skills_select, "selected_skills", spy)
        gate["open"] = False
        _warm(builder, APP_REQUEST, skill_trigger_text="the person's words")
        assert seen["unusable"] == ("computer-use",)
        assert seen["text"] == "the person's words"


class TestTriggerText:
    """``skill_trigger_text`` is what the matcher reads, when a caller names it."""

    def test_an_empty_trigger_text_matches_nothing(self, builder, gate) -> None:
        message = _warm(builder, "Please restart the gateway.", skill_trigger_text="")
        assert RESTART_BODY not in message

    def test_context_outside_the_trigger_text_matches_nothing(self, builder, gate) -> None:
        notice = "[Sub-agent result not delivered] It will restart on its own.\n\n"
        message = _warm(builder, notice + "Summarise the logs.", skill_trigger_text="Summarise.")
        assert RESTART_BODY not in message

    def test_the_trigger_text_still_matches(self, builder, gate) -> None:
        message = _warm(builder, "Please restart the gateway.", skill_trigger_text="restart it")
        assert RESTART_BODY in message


class TestDashboardTurn:
    """The dashboard runner hands the matcher only what the person wrote."""

    async def _turn(self, tmp_path, monkeypatch, message, *, prepare=None, **run_kwargs) -> str:
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        skills = tmp_path / "skills"
        _restart_skill(skills)
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"), skills=_loader(skills)
        )
        builder.conversation_log = MagicMock()
        state = _make_state(tmp_path, context_builder=builder)
        hook_store = MagicMock()
        hook_store.fire = AsyncMock(return_value=[])
        state._hook_store = hook_store
        slot = state.get_or_create_slot("trigger-text")
        if prepare is not None:
            prepare(slot)

        streamed: list[str] = []

        async def stream(prompt):
            streamed.append(prompt)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=10.0)
        # A warm session: no session-start context, only the per-turn parts.
        state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
        state.sessions.get_pid = MagicMock(return_value=None)

        await _run_chat(state, slot, message, **run_kwargs)
        assert len(streamed) == 1
        return streamed[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["_POSTTOKEN_RECOVER_MSG", "_MANUAL_RESUME_MSG"])
    async def test_a_runner_continuation_loads_no_skill(self, tmp_path, monkeypatch, name):
        from kiro_crew.dashboard import chat_utils

        continuation = getattr(chat_utils, name)
        assert "restart" in continuation  # the word the skill's trigger fired on
        prompt = await self._turn(
            tmp_path,
            monkeypatch,
            continuation,
            # What the queue drain passes for a recovery entry the runner wrote.
            _synthetic_payload=True,
            _synthetic_recovery_turn=True,
        )
        # The body line, not the marker: the prompt folds the marker's em dash.
        assert continuation.splitlines()[1][:40] in prompt
        assert RESTART_BODY not in prompt

    @pytest.mark.asyncio
    async def test_a_prepended_notice_loads_no_skill(self, tmp_path, monkeypatch) -> None:
        notice = "[Sub-agent result not delivered] The job will restart on its own."

        def prepare(slot) -> None:
            slot._pending_subagent_failures = [notice]

        prompt = await self._turn(tmp_path, monkeypatch, "Summarise the logs.", prepare=prepare)
        assert notice in prompt
        assert RESTART_BODY not in prompt

    @pytest.mark.asyncio
    async def test_a_person_asking_for_a_restart_still_gets_the_skill(
        self, tmp_path, monkeypatch
    ) -> None:
        prompt = await self._turn(tmp_path, monkeypatch, "Please restart the gateway now.")
        assert RESTART_BODY in prompt
