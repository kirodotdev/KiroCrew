"""Regression tests for interrupting CLI chat."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_chat
from kiro_crew.config import KiroCrewConfig
from kiro_crew.shell_audit_log import SHELL_AUDIT_LOG_MAX_BYTES, SHELL_AUDIT_LOG_NAME


def _patch_provider(monkeypatch) -> MagicMock:
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    cfg = KiroCrewConfig()
    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        cli_chat,
        "build_provider_factory",
        lambda config: lambda *args, **kwargs: provider,
    )
    return provider


@pytest.mark.asyncio
async def test_cancelled_turn_shuts_down_provider(monkeypatch) -> None:
    provider = _patch_provider(monkeypatch)
    monkeypatch.setattr(
        cli_chat,
        "_send_and_print",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await cli_chat._chat("hello", None)

    provider.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_chat_uses_canonical_agent_for_provider_and_gate(monkeypatch) -> None:
    """The default ACP agent's task profile must receive the same identity."""
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    cfg = KiroCrewConfig()
    assert cfg.agent.default_agent == "", "exercise the provider-default path"

    provider_agents: list[str | None] = []
    gate_agents: list[str] = []

    def _factory(config):
        assert config is cfg

        def _provider(*args, agent=None, **kwargs):
            provider_agents.append(agent)
            return provider

        return _provider

    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli_chat, "build_provider_factory", _factory)
    monkeypatch.setattr(
        cli_chat,
        "_build_tool_gate",
        lambda agent: gate_agents.append(agent) or MagicMock(),
    )
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", None)

    assert provider_agents == ["kirocrew"]
    assert gate_agents == ["kirocrew"]


def test_run_chat_renders_keyboard_interrupt_as_clean_exit(monkeypatch, capsys) -> None:
    def interrupt(coro) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_chat.asyncio, "run", interrupt)

    cli_chat._run_chat(None, None)

    assert capsys.readouterr().out == "\nBye! 👻\n"


@pytest.mark.asyncio
async def test_a_chat_start_rotates_an_over_cap_audit_log_before_the_backend_starts(
    monkeypatch, tmp_path
) -> None:
    """The bundled hook appends from inside kiro-cli, whichever process launched it.

    Only the gateway's cleanup loop bounded ``audit.log``, so an install that only
    ever runs ``kirocrew chat`` never rotated the file. The chat start now runs
    the same sweep, and it runs BEFORE the backend that will append is spawned:
    the live file is bounded on entry to the session, not after it.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    live = tmp_path / SHELL_AUDIT_LOG_NAME
    live.write_bytes(b"x" * SHELL_AUDIT_LOG_MAX_BYTES)
    provider = _patch_provider(monkeypatch)
    live_size_when_backend_started: list[int] = []

    async def _start() -> None:
        live_size_when_backend_started.append(live.stat().st_size if live.exists() else 0)

    provider.start = AsyncMock(side_effect=_start)
    monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent: MagicMock())
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", None)

    rotated = tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")
    assert rotated.stat().st_size == SHELL_AUDIT_LOG_MAX_BYTES, "over-cap file not moved aside"
    assert live_size_when_backend_started == [
        0
    ], "the file must be bounded before the backend that appends to it is spawned"
    provider.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_chat_start_leaves_a_fresh_data_home_untouched(monkeypatch, tmp_path) -> None:
    """No audit log yet: the sweep costs one ``stat`` and creates nothing, not even the lock file."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _patch_provider(monkeypatch)
    monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent: MagicMock())
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", None)

    assert not any(p.name.startswith(SHELL_AUDIT_LOG_NAME) for p in tmp_path.iterdir())
