"""The start notice for a chat whose folder was an earlier run's scratch folder.

The chat keeps its stored folder, so every fresh start falls back again. The
notice is shown once per chat per gateway run, in memory, and the turn describes
the folder the session runs in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.session_allocation_parts.get_or_create import HIDDEN_SCRATCH_CWD_NOTICE

_KEY = "dashboard_hidden_scratch"


def _provider(token: str) -> SimpleNamespace:
    return SimpleNamespace(startup_notice=(token, "fallback text"))


def test_the_notice_shows_once_per_chat_and_never_hides_another_notice(monkeypatch):
    rows: list[tuple[object, str]] = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda state, slot, *a, **kw: rows.append((slot, kw))
    )
    first, other = _ChatSlot("t1"), _ChatSlot("t2")
    starts = [_provider(HIDDEN_SCRATCH_CWD_NOTICE) for _ in range(3)]
    for provider in starts[:2]:
        chat_runner._surface_startup_notice(MagicMock(), first, provider)
    chat_runner._surface_startup_notice(MagicMock(), other, starts[2])
    chat_runner._surface_startup_notice(MagicMock(), first, _provider("cloud_session_replaced"))

    assert [(slot.key, kw["meta"]["kind"]) for slot, kw in rows] == [
        ("t1", HIDDEN_SCRATCH_CWD_NOTICE),
        ("t2", HIDDEN_SCRATCH_CWD_NOTICE),
        ("t1", "cloud_session_replaced"),
    ]
    # Every provider's notice is consumed, shown or not, so none lingers.
    assert all(provider.startup_notice is None for provider in starts)


@pytest.mark.asyncio
async def test_the_turn_describes_the_folder_the_session_runs_in(tmp_path):
    root = tmp_path / "scratch"
    old = str(root / "runtime-old" / "checkout")
    default = str(tmp_path / "default")
    unmounted = str(tmp_path / "drive" / "project")
    with (
        patch("kiro_crew.agent_scratch.scratch_root", return_value=root),
        patch("kiro_crew.sandbox_tiers.configured_sandbox_mode", return_value="auto"),
        patch("kiro_crew.platform_compat.IS_WINDOWS", False),
        patch("kiro_crew.sandbox_tiers._clamp_sandbox_mode", side_effect=lambda mode: mode),
    ):
        assert await chat_runner._fallback_turn_project(old, default, _KEY, None) == default
        # A folder outside the scratch root, even one that is missing, is kept.
        assert await chat_runner._fallback_turn_project(unmounted, default, _KEY, None) == ""
        assert await chat_runner._fallback_turn_project(default, default, _KEY, None) == ""
        assert await chat_runner._fallback_turn_project("", default, _KEY, None) == ""


@pytest.mark.asyncio
async def test_native_windows_keeps_a_chat_scratch_folder(tmp_path):
    """Native Windows masks no scratch tree, so a chat's scratch folder that exists is kept."""
    from kiro_crew.session_allocation_parts.get_or_create import _stored_cwd_is_hidden_scratch

    root = tmp_path / "scratch"
    old = str(root / "runtime-old" / "checkout")
    (root / "runtime-old" / "checkout").mkdir(parents=True)
    with (
        patch("kiro_crew.agent_scratch.scratch_root", return_value=root),
        patch("kiro_crew.sandbox_tiers.configured_sandbox_mode", return_value="auto"),
        patch("kiro_crew.sandbox_tiers._clamp_sandbox_mode", side_effect=lambda mode: mode),
        patch("kiro_crew.platform_compat.IS_WINDOWS", True),
    ):
        assert _stored_cwd_is_hidden_scratch(old, None, _KEY, None) is False
        assert (
            await chat_runner._fallback_turn_project(old, str(tmp_path / "default"), _KEY, None)
            == ""
        )


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_reading_its_folder_releases_the_session(tmp_path):
    """The session is claimed but not yet marked acquired when the turn reads its folder.

    A Stop or a disconnect cancelling that read must still hand the lease back, or
    every later turn of the chat waits on a session nobody holds.
    """
    from unittest.mock import AsyncMock

    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    client = MagicMock()
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("cancel-slot")
    slot._titled = True

    reached: list[bool] = []

    async def _cancelled(*_args):
        reached.append(True)
        raise asyncio.CancelledError

    with (
        patch.object(chat_runner, "_fallback_turn_project", _cancelled),
        patch.object(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False)),
    ):
        try:
            await chat_runner._run_chat(state, slot, "hello")
        except asyncio.CancelledError:
            pass
    assert reached, "precondition: the turn reached the folder read"
    key = state.sessions.get_or_create.await_args.args[0]
    state.sessions.release.assert_any_call(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,moved", [("opencode", True), ("", False)])
async def test_a_respawn_leaves_a_refused_cwd_by_the_floor_of_the_backend_it_spawns(
    monkeypatch, tmp_path, backend: str, moved: bool
) -> None:
    """With ``agent.sandbox`` off, a floored spawn masks the refused tree, so its cwd moves."""
    from typing import Any, cast

    from kiro_crew import platform_compat
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.sandbox = "off"
    cfg.agent.harness_sandbox = {"opencode": "auto"}
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    root = tmp_path / "scratch"
    refused = root / "parent-abcdef01"
    inside = refused / "x"
    inside.mkdir(parents=True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path / "home")
    host = SimpleNamespace(_work_dir=inside, _spawn_work_dir=str(inside))
    request = SimpleNamespace(scratch_label=_KEY, backend=backend)

    async def hop(fn, *args):
        return fn(*args)

    with (
        patch("kiro_crew.agent_scratch.scratch_root", return_value=root),
        patch("kiro_crew.sandbox_tiers._clamp_sandbox_mode", side_effect=lambda mode: mode),
    ):
        await launch_mod._leave_a_refused_scratch_cwd(
            cast(Any, host), cast(Any, request), refused, hop, MagicMock()
        )
    assert (not host._work_dir.is_relative_to(refused)) is moved
