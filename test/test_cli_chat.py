"""Regression tests for interrupting CLI chat."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_chat
from kiro_crew.acp.client import AcpTimeoutError
from kiro_crew.acp.types import STOP_REASON_STALE_RECOVER
from kiro_crew.config import KiroCrewConfig, loader
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "default_agent", "model", "expected"),
    [
        # A crew member starts its bound kiro agent under its own identity, with
        # its model pin -- what the dashboard does for the same member.
        ("reviewer", "", None, ("review-bot", "reviewer", "member-model")),
        # An explicit --model outranks the member's pin.
        ("reviewer", "", "picked-model", ("review-bot", "reviewer", "picked-model")),
        # A member bound to no kiro agent runs the built-in one, not its own name.
        ("unbound", "", None, ("kirocrew", "unbound", None)),
        # A kiro agent name that is not a member passes through unchanged.
        ("review-bot", "", None, ("review-bot", None, None)),
        # agent.default_agent is a template name, so it is never member-resolved.
        (None, "reviewer", None, ("reviewer", None, None)),
    ],
)
async def test_a_crew_member_name_starts_its_bound_kiro_agent(
    monkeypatch, agent, default_agent, model, expected
) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.default_agent = default_agent
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="review-bot", model="member-model")
    cfg.agents["unbound"] = KiroCrewAgentConfig(kiro_agent="")
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    started: list[tuple[str | None, str | None, str | None]] = []
    gate_agents: list[str] = []

    def _factory(config):
        def _provider(*args, agent=None, crew_agent=None, model_override=None, **kwargs):
            started.append((agent, crew_agent, model_override))
            return provider

        return _provider

    # Pin the materialized-agent snapshot so the resolver never scans a real
    # agents directory and no snapshot state leaks to other tests.
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"review-bot"}))
    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli_chat, "build_provider_factory", _factory)
    monkeypatch.setattr(
        cli_chat,
        "_build_tool_gate",
        lambda name: gate_agents.append(name) or MagicMock(),
    )
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", model, agent)

    assert started == [expected]
    # The governance gate keeps the name the user picked, as the dashboard's
    # tool hook does, so a member's task profile applies to its tool calls.
    assert gate_agents == [agent or default_agent]


@pytest.mark.asyncio
async def test_a_member_whose_store_is_unusable_is_refused_before_spawn(
    monkeypatch, capsys
) -> None:
    cfg = KiroCrewConfig()
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="review-bot", memory_store="gone")
    factory = MagicMock()
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"review-bot"}))
    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli_chat, "build_provider_factory", factory)

    with pytest.raises(SystemExit) as exited:
        await cli_chat._chat("hello", None, "reviewer")

    assert exited.value.code == 1
    factory.assert_not_called()
    assert "'gone' is unavailable" in capsys.readouterr().err


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


def _turn_provider(stop_reason: str | None) -> SimpleNamespace:
    """Streams one chunk, then ends the turn with ``stop_reason``; ``None`` raises
    the prompt timeout the per-process client raises instead."""

    async def stream(message, **_kw):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answ")
        if stop_reason is None:
            raise AcpTimeoutError("partial answ")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason)

    return SimpleNamespace(stream=stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["timeout", STOP_REASON_STALE_RECOVER, None])
async def test_send_and_print_reports_an_unfinished_turn_as_timed_out(stop_reason, capsys):
    """A turn the backend never finished exits 1 with the timeout notice, whether
    the provider ends it with a terminal event or raises the prompt timeout."""
    with pytest.raises(SystemExit) as excinfo:
        await cli_chat._send_and_print(_turn_provider(stop_reason), "question")
    out, err = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "Response timed out." in err
    assert "partial answ" in out
    if stop_reason is not None:
        # Streamed once, and not printed again by the timeout branch.
        assert out.count("partial answ") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
async def test_send_and_print_prints_a_finished_or_cancelled_turn(stop_reason, capsys):
    """A finished turn and a user-requested stop print their text and return."""
    await cli_chat._send_and_print(_turn_provider(stop_reason), "question")
    out, err = capsys.readouterr()
    assert "partial answ" in out
    assert "timed out" not in err
