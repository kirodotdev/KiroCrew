"""A cron job on a mirror backend, driven end to end through ``_cron_callback``.

The four backends here pick their model over ``session/set_config_option``.
That push is non-strict at session start: a refused pin leaves the session on
the backend default and raises nothing. So the cron path's exception-based
downgrade handling never fires, and the run reads as if the pin ran.

Each case builds the REAL provider shape that backend runs on, lets the adapter
refuse the pin through the real startup push, and then runs the real cron
callback on it. Three client-path backends run on a raw ``AcpClient``. codex
runs on the shared runtime, so its pin goes through ``AcpSessionHandle``.

Every case checks three things:

* the run is delivered: the result comes back and is stored on the job;
* the approval policy the job asked for reaches the session and the turn;
* the downgrade is visible: the result carries the same "unavailable" line a
  caught model error gets, and the usage row does not bill the refused pin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.llm_helpers import ToolApprovalPolicy
from kiro_crew.providers.acp import AcpProvider

_REPLY = "Agent response here"


def _refuse_every_value(calls: list[str]):
    """An adapter that refuses every model value, the way codex does."""

    async def _set_config_option(config_id: str, value: str) -> None:
        calls.append(value)
        raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    return _set_config_option


async def _client_path_provider(backend: str, pin: str, work_dir: Path, set_option):
    """A raw ``AcpClient`` provider whose startup push the adapter refuses."""
    provider = AcpProvider(acp_backend=backend, work_dir=work_dir, model=pin)
    client = provider._client
    client._session_id = "sess-cron"
    client._model = pin
    client.set_config_option = set_option  # type: ignore[method-assign]
    await client._apply_startup_model()
    return provider


async def _shared_runtime_provider(backend: str, pin: str, work_dir: Path, set_option):
    """A shared-runtime provider whose startup ``set_model`` the adapter refuses."""
    provider = AcpProvider(acp_backend=backend, work_dir=work_dir, model=pin)
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    runtime.send_notification = AsyncMock()
    handle = AcpSessionHandle(
        session_id="sess-cron",
        queue=asyncio.Queue(),
        runtime=runtime,
        watchdog=WatchdogSettings(),
    )
    handle.set_config_option = set_option  # type: ignore[method-assign]
    # The same call the cold start makes to apply a configured model.
    await handle.set_model(pin)
    provider._client = AcpSessionProvider(handle, runtime, owns_runtime=True)  # type: ignore[assignment]
    return provider


_CASES = [
    pytest.param(
        sdk_backends.ACP_BACKEND_CLAUDE, "claude-opus-4-8", _client_path_provider, id="claude"
    ),
    pytest.param(
        sdk_backends.ACP_BACKEND_GOOSE, "claude-opus-4-8", _client_path_provider, id="goose"
    ),
    pytest.param(
        sdk_backends.ACP_BACKEND_OPENCODE,
        "anthropic/claude-opus-4-8",
        _client_path_provider,
        id="opencode",
    ),
    pytest.param(
        sdk_backends.ACP_BACKEND_CODEX,
        "gpt-5.4",
        _shared_runtime_provider,
        id="codex-shared-runtime",
    ),
]


def _make_gw():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw.dashboard_state.has_slot = MagicMock(return_value=False)
    gw.dashboard_state.notify = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw._cfg = MagicMock()
    gw._cfg.agent.provider = "acp"
    gw._cfg.hooks = {}
    gw._approval_mode = None
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.ctx_builder.build_message = MagicMock(return_value=("full prompt", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


async def _run_cron(gw, job, get_or_create):
    """Run *job* through the real ``_cron_callback`` captured from ``_init_cron``."""
    captured_cb = None
    gw.sessions.get_or_create = AsyncMock(side_effect=get_or_create)
    stream = AsyncMock(return_value=_REPLY)
    persist = AsyncMock()

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_in_embed_pool",
            AsyncMock(return_value=("full prompt", None)),
        ),
        patch("kiro_crew.slack.gateway.stream_and_collect", stream),
        patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
    ):
        mock_ctx.return_value = (f"cron:{job.id}", job.message)

        def capture_cron(on_job=None, **_kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured_cb is not None
        result = await captured_cb(job)
    return result, stream, persist


@pytest.mark.asyncio
@pytest.mark.parametrize("backend, pin, build", _CASES)
async def test_a_refused_pin_is_visible_and_not_billed(backend, pin, build, tmp_path):
    job = CronJob(
        id="mirror1",
        name="mirror-job",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
        model=pin,
        approval_mode="auto",
    )
    calls: list[str] = []

    async def _get_or_create(*_args, **kwargs):
        provider = await build(backend, kwargs["model"], tmp_path, _refuse_every_value(calls))
        return provider, True, False

    gw = _make_gw()
    result, stream, persist = await _run_cron(gw, job, _get_or_create)

    # The adapter really was asked for the pin, and really refused it.
    assert calls and calls[0] == pin

    # Delivered: the reply comes back and is stored as the run's result.
    assert _REPLY in result
    assert job.last_result == result

    # The approval policy the job asked for reaches the session and the turn.
    assert gw.sessions.get_or_create.await_args.kwargs["approval_policy"] == "auto"
    assert stream.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.AUTO_APPROVE

    # Visible: the same line a caught model-unavailable error produces.
    assert result.startswith(f"⚠️ Model '{pin}' unavailable; ran with default.")

    # Not billed to the pin: the usage row's explicit model is blank.
    persist.assert_awaited_once()
    assert persist.await_args.args[1] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("backend, pin, build", _CASES)
async def test_an_accepted_pin_is_billed_and_not_annotated(backend, pin, build, tmp_path):
    """The flag must only fire on a refusal, never on a pin that ran."""
    job = CronJob(
        id="mirror2",
        name="mirror-job",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
        model=pin,
    )

    async def _accept(_config_id: str, _value: str) -> None:
        return None

    async def _get_or_create(*_args, **kwargs):
        provider = await build(backend, kwargs["model"], tmp_path, _accept)
        return provider, True, False

    gw = _make_gw()
    result, stream, persist = await _run_cron(gw, job, _get_or_create)

    assert result == _REPLY
    assert stream.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.HOOK_BASED
    persist.assert_awaited_once()
    assert persist.await_args.args[1] == pin


def _accept_model_refuse_effort(calls: list[tuple[str, str]]):
    """codex taking the bare model but refusing the effort half of a pair pin."""

    async def _set_config_option(config_id: str, value: str) -> None:
        calls.append((config_id, value))
        if config_id == "model" and "[" not in value:
            return None
        raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    return _set_config_option


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_client_path_provider, id="codex-client-path"),
        pytest.param(_shared_runtime_provider, id="codex-shared-runtime"),
    ],
)
async def test_a_half_applied_pair_pin_bills_the_bare_model(build, tmp_path):
    """The model half ran, the effort half did not.

    The session is not on the default, so no "unavailable" line. It is not on
    the full pin either, so the usage row bills the bare model that ran.
    """
    pin, bare = "gpt-5.4[high]", "gpt-5.4"
    job = CronJob(
        id="mirror3",
        name="mirror-job",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
        model=pin,
    )
    calls: list[tuple[str, str]] = []

    async def _get_or_create(*_args, **kwargs):
        provider = await build(
            sdk_backends.ACP_BACKEND_CODEX,
            kwargs["model"],
            tmp_path,
            _accept_model_refuse_effort(calls),
        )
        return provider, True, False

    gw = _make_gw()
    result, _stream, persist = await _run_cron(gw, job, _get_or_create)

    # The bare model landed and the effort write was refused.
    assert ("model", bare) in calls
    assert any(config_id != "model" for config_id, _value in calls)

    assert result == _REPLY
    persist.assert_awaited_once()
    assert persist.await_args.args[1] == bare


def _plain_provider():
    """An LLMProvider subclass that overrides nothing about model pins."""
    from kiro_crew.providers.base import LLMProvider

    stubs = {name: (lambda *_a, **_k: None) for name in LLMProvider.__abstractmethods__}
    return type("PlainProvider", (LLMProvider,), stubs)()


def test_the_model_pin_contract_is_declared_on_the_base_provider():
    """Every provider answers the pin questions, with a no-refusal default.

    The cron path reads these off any provider. A provider that only inherits
    the base must read as "pin ran", never raise AttributeError.
    """
    from kiro_crew.llm_helpers import provider_model_pin_partial, provider_model_pin_refused

    provider = _plain_provider()
    assert provider.model_pin_refused == ""
    assert provider.model_pin_partial == ""
    assert provider_model_pin_refused(provider) is False
    assert provider_model_pin_partial(provider) == ""
