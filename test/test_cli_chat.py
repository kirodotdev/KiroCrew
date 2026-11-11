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
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.shell_audit_log import SHELL_AUDIT_LOG_MAX_BYTES, SHELL_AUDIT_LOG_NAME

#: ``_chat`` builds a ``SkillsLoader``, whose index and catalog worker outlive the test.
pytestmark = pytest.mark.usefixtures("close_skills_loaders")


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
async def test_a_stored_kiro_pick_runs_on_kas_in_cli_chat(monkeypatch, tmp_path) -> None:
    """End-to-end: ``kirocrew chat`` runs a stored Kiro CLI pick on KAS.

    The CLI does not run the gateway's KAS floor, so ``ensure_kiro_cli_kas_verdict``
    is what establishes the v3 evidence a stored ``"kiro"`` pick needs to resolve to
    KAS. Drive ``_chat`` with the probe answering v3-present and assert the backend
    the factory is handed is KAS.
    """
    import json

    from kiro_crew.acp.types import ACP_BACKEND_KAS
    from kiro_crew.acp_backends import STORED_BACKEND_KIRO
    from kiro_crew.agent_sdk import backends

    # A fresh process: no floor verdict, nothing probed yet.
    monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
    monkeypatch.setattr(backends, "_host_unservable", {})
    monkeypatch.setattr(backends, "_kiro_cli_offers_kas", False)
    monkeypatch.setattr(backends, "_kiro_cli_kas_probed", False)
    # The host's kiro-cli offers v3 (positive probe), and the stored pick is Kiro CLI.
    monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", lambda: (True, None))
    # ... and its v3 engine starts here without kiro-cli refusing it.
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.kas_engine_start_verdict", lambda binary=None: (True, None)
    )

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"agent": {"acp_backend": STORED_BACKEND_KIRO}}), "utf-8")
    # The real loader reads config_path(); point it at the file just written so
    # ``_chat``'s own ``KiroCrewConfig.load()`` runs the remap against our pick.
    monkeypatch.setattr(loader, "config_path", lambda: cfg_file)

    seen = {}
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()

    def _factory(config):
        seen["backend"] = config.agent.acp_backend
        return lambda *a, **k: provider

    monkeypatch.setattr(cli_chat, "build_provider_factory", _factory)
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", None)

    assert seen["backend"] == ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_a_refusal_on_record_moves_cli_chat_off_kas(monkeypatch, tmp_path) -> None:
    """End-to-end: ``kirocrew chat`` on a default KAS reads a refusal an earlier run kept.

    A first ``kirocrew chat -m`` met kiro-cli's "V3 is currently not supported on
    this system." at its KAS start, and its runtime kept that verdict in the data
    home. The next one must not spawn the refused relay again: the factory it builds
    gets Kiro CLI, and nothing is started to learn that.
    """
    from kiro_crew import kiro_cli
    from kiro_crew.acp import client as client_mod
    from kiro_crew.acp import kas_engine_refusal
    from kiro_crew.acp.types import ACP_BACKEND_KIRO
    from kiro_crew.agent_sdk import backends

    # A fresh process on the default backend (no config document), KAS selectable.
    monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
    monkeypatch.setattr(backends, "_host_unservable", {})
    monkeypatch.setattr(backends, "_kiro_cli_offers_kas", False)
    monkeypatch.setattr(backends, "_kiro_cli_kas_probed", False)
    binary = tmp_path / "kiro-cli"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    # The binary a session spawn runs, and the pinned install it is.
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: str(binary))
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("the record is read, never re-learned by a spawn")

    monkeypatch.setattr(kiro_cli, "kas_relay_start_output", _no_spawn)
    monkeypatch.setattr(kiro_cli, "kas_relay_help", _no_spawn)
    # What the first process's KAS runtime kept when kiro-cli refused its v3 engine.
    kas_engine_refusal.remember_start_verdict(str(binary), False)

    seen = {}
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()

    def _factory(config):
        seen["backend"] = config.agent.acp_backend
        return lambda *a, **k: provider

    monkeypatch.setattr(cli_chat, "build_provider_factory", _factory)
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", None)

    assert seen["backend"] == ACP_BACKEND_KIRO


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
        lambda agent, kiro_agent="": gate_agents.append(agent) or MagicMock(),
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
    gate_agents: list[tuple[str, str]] = []

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
        lambda name, kiro_agent="": gate_agents.append((name, kiro_agent)) or MagicMock(),
    )
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())

    await cli_chat._chat("hello", model, agent)

    assert started == [expected]
    # The governance gate keeps the name the user picked, as the dashboard's
    # tool hook does, so a member's task profile applies to its tool calls; the
    # spec hooks it asks are those of the kiro agent the session started.
    assert gate_agents == [(agent or default_agent, expected[0])]


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


def test_run_chat_prints_a_sandbox_unavailable_refusal_as_one_message(monkeypatch, capsys) -> None:
    """A host with no sandbox backend (stock Ubuntu's AppArmor userns restriction, for
    one) refuses the agent's spawn: chat prints the refusal's own remedy as one line
    and exits 1, not a traceback."""
    refusal = (
        "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
        "Run `kirocrew service install` to install the profile."
    )

    def unavailable(coro) -> None:
        coro.close()
        raise SandboxUnavailableError(refusal, kind="no_backend", detail="errno 1 (EPERM)")

    monkeypatch.setattr(cli_chat.asyncio, "run", unavailable)

    with pytest.raises(SystemExit) as excinfo:
        cli_chat._run_chat("hello", None)

    assert excinfo.value.code == 1
    assert f"\n❌ {refusal}\n" in capsys.readouterr().err


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
    monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent, kiro_agent="": MagicMock())
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
    monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent, kiro_agent="": MagicMock())
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


@pytest.mark.asyncio
async def test_send_and_print_names_kas_login_first_when_kiro_is_signed_out(capsys):
    """A signed-out KAS turn names ``kirocrew kas login`` before the dashboard card:
    a host reached by ssh may have no browser to open the dashboard in."""
    from kiro_crew.acp.client import AcpError
    from kiro_crew.agent_sdk import host_auth

    async def stream(message, **_kw):
        raise AcpError(host_auth.signed_out_message("kas"))
        yield  # pragma: no cover - makes this an async generator

    with pytest.raises(SystemExit) as excinfo:
        await cli_chat._send_and_print(SimpleNamespace(stream=stream), "question")
    err = capsys.readouterr().err
    assert excinfo.value.code == 1
    assert (
        "Not signed in to Kiro. Sign in with `kirocrew kas login` (or in the dashboard: "
        "Settings → Agent Harness → Kiro Agent → Kiro sign-in), then send your message again."
        in err
    )


def test_run_chat_names_kas_login_first_when_the_start_is_signed_out(monkeypatch, capsys):
    """A sign-in failure before any turn, while the backend starts, prints the same
    one line as a failed turn, not a traceback."""
    from kiro_crew.acp.client import AcpAuthRequired
    from kiro_crew.agent_sdk import host_auth

    def signed_out(coro) -> None:
        coro.close()
        raise AcpAuthRequired(host_auth.signed_out_message("kas"), backend="kas")

    monkeypatch.setattr(cli_chat.asyncio, "run", signed_out)

    with pytest.raises(SystemExit) as excinfo:
        cli_chat._run_chat("hello", None)
    err = capsys.readouterr().err
    assert excinfo.value.code == 1
    assert "Sign in with `kirocrew kas login` (or in the dashboard:" in err
    assert "Traceback" not in err
