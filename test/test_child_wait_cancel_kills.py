"""A cancel during a child-process wait kills and reaps the child.

Each site already killed its child on timeout; a cancelled awaiting task
(gateway shutdown, torn-down request) skipped that and left the child running
with no owner. Each test cancels the task once the child is being awaited and
checks the cancel propagates and the child goes to ``kill_and_reap``.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.testing.wait import await_until


class _HangingProc:
    """A child that runs until killed: ``wait``/``communicate`` never return."""

    def __init__(self):
        self.returncode = None
        self.awaited = asyncio.Event()
        self.reaped: list = []

    async def _hang(self, *args, **kwargs):
        self.awaited.set()
        # flake-ok: a child that never exits on its own; only the cancel ends this wait
        await asyncio.Event().wait()

    wait = _hang
    communicate = _hang

    def kill(self):
        pass


@pytest.fixture
def child(monkeypatch):
    proc = _HangingProc()

    async def fake_exec(*args, **kwargs):
        return proc

    async def fake_kill_and_reap(p, *args, **kwargs):
        proc.reaped.append(p)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(platform_compat, "kill_and_reap", fake_kill_and_reap)
    return proc


async def _cancel_while_awaited(coro, proc):
    task = asyncio.ensure_future(coro)
    await await_until(proc.awaited, what="the child to be awaited")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled task to finish")
    assert proc.reaped == [proc]


@pytest.mark.asyncio
async def test_login_status_cancel_kills_child(child, monkeypatch):
    from kiro_crew.remote_session import git_gateway

    monkeypatch.setattr(git_gateway, "_binary", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(git_gateway, "_credential_env", lambda name: {})
    await _cancel_while_awaited(git_gateway.login_status("github.com"), child)


@pytest.mark.asyncio
async def test_permission_probe_cancel_kills_child(child, monkeypatch):
    from kiro_crew.dashboard.handlers import computer_use

    platform_id = next(iter(computer_use._PROBEABLE_PLATFORMS))
    await _cancel_while_awaited(computer_use._probe_permissions(platform_id), child)


@pytest.mark.asyncio
async def test_upload_chooser_cancel_kills_child(child, monkeypatch):
    from kiro_crew.dashboard.file_api import desktop

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: "/usr/bin/" + name)
    await _cancel_while_awaited(desktop.api_upload(MagicMock()), child)


@pytest.mark.asyncio
async def test_screenshot_cancel_kills_child(child, monkeypatch, tmp_path):
    from kiro_crew.dashboard.file_api import desktop

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop, "_screenshot_dir", lambda: tmp_path)
    await _cancel_while_awaited(desktop.api_screenshot(MagicMock()), child)


@pytest.mark.asyncio
async def test_describe_voices_cancel_kills_child(child, monkeypatch):
    from kiro_crew.dashboard import chat_voice

    monkeypatch.setattr(chat_voice, "_vc", MagicMock(provider="polly", aws_profile="", region=""))
    monkeypatch.setattr(chat_voice, "_voices_cache", None)
    monkeypatch.setattr(chat_voice, "_voices_cache_ts", 0)
    monkeypatch.setattr(chat_voice.aws_consent, "refuse_and_log", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_voice, "resolve_polly_cli", lambda: "/usr/local/bin/aws")
    await _cancel_while_awaited(chat_voice.api_voice_voices(MagicMock()), child)
