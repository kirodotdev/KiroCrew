"""Which globs narrow a turn's triggered skills.

``build_message`` keeps only the triggered skills the session's pinned globs
admit when a pin exists, else those the agent's mapped globs admit, else every
one. An empty glob list, from either source, narrows nothing. A pin that
resolves to no globs is still the pin's answer: the agent-name lookup is never
consulted beside it, which is what the pin exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import context
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore

_DEFAULT_AGENT = AGENT_FILENAME.removesuffix(".json")


def _turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str | None,
    pinned: list[str] | None,
    agent_globs: list[str],
) -> tuple[list[str], list[str | None]]:
    """Run one turn matching ``alpha`` and ``beta``.

    Returns the skills handed on for delivery and the agents whose mapping was
    looked up. A glob here is a skill key, so a mapping admits the keys it names.
    """
    looked_up: list[str | None] = []

    def fake_agent_skill_globs(name, agents_dir=None, **_kwargs):
        looked_up.append(name)
        return list(agent_globs)

    monkeypatch.setattr(context, "pinned_skill_globs", lambda *_a, **_k: pinned)
    monkeypatch.setattr(context, "agent_skill_globs", fake_agent_skill_globs)

    loader = MagicMock()
    loader.get_triggered_skills.return_value = ["alpha", "beta"]
    loader.scoped_skills.side_effect = lambda *, project_dir=None, only=None: [
        {"key": key} for key in only or ()
    ]
    loader.split_triggered.side_effect = lambda triggered, _project: (list(triggered), [])
    loader.load_skill.return_value = "body"
    loader.strip_frontmatter.return_value = "body"
    loader.trigger_hint.return_value = ""
    builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)

    builder.build_message("trigger", is_new_session=False, session_key="s1", agent=agent)

    loader.split_triggered.assert_called_once()
    return loader.split_triggered.call_args.args[0], looked_up


def test_a_pin_narrows_the_set_and_the_agent_mapping_is_not_read(tmp_path, monkeypatch):
    delivered, looked_up = _turn(
        tmp_path, monkeypatch, agent=_DEFAULT_AGENT, pinned=["alpha"], agent_globs=["beta"]
    )

    assert delivered == ["alpha"]
    assert looked_up == []


def test_an_empty_pin_still_keeps_the_agent_mapping_unread(tmp_path, monkeypatch):
    _delivered, looked_up = _turn(
        tmp_path, monkeypatch, agent=_DEFAULT_AGENT, pinned=[], agent_globs=["beta"]
    )

    assert looked_up == []


def test_without_a_pin_the_agent_mapping_narrows_the_set(tmp_path, monkeypatch):
    delivered, looked_up = _turn(
        tmp_path, monkeypatch, agent=_DEFAULT_AGENT, pinned=None, agent_globs=["beta"]
    )

    assert delivered == ["beta"]
    assert looked_up == [_DEFAULT_AGENT]


def test_with_no_pin_and_no_agent_nothing_narrows_the_set(tmp_path, monkeypatch):
    delivered, looked_up = _turn(
        tmp_path, monkeypatch, agent=None, pinned=None, agent_globs=["beta"]
    )

    assert delivered == ["alpha", "beta"]
    assert looked_up == []
