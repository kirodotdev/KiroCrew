"""Copilot CLI takes a model switch and a reasoning effort through config options.

``test/fixtures/acp_frames/copilot/model-effort.jsonl`` is the Copilot CLI 1.0.107
capture of a model and an effort switch, on an account whose plan offers model choice:
``session/new`` advertises a ``model`` select (category ``model``) and a
``reasoning_effort`` select (category ``thought_level``, low through max), an effort
write on the starting model is refused with ``-32603``, the model write answers with
the switched select, and an effort write after the switch reads back. It is marked
synthesized because the capture was edited after recording (each model entry's
``_meta`` removed), but every frame Crew reads here is as the wire carried it.

The 1.0.100 captures beside it were taken on an account offering only ``auto``,
whose ``session/new`` advertises neither select; the last tests pin that such a
session still writes nothing.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import AcpClient, _is_config_value_rejection
from kiro_crew.acp.command_refused import AcpCommandRefused
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import ACP_BACKEND_COPILOT

COPILOT = ACP_BACKEND_COPILOT
_CORPUS = Path(__file__).parent / "fixtures" / "acp_frames" / "copilot"
CAPTURE = _CORPUS / "model-effort.jsonl"
START = "claude-sonnet-5.5"
SWITCHED = "claude-sonnet-5"
LEVELS = ["low", "medium", "high", "xhigh", "max"]


def _frames(name: str = CAPTURE.name) -> list[dict]:
    lines = (_CORPUS / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _answer(request_id: int, name: str = CAPTURE.name) -> dict:
    return next(f for f in _frames(name) if f.get("id") == request_id)


def _options(request_id: int, name: str = CAPTURE.name) -> list[dict]:
    return copy.deepcopy(_answer(request_id, name)["result"]["configOptions"])


def _select(options: list[dict], option_id: str) -> dict | None:
    return next((o for o in options if o.get("id") == option_id), None)


def _effort_refusal() -> AcpError:
    error = _answer(4)["error"]
    return AcpError(error["message"], code=error["code"])


def _provider(tmp_path, options: list[dict], model: str = START):
    """An ``AcpProvider`` on Copilot holding one captured ``configOptions`` array."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(acp_backend=COPILOT, work_dir=tmp_path, model=model)
    provider._client._acp_config_options = options
    return provider


# ── membership ───────────────────────────────────────────────────────────────


def test_copilot_takes_model_and_effort_through_config_options() -> None:
    assert COPILOT in sdk_backends.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert COPILOT in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert sdk_backends.effort_config_option_id(COPILOT) == "reasoning_effort"
    for level in LEVELS:
        assert sdk_backends.effort_config_option_value(COPILOT, level) == level


def test_the_capture_is_the_release_and_account_the_membership_rests_on() -> None:
    lines = CAPTURE.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])["_meta"]
    assert meta["backend"] == COPILOT
    assert meta["agent_version"] == "1.0.107"
    assert _answer(1)["result"]["agentInfo"]["version"] == "1.0.107"
    # The account offers more than ``auto``: 25 values before the select was cut.
    assert '"count": 25' in meta["note"]


# ── what session/new advertises ──────────────────────────────────────────────


def test_session_new_advertises_a_model_and_an_effort_select() -> None:
    result = _answer(2)["result"]
    model = _select(result["configOptions"], "model")
    effort = _select(result["configOptions"], "reasoning_effort")
    assert model is not None and model["category"] == "model"
    assert model["currentValue"] == START == result["models"]["currentModelId"]
    assert [o["value"] for o in model["options"]] == [START, SWITCHED]
    assert effort is not None and effort["category"] == "thought_level"
    assert effort["currentValue"] == "medium"
    assert [o["value"] for o in effort["options"]] == LEVELS


