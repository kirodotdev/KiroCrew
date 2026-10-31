"""Strands takes its model and effort at launch, so a switch relaunches and reloads.

The Strands CLI's Python-project ACP mode reads ``--model`` and ``--effort`` when it
starts and offers no ACP request that changes either. ``acp.launch_tuning`` moves an
idle session by replacing the process with one launched on the new values; the
replacement ``session/load``s the same session id, so the harness replays the chat.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import launch_tuning
from kiro_crew.acp.harness import strands as strands_mod
from kiro_crew.acp.transport_errors import AcpError, AcpModelUnavailable
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_EFFORT_AT_LAUNCH,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.acp_model_effort import launch_effort_level

STRANDS = ACP_BACKEND_STRANDS


class _Stats:
    def __init__(self) -> None:
        self.windows: list[int] = []

    def rebase_to_window(self, window: int) -> None:
        self.windows.append(window)


def _client(*, resumes: bool = True, fails: BaseException | None = None) -> SimpleNamespace:
    """A client whose ``ensure_ready`` records the launch it would start."""
    client = SimpleNamespace(
        backend=STRANDS,
        _session_id="sess-1",
        _model="openai/gpt-a",
        _launch_effort="",
        _resume_session_id=None,
        _stdin_stalled=False,
        _resumed=True,
        _resolved_model_id=None,
        _last_substitution_model=None,
        _model_registry_namespace="strands",
        _launch_tuning_lock=None,
        model_pin_refused="stale",
        last_prompt_stats=_Stats(),
        _turn_done=asyncio.Event(),
        launches=[],
    )
    client._turn_done.set()

    async def ensure_ready() -> None:
        client.launches.append(
            (client._model, client._launch_effort, client._resume_session_id, client._stdin_stalled)
        )
        if fails is not None:
            raise fails
        # What the stalled-child replacement leaves: a fresh process, the resume id
        # consumed, and the restore verdict.
        client._stdin_stalled = False
        client._resume_session_id = None
        client._resumed = resumes
        client._session_id = "sess-1" if resumes else "sess-2"
        client._resolved_model_id = client._model

    client.ensure_ready = ensure_ready
    return client


def test_strands_takes_model_and_effort_at_launch() -> None:
    assert STRANDS in ACP_BACKENDS_MODEL_AT_LAUNCH
    assert STRANDS in ACP_BACKENDS_EFFORT_AT_LAUNCH


def test_a_model_switch_relaunches_and_reloads_the_same_session() -> None:
    client = _client()
    asyncio.run(launch_tuning.switch_launch_model(client, "anthropic/claude-b"))
    # One relaunch, on the new model, marked for replacement and told to load sess-1.
    assert client.launches == [("anthropic/claude-b", "", "sess-1", True)]
    assert client._model == "anthropic/claude-b"
    assert client._session_id == "sess-1"
    assert client.model_pin_refused == ""
    assert client.last_prompt_stats.windows == [0]


def test_model_and_effort_ride_one_relaunch() -> None:
    client = _client()
    asyncio.run(launch_tuning.switch_launch_model(client, "anthropic/claude-b", effort="high"))
    assert client.launches == [("anthropic/claude-b", "high", "sess-1", True)]
    assert client._launch_effort == "high"


def test_an_unchanged_value_starts_no_process() -> None:
    client = _client()
    asyncio.run(launch_tuning.switch_launch_tuning(client, model="openai/gpt-a", effort=""))
    client._model = ""
    asyncio.run(launch_tuning.switch_launch_tuning(client, model="auto"))
    assert client.launches == []


def test_a_replacement_that_opened_another_session_puts_the_old_values_back() -> None:
    client = _client(resumes=False)
    with pytest.raises(AcpError, match="could not reopen this chat"):
        asyncio.run(launch_tuning.switch_launch_tuning(client, model="anthropic/claude-b"))
    assert client._model == "openai/gpt-a"
    # The next start reopens the original chat on the original model.
    assert client._resume_session_id == "sess-1"
    assert client._stdin_stalled is True


def test_no_switch_runs_while_an_earlier_relaunch_still_owes_the_chat() -> None:
    """A fallback walk's next candidate must not reload the fresh session in its place."""
    client = _client(resumes=False)
    with pytest.raises(AcpError):
        asyncio.run(launch_tuning.switch_launch_model(client, "anthropic/claude-b"))
    assert client._relaunch_owed_session == "sess-1"
    assert client._session_id == "sess-2"
    launches = len(client.launches)
    with pytest.raises(AcpError, match="could not reopen this chat"):
        asyncio.run(launch_tuning.switch_launch_model(client, "openai/gpt-c"))
    assert len(client.launches) == launches
    assert client._relaunch_owed_session == "sess-1"


