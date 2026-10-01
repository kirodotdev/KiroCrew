"""Regression tests for interrupting CLI chat."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_chat
from kiro_crew.config import KiroCrewConfig


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
async def test_provider_notice_uses_stderr_and_preserves_stdout_answer(capsys):
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_NOTICE, EVENT_TEXT_CHUNK, AcpEvent

    class Provider:
        async def stream(self, message):
            yield AcpEvent(
                kind=EVENT_NOTICE, title="Warning", text="Detail", notice_severity="warning"
            )
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Answer")
            yield AcpEvent(kind=EVENT_COMPLETE)

    await cli_chat._send_and_print(Provider(), "question")
    captured = capsys.readouterr()
    assert captured.out == "Answer\n"
    assert "[warning] Warning\nDetail" in captured.err