def test_the_client_reads_the_advertised_models(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._capture_available_models(_answer(2)["result"])
    assert client._advertised_model_ids() == [START, SWITCHED]


def test_the_provider_reads_the_advertised_effort_levels(tmp_path) -> None:
    provider = _provider(tmp_path, _options(2))
    assert provider._client.supports_config_option("model") is True
    assert provider._client.supports_config_option("reasoning_effort") is True
    assert provider.supports_effort() is True
    assert provider.get_valid_effort_levels() == LEVELS
    assert provider.accepts_effort_level("max") is True


# ── a model switch ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_model_switch_travels_as_the_model_config_option(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._session_id = "copilot-session-1"
    client._acp_config_options = _options(2)
    writes: list[tuple[str, str]] = []

    async def _write(config_id: str, value: str) -> None:
        writes.append((config_id, value))
        client._acp_config_options = _options(5)

    client.set_config_option = _write  # type: ignore[method-assign]
    client._send_request = AsyncMock(side_effect=AssertionError("no session/set_model"))

    await client.set_model(SWITCHED)

    assert writes == [("model", SWITCHED)]
    assert client._model == SWITCHED
    assert _select(client._acp_config_options, "model")["currentValue"] == SWITCHED


# ── an effort change ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_effort_change_after_the_switch_is_written_and_read_back(tmp_path) -> None:
    provider = _provider(tmp_path, _options(5), model=SWITCHED)
    provider._client._model = SWITCHED
    writes: list[tuple[str, str]] = []

    async def _write(config_id: str, value: str) -> None:
        writes.append((config_id, value))
        provider._client._acp_config_options = _options(6)

    provider._client.set_config_option = _write

    assert await provider.change_effort("high") is True
    assert writes == [("reasoning_effort", "high")]
    assert provider._effort_per_model[SWITCHED] == "high"
    effort = _select(provider._client._acp_config_options, "reasoning_effort")
    assert effort["currentValue"] == "high"


def test_the_starting_models_refusal_is_not_a_value_refusal() -> None:
    """-32603 with its own text: no ladder walk, it is the model saying it has no setting."""
    exc = _effort_refusal()
    assert exc.code == -32603
    assert _is_config_value_rejection(exc, "reasoning_effort", COPILOT) is False
    assert sdk_backends.effort_refused_by_model(COPILOT, exc) is True
    # The phrase is Copilot's: another harness's -32603 stays a failure.
    assert sdk_backends.effort_refused_by_model(sdk_backends.ACP_BACKEND_CLAUDE, exc) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("before", [None, "low"])
async def test_an_effort_refused_by_the_model_raises_a_command_refusal_and_rolls_back(
    tmp_path, before
) -> None:
    provider = _provider(tmp_path, _options(2))
    if before:
        provider._effort_per_model[START] = before
    provider._client.set_config_option = AsyncMock(side_effect=_effort_refusal())

    with pytest.raises(AcpCommandRefused) as raised:
        await provider.change_effort("high")
    assert raised.value.command_refused is True
    assert raised.value.code == -32603

    # One write, no step-down: the refusal names the model, not the level.
    assert [c.args for c in provider._client.set_config_option.await_args_list] == [
        ("reasoning_effort", "high")
    ]
    assert provider._effort_per_model.get(START) == before
    # What a respawn applies is the stored level, never the refused one.
    assert provider._resolve_effort() != "high"


@pytest.mark.asyncio
async def test_another_effort_failure_still_raises(tmp_path) -> None:
    provider = _provider(tmp_path, _options(2))
    provider._client.set_config_option = AsyncMock(
        side_effect=AcpError("Internal error", code=-32603)
    )
    with pytest.raises(AcpError):
        await provider.change_effort("high")
    assert START not in provider._effort_per_model


# ── an account with only auto (the 1.0.100 captures) ────────────────────────


@pytest.mark.parametrize(
    ("name", "request_id"),
    [("handshake-live.jsonl", 2), ("session-load-live.jsonl", 2)],
)
def test_the_auto_only_sessions_advertise_neither_select(name, request_id) -> None:
    options = _options(request_id, name)
    assert _select(options, "model") is None
    assert _select(options, "reasoning_effort") is None


@pytest.mark.asyncio
async def test_an_auto_only_session_writes_no_effort(tmp_path) -> None:
    provider = _provider(tmp_path, _options(2, "handshake-live.jsonl"), model=START)
    provider._client.set_config_option = AsyncMock()

    assert await provider.change_effort("high") is False
    await provider._set_effort_config_option("high")

    provider._client.set_config_option.assert_not_awaited()
    assert START not in provider._effort_per_model


@pytest.mark.asyncio
async def test_an_auto_only_session_keeps_its_default_on_a_startup_model_pin(tmp_path) -> None:
    """The model option is unknown there; an inherited pin leaves the default standing."""
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._session_id = "copilot-session-1"
    client._acp_config_options = _options(2, "handshake-live.jsonl")
    unknown = _answer(9, "handshake-live.jsonl")["error"]
    client.set_config_option = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpError(unknown["message"], code=unknown["code"])
    )

    assert await client._push_model_config_option(SWITCHED, strict=False) == ""
