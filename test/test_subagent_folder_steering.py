"""A sub-agent receives the folder steering its parent session receives.

A chat filed in a folder gets that folder's steering directories on every turn
that rebuilds its context. Its children must get the same set: the run resolves
it from the parent's session key when it starts and hands it to
``build_message``. The run-level tests reuse the harness of
``test_subagent_context_group_applied.py`` (a scripted provider and a mocked
``ContextBuilder``), so they assert on the argument rather than on rendered text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import pinned_fs
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentManager

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

#: The steering-dirs validator refuses every directory where one cannot be
#: opened relative to a descriptor (native Windows), so a test that expects a
#: directory to RESOLVE carries this marker, as in
#: ``test_chat_folder_steering_dirs.py``.
_needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="steering_dirs are refused where a directory cannot be opened relative to a descriptor",
)

_PARENT = "dashboard:chat-parent"


def _stream(*_a: object, **_k: object):
    async def _gen():
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="done", runtime_global=False)
        yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

    return _gen()


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=_stream)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    return sessions


async def _run(resolver: object, **spawn_kwargs: object) -> tuple[object, dict]:
    """Run one sub-agent; return its record and the ``build_message`` kwargs."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=ctx)
    mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
    mgr._parent_steering_dirs = resolver  # type: ignore[assignment]
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do the thing", **spawn_kwargs)  # type: ignore[arg-type]
        assert info is not None
        await mgr._tasks[info.id]
    assert ctx.build_message.called, "build_message was never reached"
    return info, ctx.build_message.call_args.kwargs


class TestRunPassesParentSteering:
    @pytest.mark.asyncio
    async def test_parent_dirs_reach_build_message(self):
        resolver = AsyncMock(return_value=("/guides/org", "/guides/repo"))
        info, kwargs = await _run(resolver, parent_session_key=_PARENT)
        resolver.assert_awaited_once_with(_PARENT)
        assert kwargs["steering_dirs"] == ("/guides/org", "/guides/repo")
        # Kept on the record, where a grandchild's resolution reads it.
        assert info.steering_dirs == ("/guides/org", "/guides/repo")

    @pytest.mark.asyncio
    async def test_withheld_project_group_skips_the_read(self):
        resolver = AsyncMock(return_value=("/guides/org",))
        _, kwargs = await _run(resolver, parent_session_key=_PARENT, include_project=False)
        resolver.assert_not_awaited()
        assert kwargs["steering_dirs"] == ()

    @pytest.mark.asyncio
    async def test_resolver_failure_runs_without_steering(self):
        resolver = AsyncMock(side_effect=RuntimeError("folders unreadable"))
        _, kwargs = await _run(resolver, parent_session_key=_PARENT)
        assert kwargs["steering_dirs"] == ()

    @pytest.mark.asyncio
    async def test_unwired_manager_passes_none(self):
        _, kwargs = await _run(None, parent_session_key=_PARENT)
        assert kwargs["steering_dirs"] == ()


def _state(folders: list[dict], slots: dict, agents: dict | None = None) -> SimpleNamespace:
    async def read_folders(read):
        return read(folders)

    return SimpleNamespace(
        _slots=slots,
        read_folders=read_folders,
        subagents=SimpleNamespace(_agents=agents or {}),
    )


def _slot(key: str, folder_id: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, linked_session_key="", folder_id=folder_id, _app="")


@pytest.fixture
def _no_execution(monkeypatch):
    """The parent has no member execution: a person's chat, principal ``""``."""
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: None
    )


@pytest.mark.usefixtures("_no_execution")
class TestDashboardResolver:
    @_needs_pinned_walk
    @pytest.mark.asyncio
    async def test_chat_parent_gets_its_folder_chain(self, tmp_path):
        org, repo = tmp_path / "org", tmp_path / "repo"
        org.mkdir()
        repo.mkdir()
        folders = [
            {"id": "root", "steering_dirs": [str(org)]},
            {"id": "leaf", "parent_id": "root", "steering_dirs": [str(repo)]},
        ]
        state = _state(folders, {"chat-parent": _slot("chat-parent", "leaf")})
        dirs = await _shared.session_folder_steering_dirs(state, _PARENT)  # type: ignore[arg-type]
        assert dirs == (str(org.resolve()), str(repo.resolve()))

    @pytest.mark.asyncio
    async def test_unfiled_chat_gets_nothing(self, tmp_path):
        state = _state([], {"chat-parent": _slot("chat-parent", "")})
        assert await _shared.session_folder_steering_dirs(state, _PARENT) == ()  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_other_principals_folder_is_fenced(self, tmp_path):
        """An app-owned folder steers only that app's slots, never a person's child."""
        guides = tmp_path / "app"
        guides.mkdir()
        folders = [{"id": "f", "owner_app": "some-app", "steering_dirs": [str(guides)]}]
        state = _state(folders, {"chat-parent": _slot("chat-parent", "f")})
        assert await _shared.session_folder_steering_dirs(state, _PARENT) == ()  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_subagent_parent_passes_its_own_set_down(self):
        parent = SimpleNamespace(steering_dirs=("/guides/org",))
        state = _state([], {}, agents={"abc123": parent})
        dirs = await _shared.session_folder_steering_dirs(state, "subagent:abc123")  # type: ignore[arg-type]
        assert dirs == ("/guides/org",)

    @pytest.mark.asyncio
    async def test_continued_parent_is_found_by_its_conversation_key(self):
        """A continuation calls tools as ``subagent:<first id>``; its live set wins."""
        first = SimpleNamespace(id="first1", conversation_key="", steering_dirs=("/stale",))
        live = SimpleNamespace(
            id="second2", conversation_key="subagent:first1", steering_dirs=("/guides/now",)
        )
        state = _state([], {}, agents={"first1": first, "second2": live})
        state.subagents.running = [live]
        dirs = await _shared.session_folder_steering_dirs(state, "subagent:first1")  # type: ignore[arg-type]
        assert dirs == ("/guides/now",)


@_needs_pinned_walk
@pytest.mark.usefixtures("_no_execution")
@pytest.mark.asyncio
async def test_gateway_wires_the_resolver_into_the_manager(tmp_path, monkeypatch):
    """``DashboardState`` hands its manager the folder resolver, bound to itself."""
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.subagent_parts import agent_policy

    # The constructor also installs the process-wide parent-project resolver.
    monkeypatch.setattr(agent_policy, "_parent_project_resolver", None)
    guides = tmp_path / "guides"
    guides.mkdir()
    manager = SimpleNamespace(_parent_steering_dirs=None)
    state = DashboardState(MagicMock(), None, None, 0, subagents=manager)
    state._folders = [{"id": "f", "steering_dirs": [str(guides)]}]
    state._slots["chat-parent"] = _slot("chat-parent", "f")
    assert manager._parent_steering_dirs is not None
    assert await manager._parent_steering_dirs(_PARENT) == (str(guides.resolve()),)
