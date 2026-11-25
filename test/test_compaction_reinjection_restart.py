"""A restart between a compaction and the next turn keeps the re-injection owed.

The in-memory flag dies with the session record, so the session map keeps a copy
until a turn lands: the resume that follows a restart reads it, and its slim
prompt carries what a warm post-compaction turn would have sent.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew import context as context_mod
from kiro_crew import session_compaction_mark
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.session import SessionManager
from kiro_crew.session_map import COMPACTED_FLAG, SessionMap
from kiro_crew.skills import SkillsLoader

pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


async def _marked_manager(cfg) -> SessionManager:
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create("thread1")
    mgr.release("thread1")
    mgr.mark_needs_reinjection("thread1")
    return mgr


def _replace_record(mgr: SessionManager, key: str) -> None:
    """What a restart leaves on a resumed record: no in-memory mark, nothing taken."""
    mgr._sessions[key].needs_context_reinjection = False
    mgr._sessions[key].compaction_mark_taken = False


@pytest.mark.asyncio
async def test_a_reported_compaction_has_its_mark_on_disk(cfg):
    """Read from disk before the debounced flush or the shutdown could write it."""
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    try:
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr._fire_compact_callback("thread1", 80.0, success=True)
        assert SessionMap().get_flag("thread1", COMPACTED_FLAG) is True
    finally:
        await mgr.close_all()
    successor = SessionManager(cfg, provider_factory=_mock_provider_factory())
    try:
        assert successor._session_map.get_flag("thread1", COMPACTED_FLAG) is True
    finally:
        await successor.close_all()


@pytest.mark.asyncio
async def test_a_failed_flush_is_logged_and_does_not_fail_the_report(caplog):
    session_map = MagicMock()

    async def _broken() -> None:
        raise OSError("disk full")

    session_map.aflush = _broken
    with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
        await session_compaction_mark.aflush(session_map, "thread1")
    assert "post-compaction mark for thread1 not written" in caplog.text


@pytest.mark.asyncio
async def test_a_turn_that_never_lands_leaves_the_mark_owed(cfg):
    mgr = await _marked_manager(cfg)
    try:
        _replace_record(mgr, "thread1")
        assert mgr.consume_needs_reinjection("thread1") is True
        assert mgr.consume_needs_reinjection("thread1") is False, "once per record"
        # The process stops before the turn lands: the copy is still there.
        assert mgr._session_map.get_flag("thread1", COMPACTED_FLAG) is True
        _replace_record(mgr, "thread1")
        assert mgr.consume_needs_reinjection("thread1") is True
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_a_landed_turn_pays_the_mark(cfg):
    mgr = await _marked_manager(cfg)
    try:
        assert mgr.consume_needs_reinjection("thread1") is True
        mgr.note_turn_landed("thread1")
        assert mgr._session_map.get_flag("thread1", COMPACTED_FLAG) is False
        _replace_record(mgr, "thread1")
        assert mgr.consume_needs_reinjection("thread1") is False
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_a_mark_armed_during_the_turn_survives_its_landing(cfg):
    mgr = await _marked_manager(cfg)
    try:
        assert mgr.consume_needs_reinjection("thread1") is True
        mgr.mark_needs_reinjection("thread1")
        mgr.note_turn_landed("thread1")
        assert mgr._session_map.get_flag("thread1", COMPACTED_FLAG) is True
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed,kept", [(True, True), (False, False)])
async def test_only_a_fresh_claim_drops_the_mark(cfg, resumed, kept):
    """A fresh start sends the full session start; a resume still owes it."""
    mgr = await _marked_manager(cfg)
    try:
        _replace_record(mgr, "thread1")
        provider = MagicMock(session_head_held=False)

        mgr.note_session_head("thread1", provider, is_new=True, resumed=resumed)

        assert mgr.consume_needs_reinjection("thread1") is kept
    finally:
        await mgr.close_all()


def _builder(tmp_path) -> ContextBuilder:
    skills_dir = tmp_path / "skills" / "widget-maker"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: widget-maker\ndescription: Build a widget.\n---\n# WidgetMaker\nBody.",
        encoding="utf-8",
    )
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


@pytest.mark.asyncio
async def test_a_resume_after_a_compaction_re_sends_the_session_start(cfg, tmp_path, monkeypatch):
    """The resumed turn builds with what the consume path hands it."""
    mgr = await _marked_manager(cfg)
    try:
        _replace_record(mgr, "thread1")
        needs = mgr.consume_needs_reinjection("thread1")
    finally:
        await mgr.close_all()
    monkeypatch.setattr(cfg.dashboard, "verbosity", "concise")
    monkeypatch.setattr(context_mod.KiroCrewConfig, "load", lambda *a, **k: cfg)
    builder = _builder(tmp_path)
    builder.memory.write_projects("# Active Projects\n\n- Gizmo rollout\n")
    msg, _ = builder.build_message(
        "carry on",
        is_new_session=True,
        resumed=True,
        needs_reinjection=needs,
        session_key="dashboard:s1",
        agent="kirocrew",
    )
    assert "[SESSION RESUMED" in msg
    # The block itself, not the critical rules' mention of its marker.
    assert msg.count("[AGENT SYSTEM PROMPT]\n") == 1
    assert "[REINJECTED AFTER COMPACTION" in msg
    assert "## Available Skills" in msg
    assert "widget-maker" in msg
    assert "[Memory activity index" in msg
    assert "Gizmo rollout" in msg
    assert "[Memory tools] Call memory_recall" in msg
    # Sent once: the slim path's own delivery stands down for the re-injected one.
    assert msg.count("## Reply style: Concise") == 1
    assert "response preferences]\n[RESPONSE PREFERENCES" in msg


def test_a_plain_resume_stays_slim(tmp_path):
    msg, _ = _builder(tmp_path).build_message(
        "carry on",
        is_new_session=True,
        resumed=True,
        session_key="dashboard:s1",
        agent="kirocrew",
    )
    assert "[SESSION RESUMED" in msg
    assert "[AGENT SYSTEM PROMPT]\n" not in msg
    assert "[REINJECTED AFTER COMPACTION" not in msg
