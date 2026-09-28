"""The Chat_Runner resolves folder steering LIVE and hands it to ``build_message``.

The unit under test is one block in ``chat_runner._run_chat``: on a turn that
carries session-start context (a fresh provider session, or a reinjection after
compaction) it reads the COMMITTED folder tree, resolves the slot's accumulated
steering directories off-loop, and passes them to ``build_message`` as
``steering_dirs``. Nothing is cached on the slot, so a folder edit reaches the
chats already filed inside it (Req 5.1, 5.3); a slot with no folder passes ``()``
(Req 5.4); a resolver error degrades to ``()`` plus a warning naming the slot
(Req 7.3); and a warm turn never touches the tree at all.

Each test drives the REAL runner with a mocked ``build_message`` and reads the
keyword it received, rather than calling a helper -- the call site passing the
resolved value is the thing the requirement is about, and a helper test cannot
see a missing keyword argument.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew import pinned_fs
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

SLOT_NAME = "standards-chat"

#: The collector refuses to walk where a directory cannot be opened relative to a
#: descriptor (native Windows), so the turns that assert DELIVERED directories
#: carry this marker; the no-folder and error-degradation turns run everywhere.
_needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="folder steering refuses to walk by name on this platform",
)


def _turn_state(tmp_path, monkeypatch, *, is_new=True, resumed=False, needs_reinjection=False):
    """A DashboardState whose only mocked seam is the provider and the builder."""
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("task", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(provider, is_new, resumed))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    # ``sessions`` is a MagicMock, so an unset reinjection flag would read as a
    # truthy mock and silently turn every turn into a reinjection turn.
    state.sessions.consume_needs_reinjection = MagicMock(return_value=needs_reinjection)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state


def _slot(state, *, folder_id=""):
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.folder_id = folder_id
    return slot


async def _turn(state, slot, message="Follow the standards."):
    slot.append("user", message)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, message), 30)
    await asyncio.wait_for(drain_background_tasks(state), 10)


def _passed_dirs(state) -> Any:
    state.context_builder.build_message.assert_called()
    return state.context_builder.build_message.call_args.kwargs["steering_dirs"]


def _count_folder_reads(state) -> list[int]:
    """Count ``read_folders`` calls without replacing the real read path."""
    reads: list[int] = []
    real = state.read_folders

    async def _counting(read):
        reads.append(1)
        return await real(read)

    state.read_folders = _counting
    return reads


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_fresh_session_passes_resolved_dirs(tmp_path, monkeypatch):
    """Req 4.5, 5.1: a fresh session resolves the tree and passes the tuple."""
    org, repo = tmp_path / "org", tmp_path / "repo"
    org.mkdir()
    repo.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    state._folders = [
        {"id": "f-org", "name": "Org", "steering_dirs": [str(org)]},
        {"id": "f-repo", "name": "Repo", "parent_id": "f-org", "steering_dirs": [str(repo)]},
    ]
    await _turn(state, _slot(state, folder_id="f-repo"))
    # Root-first: the ancestor's standards precede the child's additions.
    assert _passed_dirs(state) == (str(org.resolve()), str(repo.resolve()))


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_reinjection_turn_passes_resolved_dirs(tmp_path, monkeypatch):
    """Req 6.1: a post-compaction turn re-resolves and re-passes the dirs."""
    standards = tmp_path / "standards"
    standards.mkdir()
    state = _turn_state(tmp_path, monkeypatch, is_new=False, resumed=True, needs_reinjection=True)
    state._folders = [{"id": "f-1", "name": "Std", "steering_dirs": [str(standards)]}]
    await _turn(state, _slot(state, folder_id="f-1"))
    assert _passed_dirs(state) == (str(standards.resolve()),)


@pytest.mark.asyncio
async def test_slot_without_folder_passes_empty(tmp_path, monkeypatch):
    """Req 5.4: an unfiled chat contributes no folder steering."""
    standards = tmp_path / "standards"
    standards.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    # A folder exists and carries steering; the slot is simply not in it.
    state._folders = [{"id": "f-1", "name": "Std", "steering_dirs": [str(standards)]}]
    reads = _count_folder_reads(state)
    await _turn(state, _slot(state))
    assert _passed_dirs(state) == ()
    assert reads == [], "an unfiled slot must not read the folder tree"


@pytest.mark.asyncio
async def test_stale_stored_entry_is_skipped_not_fatal(tmp_path, monkeypatch, caplog):
    """Req 7.3: a stale stored path is skipped at read time; the turn proceeds.

    folders.json is not trusted: a stored RELATIVE path fails re-validation.
    It is skipped with a warning naming the folder rather than failing the
    resolution, so nothing bad reaches the collector and nothing else is lost.
    """
    state = _turn_state(tmp_path, monkeypatch)
    state._folders = [{"id": "f-1", "name": "Std", "steering_dirs": ["relative/dir"]}]
    slot = _slot(state, folder_id="f-1")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_folders"):
        await _turn(state, slot)
    assert _passed_dirs(state) == ()
    skipped = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "skipped at read time" in record.message
    ]
    assert skipped, caplog.text
    assert "f-1" in skipped[0]


@pytest.mark.asyncio
async def test_resolver_error_degrades_and_warns(tmp_path, monkeypatch, caplog):
    """Req 7.3: a MALFORMED stored value yields ``()`` and a warning naming the slot."""
    state = _turn_state(tmp_path, monkeypatch)
    # A corrupt folders.json shape (not a list of strings) fails the whole
    # resolution; the runner degrades to no folder steering and says so.
    state._folders = [{"id": "f-1", "name": "Std", "steering_dirs": ["ok", 7]}]
    slot = _slot(state, folder_id="f-1")
    with caplog.at_level(logging.WARNING, logger=chat_runner.logger.name):
        await _turn(state, slot)
    assert _passed_dirs(state) == ()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "Folder steering unavailable" in record.message
    ]
    assert warnings, caplog.text
    assert slot.key in warnings[0]


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_folder_edit_is_picked_up_without_slot_state(tmp_path, monkeypatch):
    """Req 5.1, 5.2, 5.3: the next fresh session reads the edited tree."""
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    state._folders = [{"id": "f-1", "name": "Std", "steering_dirs": [str(before)]}]
    slot = _slot(state, folder_id="f-1")
    await _turn(state, slot)
    assert _passed_dirs(state) == (str(before.resolve()),)
    # The folder is edited; the slot is untouched.
    state._folders[0]["steering_dirs"] = [str(after)]
    state.context_builder.build_message.reset_mock()
    await _turn(state, slot, "Again.")
    assert _passed_dirs(state) == (str(after.resolve()),)
    assert not hasattr(slot, "steering_dirs"), "the slot must not cache steering dirs"


@pytest.mark.asyncio
async def test_warm_turn_does_not_read_folders(tmp_path, monkeypatch):
    """Req 3.10: a turn with no session-start context never touches the tree."""
    standards = tmp_path / "standards"
    standards.mkdir()
    state = _turn_state(tmp_path, monkeypatch, is_new=False, resumed=True)
    state._folders = [{"id": "f-1", "name": "Std", "steering_dirs": [str(standards)]}]
    reads = _count_folder_reads(state)
    await _turn(state, _slot(state, folder_id="f-1"))
    assert _passed_dirs(state) == ()
    assert reads == [], "a warm turn must not read the folder tree"


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_member_slot_receives_its_own_members_folder_steering(tmp_path, monkeypatch):
    """A crew member's folder steers the member's own chat through the real seam.

    ``folder_principal`` stamps a member-created folder ``owner_app="member:<store>"``
    while a member slot's ``_app`` is empty. The call site must therefore hand
    the resolver the slot's PRINCIPAL (``slot_steering_principal``), not its
    ``_app``; comparing against ``_app`` alone skipped every member-owned
    folder silently. Drives ``_run_chat`` with a real provisioned member so the
    execution context is the one the runner captures, not a fabricated stand-in.
    """
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, "reviewer")
    cfg.save()
    rules = tmp_path / "member-rules"
    rules.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    state._folders = [
        {
            "id": "m-root",
            "name": "Reviewer",
            "owner_app": f"member:{store}",
            "steering_dirs": [str(rules)],
        }
    ]
    slot = _slot(state, folder_id="m-root")
    slot.agent = "reviewer"
    await _turn(state, slot)
    assert slot.memory_store == store, "the turn must have bound the member's private store"
    assert _passed_dirs(state) == (str(rules.resolve()),)
