"""Two capabilities both ACP drivers carry, and the gates that scope them.

``AcpClient`` (one child process per session) and ``AcpSessionHandle`` (many
sessions on one shared process) each implement a config-option model push with a
candidate-spelling ladder, and a ``SESSION_CONFIG`` permission-routing write. A
harness that needs either can therefore run on either driver.

On the shared driver both are GATED, so neither is reachable for the two harnesses
that use it today:

* the ladder is reached only for ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``. KAS
  keeps its own single-write arm, so it still sends exactly ONE
  ``set_config_option`` carrying the resolved id, and a refusal there still
  raises rather than silently staying on the backend default;
* the permission write is reached only for ``Routing.SESSION_CONFIG``. kiro-cli
  and KAS both route ``Routing.AGENT_SPEC``, so the body never runs for them.

Every test below states what breaks in production if its invariant fails. The
transport is a mock: nothing here starts a subprocess.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import acp_tool_gate
from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    AcpToolGateUnroutable,
)
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpRuntimeProtocol, AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
    JsonRpcMessage,
)

_JSONRPC_INVALID_PARAMS = -32602

#: A stored id carrying BOTH a known inference-profile prefix and the ``[1m]``
#: window qualifier, so the ladder has all three rungs to walk.
_THREE_RUNG = "global.anthropic.claude-opus-4-8[1m]"
#: Prefixed but unqualified: two rungs.
_TWO_RUNG = "us.anthropic.claude-sonnet-4-5"


def _make_handle(backend: str = ACP_BACKEND_KIRO) -> AcpSessionHandle:
    """A handle on a mock runtime speaking *backend*, with no config on disk."""
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    runtime.send_notification = AsyncMock()
    return AcpSessionHandle(
        session_id="sess-cap",
        queue=asyncio.Queue(),
        runtime=runtime,
        watchdog=WatchdogSettings(),
    )


def _mock(handle: AcpSessionHandle) -> MagicMock:
    """The mock behind the handle's runtime, typed for the assertion helpers.

    The handle declares its runtime as the protocol, which is the point of the
    protocol -- so a test asserting on call records has to say out loud that it
    is reaching for the double.
    """
    return cast(MagicMock, handle._runtime)


class _Writes:
    """Records every ``set_config_option`` and answers each with a script.

    The script is a list of one entry per call: ``None`` accepts, an exception
    instance is raised. Running out of entries accepts, so a test that expects
    one write does not have to script the writes it is asserting never happen.
    """

    def __init__(self, *script: BaseException | None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._script = list(script)

    async def __call__(self, config_id: str, value: str) -> None:
        self.calls.append((config_id, value))
        answer = self._script.pop(0) if self._script else None
        if answer is not None:
            raise answer

    @property
    def values(self) -> list[str]:
        return [value for _cid, value in self.calls]


def _value_refusal_named() -> AcpError:
    """A claude-agent-acp refusal that names the option while rejecting the value."""
    return AcpError(f"Invalid value for config option {MODEL_CONFIG_ID}: nope")


def _value_refusal_bare() -> AcpError:
    """A codex refusal: a bare ``-32602`` with no prose to classify from."""
    return AcpError("JSON-RPC error: Invalid params", code=_JSONRPC_INVALID_PARAMS)


# ── The candidate ladder ──────────────────────────────────────────────────────


def test_candidate_ladder_tries_the_verbatim_id_first():
    """Order is the contract: verbatim, then prefix-stripped, then ``[1m]``-stripped.

    If the bare spelling were tried first, a session whose adapter DOES serve the
    window-qualified id would silently land on the base one, and the context
    meter would then convert every percentage against a window twice the size of
    the one actually in force.
    """
    cands = AcpClient._model_config_candidates(_THREE_RUNG)
    assert cands == [_THREE_RUNG, "claude-opus-4-8[1m]", "claude-opus-4-8"]


def test_candidate_ladder_offers_a_plain_id_exactly_once():
    """An id with nothing to peel yields ONE candidate.

    A duplicated spelling would send the same rejected value twice, doubling the
    round trips on every failed switch on a shared process that other sessions
    are waiting to be read from.
    """
    assert AcpClient._model_config_candidates("gpt-5") == ["gpt-5"]


# ── The three error classifications ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_config_option_abandons_the_ladder():
    """ "unknown config option" means the option is ABSENT, so no spelling helps.

    Walking the ladder here would send two more doomed writes per switch against
    an adapter build that has no model option at all.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    writes = _Writes(AcpError("unknown config option 'model'"))
    handle.set_config_option = writes  # type: ignore[method-assign]

    assert await handle._push_model_config_option(_THREE_RUNG, strict=False) == ""
    assert len(writes.calls) == 1


