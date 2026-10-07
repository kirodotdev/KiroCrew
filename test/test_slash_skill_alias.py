"""A leading ``/name`` that names no command loads the skill ``$name`` would."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_utils import (
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
    slash_skill_alias,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.quick_prompts import QUICK_PROMPTS


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/my-skill", "$my-skill"),
        ("/my-skill please run it", "$my-skill please run it"),
        ("  /my-skill\nmore", "$my-skill\nmore"),
        ("/team/oncall-handover go", "$team/oncall-handover go"),
        ("/5whys", "$5whys"),
    ],
)
def test_alias_rewrites_a_leading_non_command_slash(message, expected):
    assert slash_skill_alias(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "",
        "plain text",
        "see /my-skill later",  # only the FIRST word is ever aliased
        "/",
        "/My-Skill",  # skill token charset is lowercase, as with `$`
        "/-dash",
        "/my-skill: run",  # punctuation makes it not a token
    ],
)
def test_alias_ignores_non_tokens(message):
    assert slash_skill_alias(message) is None


@pytest.mark.parametrize(
    "command",
    sorted(_SLASH_COMMANDS | set(SLASH_COMMAND_DESCRIPTIONS) | set(QUICK_PROMPTS)),
)
def test_commands_and_quick_prompts_always_win(command):
    assert slash_skill_alias(command) is None
    assert slash_skill_alias(f"{command} with args") is None


@pytest.mark.usefixtures("close_skills_loaders")
class TestExpandWithSlashAlias:
    def _state(self, tmp_path: Path, monkeypatch, *skills: tuple[str, str]):
        from kiro_crew.platform.defaults import DefaultMcpToolingProvider
        from kiro_crew.skills import SkillsLoader

        monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])
        skills_dir = tmp_path / "skills"
        for name, body in skills:
            d = skills_dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(body, encoding="utf-8")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state._standalone_skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        return state

    def test_slash_name_loads_the_skill_and_keeps_the_typed_text(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state(
            tmp_path, monkeypatch, ("my-skill", "---\nname: my-skill\n---\nBODY-SLASH")
        )
        out, n = _expand_dollar_skills("/my-skill do it", state, _ChatSlot("s1"), "sess")
        assert n == 1
        assert out.startswith("/my-skill do it")
        assert "[Skill: my-skill]" in out and "BODY-SLASH" in out

    def test_unknown_slash_stays_plain_text_without_audit(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state(tmp_path, monkeypatch, ("my-skill", "---\nname: my-skill\n---\nB"))
        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)
        out, n = chat_runner._expand_dollar_skills("/tmp is full", state, _ChatSlot("s1"), "s")
        assert (out, n) == ("/tmp is full", 0)
        state.push_slots_update.assert_not_called()
        sel_mock.log_tool_invocation.assert_not_called()

    def test_skill_named_like_a_command_is_not_hijacked(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state(tmp_path, monkeypatch, ("compact", "---\nname: compact\n---\nX"))
        out, n = _expand_dollar_skills("/compact", state, _ChatSlot("s1"), "sess")
        assert (out, n) == ("/compact", 0)
        # ...and `$` still reaches it.
        _, n2 = _expand_dollar_skills("$compact", state, _ChatSlot("s2"), "sess")
        assert n2 == 1