def test_a_replacement_that_failed_to_start_puts_the_old_values_back() -> None:
    client = _client(fails=AcpError("boom"))
    with pytest.raises(AcpError, match="boom"):
        asyncio.run(launch_tuning.switch_launch_tuning(client, effort="max"))
    assert (client._model, client._launch_effort) == ("openai/gpt-a", "")
    assert client._resume_session_id == "sess-1"
    assert client._stdin_stalled is True


def test_a_switch_during_a_turn_is_refused_without_touching_the_process() -> None:
    client = _client()
    client._turn_done.clear()
    with pytest.raises(AcpError, match="between turns"):
        asyncio.run(launch_tuning.switch_launch_tuning(client, model="anthropic/claude-b"))
    assert client.launches == []
    assert client._model == "openai/gpt-a"


def test_a_switch_before_the_session_exists_is_refused() -> None:
    client = _client()
    client._session_id = None
    with pytest.raises(AcpError, match="before the session"):
        asyncio.run(launch_tuning.switch_launch_tuning(client, model="anthropic/claude-b"))


def test_a_kiro_id_is_refused_rather_than_relaunched_on() -> None:
    """A fallback chain entry or a pick made on another harness never reaches the CLI."""
    client = _client()
    with pytest.raises(AcpModelUnavailable):
        asyncio.run(launch_tuning.switch_launch_model(client, "claude-sonnet-4.5"))
    assert client.launches == []


def test_set_model_on_the_client_relaunches() -> None:
    from kiro_crew.acp import client_model_selection as selection

    client = _client()
    asyncio.run(selection.set_model(client, "anthropic/claude-b"))
    assert [launch[0] for launch in client.launches] == ["anthropic/claude-b"]


@pytest.mark.parametrize(
    ("level", "args"),
    [("high", ["--effort", "high"]), ("", []), ("turbo", [])],
)
def test_the_launch_passes_only_a_level_the_cli_knows(level: str, args: list[str]) -> None:
    assert strands_mod.strands_effort_args(SimpleNamespace(_launch_effort=level)) == args


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", "gpt-a", "openai/gpt-a"),
        ("bedrock", "auto", ""),
        ("not-a-provider", "x", ""),
    ],
)
def test_the_picker_default_is_the_strands_settings_model(
    provider: str, model: str, expected: str
) -> None:
    agent = SimpleNamespace(strands_provider=provider, strands_model=model)
    assert launch_tuning.settings_default_model(agent, STRANDS) == expected
    assert launch_tuning.settings_default_model(agent, "") == ""


# ── the provider ──


def _provider(*, override: str = "") -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=STRANDS)
    client = MagicMock()
    client.backend = STRANDS
    client._model = "openai/gpt-a"
    provider._client = client
    provider._effort_per_model = {"openai/gpt-a": override} if override else {}
    provider._unbound_effort = ""
    return provider


@pytest.fixture
def relaunch():
    with patch(
        "kiro_crew.providers.acp_model_effort.switch_launch_tuning", new=AsyncMock()
    ) as switch:
        yield switch


def test_strands_offers_an_effort_control() -> None:
    assert _provider().supports_effort() is True


def test_an_effort_change_relaunches_on_the_level(relaunch) -> None:
    provider = _provider()
    assert asyncio.run(provider.change_effort("xhigh")) is True
    relaunch.assert_awaited_once_with(provider._client, effort="xhigh")
    assert provider._effort_per_model == {"openai/gpt-a": "xhigh"}


def test_a_failed_effort_relaunch_keeps_the_old_level(relaunch) -> None:
    provider = _provider(override="low")
    relaunch.side_effect = AcpError("no")
    with pytest.raises(AcpError):
        asyncio.run(provider.change_effort("max"))
    assert provider._effort_per_model == {"openai/gpt-a": "low"}


def test_clearing_effort_relaunches_on_the_default(relaunch) -> None:
    provider = _provider(override="high")
    assert asyncio.run(provider.clear_effort()) is True
    relaunch.assert_awaited_once_with(provider._client, effort="")
    assert provider._effort_per_model == {}


def test_the_session_starts_on_the_slot_level() -> None:
    provider = _provider(override="medium")
    assert launch_effort_level(provider) == "medium"
    provider._effort_per_model = {}
    provider._unbound_effort = "low"
    assert launch_effort_level(provider) == "low"


