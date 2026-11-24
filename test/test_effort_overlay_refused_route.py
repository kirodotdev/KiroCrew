"""An effort pick the workspace ``cli.json`` refuses on every retry is refused, not reset.

kiro-cli reads effort only from that file. When the settings folder links out of the
work directory, or the file does not parse, a reset would start the chat on the file's
level while the dashboard shows the pick. The slot effort route answers 400 instead,
commits no slot value and keeps the session. The sensitive-descriptor checks in both
lock walks are pinned here too.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link, requires_o_nofollow, requires_symlinks
from kiro_crew import pinned_fs
from kiro_crew.dashboard.chat import api_chat_slot_reasoning_effort
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider, _write_cli_overlay
from kiro_crew.providers.acp_cli_overlay import EffortOverlayRefused
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_NAME

_MODEL = "claude-opus-4.7"


def _kiro_provider(work_dir: Path) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend="")
    provider._client = MagicMock()
    provider._client.backend = ""
    provider._client._model = _MODEL
    provider._client._work_dir = work_dir
    provider._client.send_command = AsyncMock()
    provider._client.has_active_turn = MagicMock(return_value=False)
    return provider


def _state(slot: _ChatSlot, provider: AcpProvider) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


async def _post_effort(state: DashboardState, level: str) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": level}
        )
        return resp.status, await resp.json()


def _outside_settings_link(tmp_path: Path) -> tuple[Path, Path]:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "shared-kiro"
    outside.mkdir()
    make_dir_link(work_dir / ".kiro", outside)
    return work_dir, outside


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["high", ""], ids=["pick", "clear"])
async def test_an_outside_kiro_link_refuses_the_pick_without_committing_or_resetting(
    tmp_path, level
):
    work_dir, outside = _outside_settings_link(tmp_path)
    provider = _kiro_provider(work_dir)
    provider._effort_per_model[_MODEL] = "low"
    slot = _ChatSlot("test")
    slot.reasoning_effort = "low"
    state = _state(slot, provider)

    status, body = await _post_effort(state, level)

    assert status == 400
    assert body["code"] == "effort_overlay_refused"
    assert slot.reasoning_effort == "low"
    state.sessions.reset.assert_not_called()
    state.sessions.record_effort_override.assert_not_called()
    provider._client.send_command.assert_not_awaited()
    assert provider._effort_per_model == {_MODEL: "low"}
    assert list(outside.rglob("*")) == []


@pytest.mark.asyncio
async def test_an_unparseable_cli_json_refuses_the_pick_and_keeps_its_bytes(tmp_path):
    work_dir = tmp_path / "work"
    settings = work_dir / ".kiro" / "settings"
    settings.mkdir(parents=True)
    written = b'{"chat.modelDefaults": '
    (settings / "cli.json").write_bytes(written)
    provider = _kiro_provider(work_dir)
    slot = _ChatSlot("test")
    state = _state(slot, provider)

    status, body = await _post_effort(state, "high")

    assert status == 400
    assert body["code"] == "effort_overlay_refused"
    assert slot.reasoning_effort == ""
    state.sessions.reset.assert_not_called()
    provider._client.send_command.assert_not_awaited()
    assert (settings / "cli.json").read_bytes() == written


@requires_symlinks
@pytest.mark.asyncio
@pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned", "by-name"])
async def test_a_linked_cli_json_refuses_the_pick_on_either_walk(
    tmp_path, monkeypatch, pinned_walk
):
    if pinned_walk and not pinned_fs.supports_pinned_walk():
        pytest.skip("no descriptor-relative walk on this host")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinned_walk)
    work_dir = tmp_path / "work"
    settings = work_dir / ".kiro" / "settings"
    settings.mkdir(parents=True)
    linked = tmp_path / "dotfiles-cli.json"
    written = b'{"chat.modelDefaults": {}}'
    linked.write_bytes(written)
    (settings / "cli.json").symlink_to(linked)
    provider = _kiro_provider(work_dir)

    with pytest.raises(EffortOverlayRefused):
        await provider.change_effort("high")

    assert linked.read_bytes() == written
    assert _MODEL not in provider._effort_per_model
    provider._client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_busy_lock_still_raises_the_retryable_error(tmp_path):
    provider = _kiro_provider(tmp_path)
    with patch(
        "kiro_crew.providers.acp_cli_overlay._write_cli_overlay",
        side_effect=TimeoutError("lock busy"),
    ):
        with pytest.raises(RuntimeError, match="try again") as raised:
            await provider.change_effort("high")
    assert getattr(raised.value, "overlay_refused", False) is False
    assert _MODEL not in provider._effort_per_model


@requires_o_nofollow
@pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned", "by-name"])
def test_a_held_settings_folder_on_a_sensitive_path_is_refused_before_the_lock(
    tmp_path, monkeypatch, pinned_walk
):
    from kiro_crew import security

    work_dir = tmp_path / "work"
    settings = work_dir / ".kiro" / "settings"
    settings.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    security._home_targets_cache.clear()
    if pinned_walk and not pinned_fs.supports_pinned_walk():
        pytest.skip("no descriptor-relative walk on this host")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinned_walk)
    sensitive = os.fspath(tmp_path / ".ssh")
    real_settings = os.path.realpath(settings)
    real_fd_real_path = pinned_fs.fd_real_path

    def _held_folder_reads_as_sensitive(fd: int) -> str | None:
        opened = real_fd_real_path(fd)
        return sensitive if opened == real_settings else opened

    monkeypatch.setattr(pinned_fs, "fd_real_path", _held_folder_reads_as_sensitive)

    with pytest.raises(OSError, match="sensitive path"):
        _write_cli_overlay(work_dir, _MODEL, "high")

    assert not (settings / CLI_SETTINGS_LOCK_NAME).exists()
    assert not (settings / "cli.json").exists()


@pytest.mark.asyncio
async def test_a_live_model_switch_keeps_its_session_when_the_overlay_refuses_the_level(
    monkeypatch,
):
    from kiro_crew.dashboard.chat_api import slot_model
    from kiro_crew.providers.acp_cli_overlay import EffortOverlayRefused

    monkeypatch.setattr(
        AcpProvider,
        "reapply_live_effort",
        AsyncMock(side_effect=EffortOverlayRefused("settings folder links outside")),
    )
    slot = _ChatSlot("test")
    slot.reasoning_effort = "high"

    keep_live_session = await slot_model._reapply_effort_after_live_switch(
        "test", slot, MagicMock(spec=AcpProvider)
    )

    assert keep_live_session is True


@pytest.mark.asyncio
async def test_a_workflow_step_keeps_its_session_when_the_overlay_refuses_the_level():
    from kiro_crew.workflows.agent_exec import apply_step_effort

    provider = MagicMock(spec=AcpProvider)
    provider.accepts_effort_level = MagicMock(return_value=True)
    provider.change_effort = AsyncMock(
        side_effect=EffortOverlayRefused("settings folder links outside")
    )

    await apply_step_effort(provider, "high")

    provider.change_effort.assert_awaited_once_with("high")


def _no_live_session(slot: _ChatSlot, monkeypatch) -> DashboardState:
    from kiro_crew.dashboard import chat_handlers

    # The cold slot is configured for kiro-cli, the harness that reads the overlay.
    monkeypatch.setattr(chat_handlers, "_configured_backend_for_slot", AsyncMock(return_value=""))
    return _state(slot, None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_pick_with_no_live_session_is_refused_for_an_outside_kiro_link(
    tmp_path, monkeypatch
):
    work_dir, outside = _outside_settings_link(tmp_path)
    slot = _ChatSlot("test")
    slot.project = str(work_dir)
    slot.reasoning_effort = "low"
    state = _no_live_session(slot, monkeypatch)

    status, body = await _post_effort(state, "high")

    assert status == 400
    assert body["code"] == "effort_overlay_refused"
    assert slot.reasoning_effort == "low"
    state.sessions.reset.assert_not_called()
    assert list(outside.rglob("*")) == []


@pytest.mark.asyncio
async def test_a_pick_with_no_live_session_is_saved_when_the_overlay_can_take_it(
    tmp_path, monkeypatch
):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    slot = _ChatSlot("test")
    slot.project = str(work_dir)
    state = _no_live_session(slot, monkeypatch)

    status, _body = await _post_effort(state, "high")

    assert status != 400
    assert slot.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_a_pick_during_a_running_turn_is_refused_for_an_unparseable_cli_json(tmp_path):
    work_dir = tmp_path / "work"
    settings = work_dir / ".kiro" / "settings"
    settings.mkdir(parents=True)
    written = b'{"chat.modelDefaults": '
    (settings / "cli.json").write_bytes(written)
    provider = _kiro_provider(work_dir)
    provider._client.has_active_turn = MagicMock(return_value=True)
    slot = _ChatSlot("test")
    slot.reasoning_effort = "low"
    state = _state(slot, provider)

    status, body = await _post_effort(state, "high")

    assert status == 400
    assert body["code"] == "effort_overlay_refused"
    assert slot.reasoning_effort == "low"
    assert (settings / "cli.json").read_bytes() == written


def test_a_cold_start_work_dir_follows_the_cwd_then_the_resumed_cwd_then_the_session_dir(
    tmp_path,
):
    from kiro_crew.config.loader_locations import provider_work_dir
    from kiro_crew.session_allocation_parts.cold_start_work_dir import cold_start_work_dir

    recorded = tmp_path / "recorded"
    recorded.mkdir()
    resumed = MagicMock()
    resumed.get.return_value = "sid-1"
    resumed.get_cwd.return_value = str(recorded)
    fresh = MagicMock()
    fresh.get.return_value = None
    fresh.get_native_fork.return_value = None

    assert cold_start_work_dir(resumed, "k", str(tmp_path)) == tmp_path
    assert cold_start_work_dir(resumed, "k", None) == recorded
    assert cold_start_work_dir(fresh, "k", None) == provider_work_dir("k", None)
    resumed.get_cwd.return_value = str(tmp_path / "gone")
    assert cold_start_work_dir(resumed, "k", None) == provider_work_dir("k", None)


@pytest.mark.parametrize(
    "failure,refused",
    [
        (errno.ELOOP, True),
        (errno.EISDIR, True),
        (errno.EIO, False),
        (errno.EMFILE, False),
    ],
    ids=["link", "directory", "io-error", "descriptor-table-full"],
)
@pytest.mark.parametrize("reader", ["pinned", "by-name"])
def test_only_a_shape_error_opening_cli_json_is_a_refusal(
    tmp_path, monkeypatch, failure, refused, reader
):
    from kiro_crew import platform_compat, workspace_cli_settings

    settings = tmp_path / ".kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "cli.json").write_bytes(b"{}")

    def _fail(*_args, **_kwargs):
        raise OSError(failure, os.strerror(failure))

    if reader == "pinned":
        if not pinned_fs.supports_pinned_walk():
            pytest.skip("no descriptor-relative open on this host")
        settings_fd = os.open(settings, pinned_fs.dir_flags())
        try:
            with monkeypatch.context() as patched:
                patched.setattr(workspace_cli_settings.os, "open", _fail)
                with pytest.raises(OSError) as raised:
                    workspace_cli_settings.read_pinned_cli_json(settings_fd, settings / "cli.json")
        finally:
            os.close(settings_fd)
    else:
        monkeypatch.setattr(platform_compat, "open_file_no_reparse", _fail)
        with pytest.raises(OSError) as raised:
            workspace_cli_settings.read_named_cli_json(settings / "cli.json", settings)

    assert isinstance(raised.value, workspace_cli_settings.CliSettingsRefused) is refused