@pytest.mark.asyncio
async def test_unknown_config_option_re_raises_under_strict():
    """An EXPLICIT user pick must fail loudly when the option does not exist.

    Returning ``""`` there reports the switch as applied while the session keeps
    serving turns on the previous model.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle.set_config_option = _Writes(AcpError("unknown config option 'model'"))  # type: ignore[method-assign]

    with pytest.raises(AcpError, match="unknown config option"):
        await handle._push_model_config_option(_THREE_RUNG, strict=True)


@pytest.mark.asyncio
async def test_a_named_value_refusal_walks_to_the_next_spelling():
    """A refusal naming the option rejects the VALUE, so the next spelling is tried.

    Treating it as terminal would strand every session whose stored id carries a
    qualifier the serving adapter spells differently.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    writes = _Writes(_value_refusal_named())
    handle.set_config_option = writes  # type: ignore[method-assign]

    applied = await handle._push_model_config_option(_TWO_RUNG, strict=False)
    assert applied == "claude-sonnet-4-5"
    assert writes.values == [_TWO_RUNG, "claude-sonnet-4-5"]


@pytest.mark.asyncio
async def test_a_bare_invalid_params_is_read_as_a_value_refusal():
    """``-32602`` with no prose IS the refusal on a fixed-shape request.

    Read as a protocol failure it re-raises, session init fails, and a model pin
    carried over from another harness kills every session of this one at startup.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    writes = _Writes(_value_refusal_bare())
    handle.set_config_option = writes  # type: ignore[method-assign]

    applied = await handle._push_model_config_option(_TWO_RUNG, strict=False)
    assert applied == "claude-sonnet-4-5"
    assert writes.values == [_TWO_RUNG, "claude-sonnet-4-5"]


@pytest.mark.asyncio
async def test_a_transport_failure_propagates_untouched():
    """Anything that is neither shape must keep propagating.

    Swallowing it would report a model switch that never reached the process, so
    the dashboard would show one model while the turns run on another.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    writes = _Writes(AcpError("pipe broken"))
    handle.set_config_option = writes  # type: ignore[method-assign]

    with pytest.raises(AcpError, match="pipe broken"):
        await handle._push_model_config_option(_THREE_RUNG, strict=False)
    assert len(writes.calls) == 1


# ── Strict vs non-strict on exhaustion ────────────────────────────────────────


@pytest.mark.asyncio
async def test_exhaustion_raises_model_unavailable_under_strict():
    """Every spelling refused on an explicit pick is a failure, not a downgrade.

    A silent fallback would tell the user their chosen model is running while the
    session serves the backend default.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    refusals = [_value_refusal_named() for _ in range(3)]
    handle.set_config_option = _Writes(*refusals)  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable):
        await handle._push_model_config_option(_THREE_RUNG, strict=True)


@pytest.mark.asyncio
async def test_exhaustion_stays_on_the_backend_default_when_not_strict():
    """A substitute pick that no spelling satisfies inherits the default.

    Raising here would abort a background one-liner or a tips sweep over a model
    preference the caller never asked to be honoured exactly.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    writes = _Writes(*[_value_refusal_bare() for _ in range(3)])
    handle.set_config_option = writes  # type: ignore[method-assign]

    assert await handle._push_model_config_option(_THREE_RUNG, strict=False) == ""
    assert len(writes.calls) == 3


