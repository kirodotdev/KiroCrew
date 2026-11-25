"""Which globs narrow a turn's triggered skills.

``build_message`` keeps only the triggered skills the session's pinned globs
admit when a pin exists, else those the agent's mapped globs admit, else every
one. An empty glob list, from either source, narrows nothing. A pin that
resolves to no globs is still the pin's answer: the agent-name lookup is never
consulted beside it, which is what the pin exists to prevent. The agent lookup
names the turn's session: a Bot's worker's mapping lives in that session's
merged spec, and without the key it answers ``[]``. Both lookups are strict, so
a scope that cannot be resolved admits no triggered skill. A turn that matched
nothing looks up no scope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import context
from kiro_crew.agent_discovery import SkillScopeResolutionError
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.context import ContextBuilder
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.memory import MemoryStore

_DEFAULT_AGENT = AGENT_FILENAME.removesuffix(".json")


def _turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str | None,
    pinned: list[str] | None,
    agent_globs: list[str] | None,
    matched: tuple[str, ...] = ("alpha", "beta"),
    pin_unreadable: bool = False,
) -> tuple[list[str], list[str | None]]:
    """Run one turn matching *matched* (``alpha`` and ``beta`` by default).

    Returns the skills handed on for delivery (``[]`` when none were) and the
    agents whose mapping was looked up. A glob here is a skill key, so a mapping
    admits the keys it names. *agent_globs* ``None`` is a mapping that cannot be
    resolved, and *pin_unreadable* a pin whose file cannot be read: each raises
    under ``strict`` and answers ``[]`` without it, as the real lookups do.
    """
    looked_up: list[str | None] = []

    def fake_agent_skill_globs(name, agents_dir=None, *, session_key="", strict=False, **_kwargs):
        looked_up.append(name)
        if agent_globs is None:
            if strict:
                raise SkillScopeResolutionError(f"Cannot read skill scope for agent {name!r}")
            return []
        # As for a Bot's worker: the mapping resolves only from the turn's session.
        return list(agent_globs) if session_key == "s1" else []

    def fake_pinned_skill_globs(_session_key, *, strict=False, **_kwargs):
        if pin_unreadable:
            if strict:
                raise SkillScopeResolutionError("Cannot read skill scope from the pin")
            return []
        return pinned

    monkeypatch.setattr(context, "pinned_skill_globs", fake_pinned_skill_globs)
    monkeypatch.setattr(context, "agent_skill_globs", fake_agent_skill_globs)

    loader = MagicMock()
    loader.get_triggered_skills.return_value = list(matched)
    loader.scoped_skills.side_effect = lambda *, project_dir=None, only=None: [
        {"key": key} for key in only or ()
    ]
    loader.split_triggered.side_effect = lambda triggered, _project: (list(triggered), [])
    loader.load_skill.return_value = "body"
    loader.strip_frontmatter.return_value = "body"
    loader.trigger_hint.return_value = ""
    builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)

    builder.build_message("trigger", is_new_session=False, session_key="s1", agent=agent)

    assert loader.split_triggered.call_count <= 1
    if not loader.split_triggered.called:
        return [], looked_up
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


def test_a_bots_worker_mapping_narrows_the_set(tmp_path, monkeypatch):
    """A Bot's Abilities mapping reaches its worker through the session's merged
    spec, so a skill outside it is not injected by a trigger."""
    delivered, looked_up = _turn(
        tmp_path, monkeypatch, agent=MATE_WORKER_AGENT, pinned=None, agent_globs=["beta"]
    )

    assert delivered == ["beta"]
    assert looked_up == [MATE_WORKER_AGENT]


def test_a_mapping_that_cannot_be_resolved_admits_no_triggered_skill(tmp_path, monkeypatch):
    """A Bot's worker whose kit cannot be read (its file removed, its record
    unreadable) gets no triggered skill, not the whole catalog."""
    delivered, looked_up = _turn(
        tmp_path, monkeypatch, agent=MATE_WORKER_AGENT, pinned=None, agent_globs=None
    )

    assert delivered == []
    assert looked_up == [MATE_WORKER_AGENT]


def test_an_unreadable_pin_admits_no_triggered_skill(tmp_path, monkeypatch):
    delivered, looked_up = _turn(
        tmp_path,
        monkeypatch,
        agent=_DEFAULT_AGENT,
        pinned=None,
        agent_globs=["beta"],
        pin_unreadable=True,
    )

    assert delivered == []
    assert looked_up == []


def test_a_turn_with_no_match_does_not_look_up_the_mapping(tmp_path, monkeypatch):
    """A worker's mapping is read from its kit on disk; a turn that matched no
    skill has nothing to narrow, so it pays for no lookup."""
    delivered, looked_up = _turn(
        tmp_path,
        monkeypatch,
        agent=MATE_WORKER_AGENT,
        pinned=None,
        agent_globs=["beta"],
        matched=(),
    )

    assert delivered == []
    assert looked_up == []