def test_a_provider_model_switch_carries_the_level_in_one_relaunch(relaunch) -> None:
    provider = _provider(override="high")
    with patch(
        "kiro_crew.providers.acp_model_effort.switch_launch_model", new=AsyncMock()
    ) as switch:
        asyncio.run(provider.set_model("anthropic/claude-b"))
    switch.assert_awaited_once_with(provider._client, "anthropic/claude-b", effort="high")
    relaunch.assert_not_awaited()


def test_a_strands_provider_id_another_harness_also_lists_is_strands_own() -> None:
    """pi and opencode advertise ``provider/model`` ids of the same shape."""
    with patch(
        "kiro_crew.model_scope.foreign_namespaces", return_value=frozenset({"pi"})
    ) as foreign:
        assert strands_mod.strands_model_is_foreign("anthropic/claude-b") is False
        assert strands_mod.strands_model_args("anthropic/claude-b") == [
            "--model",
            "anthropic/claude-b",
        ]
        client = _client()
        asyncio.run(launch_tuning.switch_launch_model(client, "anthropic/claude-b"))
        assert [launch[0] for launch in client.launches] == ["anthropic/claude-b"]
        # An id with no Strands provider is still judged against the other catalogs.
        assert strands_mod.strands_model_is_foreign("pi-only-id") is True
    foreign.assert_called_once_with("pi-only-id", "strands")


def test_a_send_during_a_switch_waits_for_the_switch_to_land() -> None:
    """The prompt boundary takes the switch's lock around its own ensure_ready."""
    from kiro_crew.providers import acp_turns

    async def scenario() -> list[str]:
        order: list[str] = []
        client = _client()
        client._launch_tuning_lock = asyncio.Lock()
        released = asyncio.Event()

        async def ensure_ready() -> None:
            order.append("start")
            await released.wait()
            client._stdin_stalled = False
            client._resumed = True
            order.append("end")

        client.ensure_ready = ensure_ready
        client.confirm_member_projection = AsyncMock()
        provider = SimpleNamespace(_client=client)
        with (
            patch.object(acp_turns, "AcpClient", SimpleNamespace),
            patch.object(acp_turns.relaunch_reload, "apply_pending_mcp_relaunch", new=AsyncMock()),
        ):
            switch = asyncio.ensure_future(
                launch_tuning.switch_launch_tuning(client, model="anthropic/claude-b")
            )
            send = asyncio.ensure_future(acp_turns._ready_confirmed_session(provider))
            for _ in range(5):
                await asyncio.sleep(0)
            assert order == ["start"]
            released.set()
            await asyncio.wait_for(asyncio.gather(switch, send), timeout=5)
        return order

    assert asyncio.run(scenario()) == ["start", "end", "start", "end"]


def test_a_saved_level_survives_a_cold_start_on_an_opaque_strands_id() -> None:
    """The factory carries the level: the registry knows none of the ids Strands runs."""
    from kiro_crew.config.loader import KiroCrewConfig

    model = "bedrock/arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/x"
    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.agent.acp_backend = STRANDS
    with patch("kiro_crew.providers.acp.AcpProvider") as made:
        made.return_value = MagicMock()
        cfg.create_provider_factory()(
            session_key="dashboard:1", model_override=model, reasoning_effort_override="high"
        )
    assert made.call_args.kwargs.get("effort_per_model") == {model: "high"}


def _cold_start(model: str, level: str) -> dict:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.agent.acp_backend = STRANDS
    with patch("kiro_crew.providers.acp.AcpProvider") as made:
        made.return_value = MagicMock()
        cfg.create_provider_factory()(
            session_key="dashboard:1", model_override=model, reasoning_effort_override=level
        )
    return made.call_args.kwargs


@pytest.mark.parametrize("level", ["off", "minimal"])
def test_a_cli_only_level_survives_a_pinned_cold_start(level: str) -> None:
    """A level the live change accepts is the level the next cold start launches with."""
    assert _cold_start("openai/gpt-a", level).get("effort_per_model") == {"openai/gpt-a": level}


@pytest.mark.parametrize("level", ["off", "minimal"])
def test_a_cli_only_level_survives_an_unpinned_cold_start(level: str) -> None:
    assert _cold_start("", level).get("unbound_effort") == level


def test_a_level_the_cli_does_not_take_is_dropped_on_a_cold_start() -> None:
    kwargs = _cold_start("openai/gpt-a", "turbo")
    assert not kwargs.get("effort_per_model")
    assert not kwargs.get("unbound_effort")


@pytest.mark.parametrize("level", ["off", "minimal"])
def test_a_cli_only_level_is_taken_by_a_live_change(relaunch, level: str) -> None:
    provider = _provider()
    assert asyncio.run(provider.change_effort(level)) is True
    relaunch.assert_awaited_once_with(provider._client, effort=level)