@pytest.mark.asyncio
async def test_a_rejected_id_is_redacted_before_it_reaches_the_message():
    """The rejected id is caller-supplied text that ends up in front of a user.

    Un-redacted, a pin that carries a URL with an embedded token publishes that
    token into the error row, the logs and every sink downstream of them.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle.set_config_option = _Writes(_value_refusal_bare())  # type: ignore[method-assign]
    leaky = "https://evil.example.com/m?access_token=AKIAIOSFODNN7EXAMPLE"

    with pytest.raises(AcpModelUnavailable) as caught:
        await handle._push_model_config_option(leaky, strict=True)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(caught.value)
    assert "REDACTED" in str(caught.value)


# ── set_model dispatch: what each harness actually sends ──────────────────────


@pytest.mark.asyncio
async def test_kas_still_sends_exactly_one_config_write():
    """KAS is NOT in the ladder's membership set, and must stay out of it.

    KAS advertises the option and accepts the resolved id. A ladder here would
    add two doomed writes after a failure that is already terminal, and would
    turn a refusal into a silent stay-on-default where today it raises -- so the
    dashboard would report a switch KAS never made.
    """
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    handle = _make_handle(ACP_BACKEND_KAS)
    writes = _Writes()
    handle.set_config_option = writes  # type: ignore[method-assign]

    await handle.set_model(_THREE_RUNG)

    assert writes.calls == [(MODEL_CONFIG_ID, _THREE_RUNG)]
    assert handle.model == _THREE_RUNG
    _mock(handle).send_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_kas_refusal_still_raises():
    """The single-write arm keeps propagating its failure.

    Absorbing it would leave the session on the old model while every surface
    above reports the new one.
    """
    handle = _make_handle(ACP_BACKEND_KAS)
    handle.set_config_option = _Writes(AcpError("nope"))  # type: ignore[method-assign]

    with pytest.raises(AcpError, match="nope"):
        await handle.set_model("some-model")


@pytest.mark.asyncio
async def test_kiro_still_uses_the_set_model_request():
    """kiro-cli's model switch is a request, not a config write.

    Sending it as a config option draws method-not-found and the session keeps
    serving turns on the model the operator thought they had just left.
    """
    handle = _make_handle(ACP_BACKEND_KIRO)
    writes = _Writes()
    handle.set_config_option = writes  # type: ignore[method-assign]

    await handle.set_model("some-model")

    assert writes.calls == []
    assert _mock(handle).send_request.await_args.args[0] == METHOD_SET_MODEL


@pytest.mark.asyncio
async def test_a_config_option_harness_records_the_spelling_that_went_on_the_wire():
    """Bookkeeping records the ACCEPTED spelling, not the one asked for.

    The context meter looks the window up by this id; recording the rejected
    spelling converts every usage percentage against a window the session is not
    running on.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle.set_config_option = _Writes(_value_refusal_bare())  # type: ignore[method-assign]

    await handle.set_model(_TWO_RUNG)

    assert handle.model == "claude-sonnet-4-5"
    assert handle._resolved_model_id == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_a_config_option_harness_that_stays_on_default_touches_no_bookkeeping():
    """An exhausted non-strict ladder must leave the recorded model alone.

    Recording a model that was refused would make the meter, the model chip and
    every log name something the session is not running.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle.set_config_option = _Writes(*[_value_refusal_bare() for _ in range(2)])  # type: ignore[method-assign]

    await handle.set_model(_TWO_RUNG)

    assert handle.model == ""
    assert handle._resolved_model_id == ""


# ── SESSION_CONFIG permission routing ─────────────────────────────────────────


def _codex_options(value: str = "read-only") -> list[dict[str, Any]]:
    option_id, _required = acp_tool_gate.permission_config_for(ACP_BACKEND_CODEX)
    return [{"id": option_id, "options": [{"value": value}]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
async def test_an_agent_spec_harness_sends_no_permission_write(backend):
    """The routing gate makes this whole capability dead code on kiro and KAS.

    Both route ``Routing.AGENT_SPEC``. An extra ``set_config_option`` on their
    session start would draw a rejection from an option neither advertises and
    fail every session at init.
    """
    assert acp_tool_gate.routing_for(backend) is acp_tool_gate.Routing.AGENT_SPEC
    handle = _make_handle(backend)
    writes = _Writes()
    handle.set_config_option = writes  # type: ignore[method-assign]

    await handle.apply_session_permission_routing()

    assert writes.calls == []


@pytest.mark.asyncio
async def test_an_unadvertised_option_refuses_as_indeterminate():
    """No advertisement is INDETERMINATE, and INDETERMINATE still refuses.

    "Cannot tell" must not read as armed: letting the session start would run the
    harness's first prompt with the denied-command rules, the sensitive-path
    block and the governance ceiling never consulted.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle._config_options = []
    writes = _Writes()
    handle.set_config_option = writes  # type: ignore[method-assign]

    with pytest.raises(AcpToolGateUnroutable):
        await handle.apply_session_permission_routing()
    assert writes.calls == []


@pytest.mark.asyncio
async def test_an_advertised_option_whose_write_fails_refuses_as_bypassed():
    """An advertised option that will not take is an OBSERVED bypass.

    Continuing would arm nothing while the session reports a permission route it
    does not have.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle._config_options = _codex_options()
    handle.set_config_option = _Writes(AcpError("rejected"))  # type: ignore[method-assign]

    with pytest.raises(AcpToolGateUnroutable):
        await handle.apply_session_permission_routing()


@pytest.mark.asyncio
async def test_an_advertised_option_that_takes_arms_the_route():
    """The success path writes the harness's OWN required option and value.

    Writing anything else leaves the harness on a mode that permits workspace
    changes without asking.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    handle._config_options = _codex_options()
    writes = _Writes()
    handle.set_config_option = writes  # type: ignore[method-assign]

    await handle.apply_session_permission_routing()

    assert writes.calls == [acp_tool_gate.permission_config_for(ACP_BACKEND_CODEX)]


