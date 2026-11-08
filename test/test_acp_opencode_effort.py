"""opencode takes reasoning effort through its per-model ``effort`` config option.

``test/fixtures/acp_frames/opencode/effort-live.jsonl`` is one live ``opencode acp``
1.19.1 session against a local OpenAI-compatible stub: a reasoning model offered
low, medium, high and default, two writes that read back, a refused level the model does not
offer, and a switch to a model with no variants and back. Every test here reads the
capture rather than a hand-written option, so the membership rests on the harness's
own frames.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import _is_config_value_rejection
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE

CAPTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "opencode" / "effort-live.jsonl"
THINK = "stub/think-1"
PLAIN = "stub/plain-1"


def _frames() -> list[dict]:
    """Every frame of the capture, header excluded."""
    lines = CAPTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:]]


def _effort(options: list[dict]) -> tuple[str, list[str]] | None:
    for option in options:
        if option.get("id") == "effort":
            return option["currentValue"], [v["value"] for v in option["options"]]
    return None


def _options(frame: dict) -> list[dict]:
    return frame["result"]["configOptions"]


def _provider(tmp_path, options: list[dict]):
    """An ``AcpProvider`` on opencode with one captured ``configOptions`` array stored."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(acp_backend=ACP_BACKEND_OPENCODE, work_dir=tmp_path, model=THINK)
    provider._client._acp_config_options = copy.deepcopy(options)
    return provider


def test_opencode_is_a_member_of_both_effort_sets_under_the_default_id() -> None:
    assert ACP_BACKEND_OPENCODE in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert ACP_BACKEND_OPENCODE in sdk_backends.ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
    assert sdk_backends.effort_config_option_id(ACP_BACKEND_OPENCODE) == "effort"
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert sdk_backends.effort_config_option_value(ACP_BACKEND_OPENCODE, level) == level


def test_the_capture_shows_the_option_following_the_model() -> None:
    """The premise: variants on the reasoning model, no select at all on the plain one."""
    frames = _frames()
    levels = ["low", "medium", "high", "default"]
    assert _effort(_options(frames[1])) == ("low", levels)
    assert _effort(_options(frames[2])) == ("high", levels)
    assert _effort(_options(frames[3])) == ("low", levels)
    assert frames[4]["error"]["code"] == -32602
    assert _effort(_options(frames[5])) is None
    # Back on the reasoning model the variant is reset to its first level.
    assert _effort(_options(frames[6])) == ("low", levels)


def test_a_refused_level_is_read_as_a_value_refusal() -> None:
    error = _frames()[4]["error"]
    exc = AcpError(error["message"], code=error["code"])
    assert _is_config_value_rejection(exc, "effort", ACP_BACKEND_OPENCODE) is True


def test_a_reasoning_model_offers_the_control(tmp_path) -> None:
    provider = _provider(tmp_path, _options(_frames()[1]))

    assert provider.supports_effort() is True
    # The captured select offers ``default`` too; it means "no override", not a level.
    assert provider.get_valid_effort_levels() == ["low", "medium", "high"]
    assert provider.accepts_effort_level("high") is True
    assert provider.accepts_effort_level("default") is False
    # Not offered by this model, so hidden rather than folded onto a neighbour.
    assert provider.accepts_effort_level("max") is False
    assert provider.accepts_effort_level("xhigh") is False


def test_a_pre_1_19_select_without_default_keeps_its_levels(tmp_path) -> None:
    """opencode 1.18.30 offered low, medium and high only; that list is unchanged."""
    options = _options(_frames()[1])
    for option in options:
        if option["id"] == "effort":
            option["options"] = [o for o in option["options"] if o["value"] != "default"]
    provider = _provider(tmp_path, options)

    assert provider.get_valid_effort_levels() == ["low", "medium", "high"]


def test_the_shared_runtime_handle_drops_default_too() -> None:
    """``AcpSessionHandle`` reads the same captured select and answers the same levels."""
    from types import SimpleNamespace

    from kiro_crew.acp.session_handle_model_selection import get_valid_effort_levels

    handle = SimpleNamespace(
        _runtime=SimpleNamespace(acp_backend=ACP_BACKEND_OPENCODE),
        _config_options=_options(_frames()[1]),
    )

    assert get_valid_effort_levels(handle) == ["low", "medium", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "written"),
    [
        pytest.param("default", [], id="a_default_pick_is_never_written"),
        pytest.param("high", [("effort", "high")], id="the_level_is_written_under_effort"),
        pytest.param("max", [], id="a_level_the_model_does_not_offer_is_not_written"),
    ],
)
async def test_the_effort_write(tmp_path, level, written) -> None:
    provider = _provider(tmp_path, _options(_frames()[1]))
    provider._client.set_config_option = AsyncMock()

    await provider._set_effort_config_option(level)

    assert [c.args for c in provider._client.set_config_option.await_args_list] == written


def test_a_model_without_variants_offers_no_control(tmp_path) -> None:
    provider = _provider(tmp_path, _options(_frames()[5]))

    assert provider.supports_effort() is False


@pytest.mark.asyncio
async def test_a_live_set_model_follows_the_option_and_re_applies_the_level(tmp_path) -> None:
    """``AcpProvider.set_model`` reads opencode's answer to each ``model`` write.

    The switch goes through ``AcpClient.set_config_option``, so the answer comes
    from the result's own ``configOptions``. Leaving for the plain model withdraws
    the control and writes nothing; coming back, opencode has reset the variant to
    the model's first level, so the slot's level is written again after the switch.
    """
    frames = _frames()
    provider = _provider(tmp_path, _options(frames[1]))
    client = provider._client
    client._session_id = "opencode-session-1"
    provider._effort_per_model[THINK] = "high"
    assert provider.supports_effort() is True

    # The capture's results, in the order the writes below are answered.
    answers = [frames[5], frames[6], frames[2]]
    sent: list[dict] = []
    wire: list[JsonRpcMessage] = []

    async def _send_request(method, params, **_kwargs):
        assert method == "session/set_config_option"
        sent.append(params)
        answer = dict(answers.pop(0), id=len(sent))
        wire.append(JsonRpcMessage.from_dict(answer))
        return len(sent)

    async def _read_message(timeout=None):
        return wire.pop(0) if wire else None

    client._send_request = _send_request
    client._read_message = _read_message

    await provider.set_model(PLAIN)

    assert [(p["configId"], p["value"]) for p in sent] == [("model", PLAIN)]
    assert provider.supports_effort() is False

    await provider.set_model(THINK)

    assert [(p["configId"], p["value"]) for p in sent] == [
        ("model", PLAIN),
        ("model", THINK),
        ("effort", "high"),
    ]
    assert provider.supports_effort() is True
    assert _effort(client._acp_config_options) == ("high", ["low", "medium", "high", "default"])