def test_clearing_effort_relaunches_on_the_inherited_level(relaunch) -> None:
    """A clear runs the level a cold start would resolve, not the CLI's own default."""
    provider = _provider(override="low")
    provider._launch_default_effort = "high"
    assert asyncio.run(provider.clear_effort()) is True
    relaunch.assert_awaited_once_with(provider._client, effort="high")
    assert provider._effort_per_model == {}
    assert launch_effort_level(provider) == "high"


def test_a_cleared_level_does_not_come_back_with_the_model_left(relaunch) -> None:
    """A -> B carries A's level; clearing on B must not leave A's to return on A."""
    provider = _provider(override="low")
    provider._launch_default_effort = "high"
    with patch("kiro_crew.providers.acp_model_effort.switch_launch_model", new=AsyncMock()):
        asyncio.run(provider.set_model("anthropic/claude-b"))
    provider._client._model = "anthropic/claude-b"
    assert asyncio.run(provider.clear_effort()) is True
    assert provider._effort_per_model == {}
    with patch("kiro_crew.providers.acp_model_effort.switch_launch_model", new=AsyncMock()) as back:
        asyncio.run(provider.set_model("openai/gpt-a"))
    back.assert_awaited_once_with(provider._client, "openai/gpt-a", effort=None)


def test_the_factory_hands_the_inherited_level_to_the_provider() -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.agent.acp_backend = STRANDS
    with (
        patch("kiro_crew.providers.acp.AcpProvider") as made,
        patch.object(KiroCrewConfig, "resolve_session_effort", return_value="high"),
    ):
        made.return_value = MagicMock()
        cfg.create_provider_factory()(
            session_key="dashboard:1",
            model_override="openai/gpt-a",
            reasoning_effort_override="low",
        )
    kwargs = made.call_args.kwargs
    assert kwargs.get("effort_per_model") == {"openai/gpt-a": "low"}
    assert kwargs.get("launch_default_effort") == "high"


def test_every_launch_effort_harness_has_a_cli_vocabulary() -> None:
    """A new member with no vocabulary would refuse every level and drop it silently."""
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_EFFORT_AT_LAUNCH,
        EFFORT_LEVELS_AT_LAUNCH_BY_BACKEND,
    )

    assert set(EFFORT_LEVELS_AT_LAUNCH_BY_BACKEND) == set(ACP_BACKENDS_EFFORT_AT_LAUNCH)


def test_auto_is_a_strands_cli_level_and_is_carried() -> None:
    assert _cold_start("openai/gpt-a", "auto").get("effort_per_model") == {"openai/gpt-a": "auto"}


def test_a_live_change_refuses_a_level_the_cli_does_not_take(relaunch) -> None:
    with pytest.raises(ValueError):
        asyncio.run(_provider().change_effort("turbo"))
    relaunch.assert_not_awaited()


def test_a_send_never_starts_its_process_inside_a_switch() -> None:
    """The send's own ensure_ready waits on the switch's barrier."""
    from kiro_crew.acp import client_turn

    class _Stop(Exception):
        pass

    async def scenario() -> list[str]:
        order: list[str] = []
        client = _client()
        client._launch_tuning_lock = asyncio.Lock()
        client._cancelled = False
        released = asyncio.Event()

        async def ensure_ready() -> None:
            if not order:
                order.append("switch-start")
                await released.wait()
                client._stdin_stalled = False
                client._resumed = True
                order.append("switch-end")
                return
            order.append("send-start")
            raise _Stop

        async def no_timeout(timeout):
            await asyncio.sleep(0)
            return timeout

        client.ensure_ready = ensure_ready
        with patch.object(client_turn, "_effective_prompt_timeout_async", no_timeout):
            switch = asyncio.ensure_future(
                launch_tuning.switch_launch_tuning(client, model="anthropic/claude-b")
            )
            await asyncio.sleep(0)
            send = asyncio.ensure_future(client_turn.send_message(client, "hi"))
            for _ in range(5):
                await asyncio.sleep(0)
            assert order == ["switch-start"]
            released.set()
            await asyncio.wait_for(switch, timeout=5)
            with pytest.raises(_Stop):
                await asyncio.wait_for(send, timeout=5)
        return order

    assert asyncio.run(scenario()) == ["switch-start", "switch-end", "send-start"]


def test_only_a_launch_time_harness_takes_the_barrier() -> None:
    lock = asyncio.Lock()
    strands = SimpleNamespace(backend=STRANDS, _launch_tuning_lock=lock)
    kiro = SimpleNamespace(backend="", _launch_tuning_lock=lock)
    assert launch_tuning.switch_barrier(strands) is lock
    assert launch_tuning.switch_barrier(kiro) is not lock