# ── What the handle reads off the runtime ─────────────────────────────────────


def test_the_protocol_declares_the_runtime_dead_marking_the_handle_calls():
    """``_mark_dead`` is declared, and ``AcpRuntime`` still matches its signature.

    The handle calls it when a permission answer it cannot verify leaves the
    child waiting on a stranded oneshot. Reached by name instead of declared, a
    runtime that renamed it would skip the escalation silently and the child
    would hang with no error anywhere.
    """
    declared = inspect.signature(AcpRuntimeProtocol._mark_dead)
    assert declared == inspect.signature(AcpRuntime._mark_dead)


@pytest.mark.asyncio
async def test_is_responsive_reads_the_activity_clock_through_one_seam():
    """Responsiveness folds the runtime clock in through ``_runtime_idle_secs``.

    Every other read of that clock is the dispatch loop's own per-session
    bookkeeping. A second ad-hoc read here is how the two answers drift, and a
    stale-looking runtime gets its live sessions SIGTERMed.
    """
    handle = _make_handle()
    handle._runtime_idle_secs = lambda: 1.0  # type: ignore[method-assign]
    assert handle.is_responsive(stale_threshold=600.0) is True

    handle._runtime_idle_secs = lambda: 700.0  # type: ignore[method-assign]
    assert handle.is_responsive(stale_threshold=600.0) is False

    _mock(handle).is_alive.return_value = False
    handle._runtime_idle_secs = lambda: 0.0  # type: ignore[method-assign]
    assert handle.is_responsive() is False


@pytest.mark.asyncio
async def test_an_error_frame_carries_its_jsonrpc_code_onto_the_exception():
    """The raw ``code`` survives the shared raise helper.

    The helper classifies from the frame's prose, and codex's value refusal has
    none. Without the code the ladder cannot tell that refusal from a malformed
    request, so it re-raises, session init fails, and every session on that
    harness dies at startup on a stale model pin.
    """
    handle = _make_handle(ACP_BACKEND_CODEX)
    frame = JsonRpcMessage(
        id=7,
        error={"code": _JSONRPC_INVALID_PARAMS, "message": "Invalid params"},
    )
    handle._queue.put_nowait(frame)

    with pytest.raises(AcpError) as caught:
        await handle._wait_for_response(7, timeout=5.0)
    assert caught.value.code == _JSONRPC_INVALID_PARAMS


def test_both_drivers_classify_a_config_option_rejection_alike():
    """The two model-push copies must recognise the same rejection signals.

    The ladder's ORDER is shared -- both drivers call
    ``AcpClient._model_config_candidates`` -- but the CLASSIFICATION cannot be,
    because each copy calls its own send seam. That leaves the subtle half
    duplicated, and its divergence is silent: a copy that stopped treating one
    signal as a value refusal would re-raise instead of trying the next spelling,
    the session would fail to start, and both copies would still parse and still
    pass their own tests.

    Three signals, and each means "the host rejected the VALUE, try the next
    spelling" rather than "this option does not exist": one substring for a host
    that names the option in its message, one for a host that names it
    differently, and a bare invalid-params CODE for a host that sends no detail at
    all -- there the code IS the rejection, because the request shape is fixed and
    the value is the only thing that varied.
    """
    import inspect

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.session_handle import AcpSessionHandle

    signals = ("unknown config option", "config option model", "_JSONRPC_INVALID_PARAMS")
    client_src = inspect.getsource(AcpClient._push_model_config_option)
    handle_src = inspect.getsource(AcpSessionHandle._push_model_config_option)
    for signal in signals:
        assert signal in client_src, f"client lost {signal!r}"
        assert signal in handle_src, f"handle lost {signal!r}"

    # Neither copy may grow a branch the other lacks: an extra `in lowered` test on
    # one side is a rejection the other still re-raises on.
    assert client_src.count("in lowered") == handle_src.count("in lowered")


def test_the_candidate_ladder_has_one_home():
    """Only one module declares the spelling order.

    Two copies of an ORDER drift without a symptom -- both return candidates, and
    only a cold cache on one driver reveals which list ran.
    """
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.session_handle import AcpSessionHandle

    assert hasattr(AcpClient, "_model_config_candidates")
    assert "_model_config_candidates" not in vars(AcpSessionHandle)
