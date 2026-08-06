"""Tests for how a JSON-RPC error frame reaches the user.

``AcpSessionHandle`` (the shared-runtime path every dashboard chat takes) used
to raise ``AcpError(f"ACP error: {msg.error}")`` — a ``repr`` of the raw
``{code, message, data}`` dict — so a routine model-unavailable failure landed
in the transcript as an unreadable red wall:

    ACP error: {'code': -32603, 'message': 'Internal error', 'data':
    "Encountered an error in the response stream: The model 'claude-opus-4.8'
    is not available. Please use '/model' to select a different model and try
    again. (request_id: ...)"}

Meanwhile ``AcpClient`` — the other producer of the identical frame — routed
through ``_raise_acp_error``, which rewrites known failure modes into recovery
steps, redacts credentials, and raises ``AcpPromptBusy`` for a concurrent
prompt. Both handle sites now delegate to that helper, so the two producers
cannot drift.

The raw dump was quietly LOAD-BEARING: ``chat_runner`` decided prompt-busy
retry eligibility by searching the message for "already in progress", which
only ever matched because the dict was echoed verbatim. Formatting rewrites
that marker away, so the runner's check is now structural
(``isinstance(exc, AcpPromptBusy)``) with the string kept as a fallback. The
coupling guard below fails if a future reformat re-breaks it.

Both handle sites also pass the session's advertised model ids, so the
entitlement discriminator added by #1550 (``_model_is_unentitled``) actually
fires on this path. #1550 wired only the ``AcpClient`` sites, and this handle
is the path every dashboard chat takes.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError, AcpPromptBusy
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage

# The exact frame from the reported transcript, minus the request id value.
_MODEL_UNAVAILABLE = {
    "code": -32603,
    "message": "Internal error",
    "data": (
        "Encountered an error in the response stream: The model "
        "'claude-opus-4.8' is not available. Please use '/model' to select a "
        "different model and try again. (request_id: "
        "863ae6fe-de1d-4149-b3ff-6ee02d8d58a2)"
    ),
}

_PROMPT_BUSY = {
    "code": -32603,
    "message": "Internal error",
    "data": "Prompt already in progress",
}


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    h = AcpSessionHandle("sErr", asyncio.Queue(), rt)
    h._turn_done.clear()
    return h


async def _raise_via_wait(error: dict, models: list[dict] | None = None) -> BaseException:
    """Drive ``_wait_for_response`` to the error branch and return the raised exc."""
    h = _handle()
    if models is not None:
        h._available_models = models
    h._queue.put_nowait(JsonRpcMessage(id=7, error=error))
    with pytest.raises(AcpError) as ei:
        await h._wait_for_response(7, timeout=5.0)
    return ei.value


async def _raise_via_dispatch(error: dict, models: list[dict] | None = None) -> BaseException:
    """Drive ``_dispatch_events`` to the error branch and return the raised exc."""
    h = _handle()
    if models is not None:
        h._available_models = models
    h._queue.put_nowait(JsonRpcMessage(id=7, error=error))
    with pytest.raises(AcpError) as ei:
        async for _ev in h._dispatch_events(7, timeout=5.0):
            pass
    return ei.value


class TestNoRawDictInUserFacingError:
    """Both handle raise sites must format, never dump the JSON-RPC dict."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_model_unavailable_is_actionable(self, driver):
        msg = str(await driver(_MODEL_UNAVAILABLE))

        # The dict repr and the JSON-RPC boilerplate are gone.
        assert "'code': -32603" not in msg
        assert "ACP error: {" not in msg
        assert "Internal error" not in msg
        # The failing model and the recovery step are present.
        assert "claude-opus-4.8" in msg
        assert "model picker" in msg
        # The provider's own advice names a kiro-cli TUI command that does
        # nothing in the dashboard, Slack, or a cron — it must not be echoed.
        assert "/model" not in msg
        # request_id survives for support correlation.
        assert "863ae6fe-de1d-4149-b3ff-6ee02d8d58a2" in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_transient_verdict_still_carried(self, driver):
        """Formatting must not cost the retry ladder its structured verdict."""
        assert (await driver(_MODEL_UNAVAILABLE)).transient is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_prompt_busy_raises_subclass(self, driver):
        """A concurrent in-flight prompt gets the typed exception, not bare AcpError."""
        exc = await driver(_PROMPT_BUSY)
        assert isinstance(exc, AcpPromptBusy)
        assert "still processing a previous request" in str(exc)

    @pytest.mark.asyncio
    async def test_unknown_shape_still_preserved(self):
        """An unrecognised frame must not be swallowed — the raw shape is kept."""
        msg = str(await _raise_via_wait({"code": -32602, "message": "Invalid params"}))
        assert "Invalid params" in msg


class TestEntitlementReachesTheHandlePath:
    """The shared-runtime path must feed the entitlement discriminator too.

    #1550 added ``_model_is_unentitled`` and wired it into the three
    ``AcpClient`` raise sites. ``AcpSessionHandle`` is the path every dashboard
    chat actually takes, so without passing its advertised ids the entitlement
    split stays inert exactly where users hit it: a free-tier rejection would
    still read as a capacity blip and still burn the retry ladder.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_unentitled_model_is_terminal_and_names_alternatives(self, driver):
        exc = await driver(
            _MODEL_UNAVAILABLE,
            [
                {"modelId": "claude-sonnet-4-5", "name": "Sonnet", "description": ""},
                {"modelId": "auto", "name": "Auto", "description": ""},
            ],
        )
        msg = str(exc)
        # Access problem, not capacity — and retrying is called out as futile.
        assert "does not have access" in msg
        assert "claude-sonnet-4-5" in msg
        assert "Retrying will not help" in msg
        # Terminal: the retry ladder must not spend attempts reproducing this.
        assert exc.transient is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_advertised_model_stays_a_retryable_capacity_blip(self, driver):
        """A rejected model the account WAS offered is a genuine transient."""
        exc = await driver(
            _MODEL_UNAVAILABLE,
            [{"modelId": "claude-opus-4.8", "name": "Opus", "description": ""}],
        )
        assert "does not have access" not in str(exc)
        assert exc.transient is True

    @pytest.mark.asyncio
    async def test_empty_advertised_list_leaves_behaviour_unchanged(self):
        """No advertised set means entitlement is unknowable — stay transient."""
        exc = await _raise_via_wait(_MODEL_UNAVAILABLE, [])
        assert "does not have access" not in str(exc)
        assert exc.transient is True


class TestRunnerPromptBusyIsStructural:
    """The runner's retry gate must not depend on error-message wording."""

    def test_formatted_prompt_busy_has_no_string_marker(self):
        """Coupling guard: proves the string check ALONE would now miss.

        If a future edit reintroduces "already in progress" into the formatted
        text this assertion fails — which is the signal to re-check that the
        structural isinstance branch (not the resurrected substring) is what
        keeps the reset-and-requeue path alive.
        """
        from kiro_crew.acp.client import _format_acp_error

        assert "already in progress" not in _format_acp_error(_PROMPT_BUSY)

    def test_runner_gate_matches_typed_exception(self):
        """The runner's own predicate, evaluated on a formatted AcpPromptBusy."""
        from kiro_crew.acp.client import _format_acp_error

        exc = AcpPromptBusy(_format_acp_error(_PROMPT_BUSY))
        _msg = str(exc)
        # Mirrors chat_runner's `_retry_eligible` expression.
        eligible = (
            isinstance(exc, AcpPromptBusy)
            or "already in progress" in _msg
            or "process exited" in _msg
            or "not running" in _msg
        )
        assert eligible


class TestTransientMarkerCoupling:
    """``llm_helpers`` string fallback must recognise the formatted wording.

    ``acp_error_is_transient`` prefers the structured ``.transient`` flag, but
    unattended callers (``stream_and_collect``) and history-restored messages
    still fall back to substring matching against ``_TRANSIENT_MARKERS``, whose
    entries quote ``_format_acp_error``'s prose verbatim. Rewording a branch
    without updating that tuple makes a retryable failure look terminal — which
    is exactly what #1550 did when it changed "on Bedrock" to "on the backend"
    and left the marker behind.

    Scope note: this pins the OUTCOME (formatted transients classify), not any
    single marker. The capacity branch is matched twice over — by
    "is unavailable on the backend" and incidentally by "throttl" in its own
    "capacity throttle" clause — so dropping either one alone does not fail
    here. That redundancy is deliberate belt-and-braces.
    """

    @pytest.mark.parametrize(
        "error",
        [
            _MODEL_UNAVAILABLE,
            {"code": -32603, "message": "Internal error", "data": "ThrottlingException"},
            {"code": -32603, "message": "Internal error", "data": "InternalServerError"},
        ],
    )
    def test_formatted_transient_still_classifies(self, error):
        from kiro_crew.acp.client import _format_acp_error
        from kiro_crew.llm_helpers import is_transient_backend_error

        assert is_transient_backend_error(_format_acp_error(error))

    def test_unentitled_wording_is_not_marked_transient(self):
        """The terminal sibling branch must NOT match a retry marker.

        A marker that caught the unentitled text would resurrect the pointless
        retry loop #1550 removed, via the string-fallback path.
        """
        from kiro_crew.acp.client import _format_acp_error
        from kiro_crew.llm_helpers import is_transient_backend_error

        formatted = _format_acp_error(_MODEL_UNAVAILABLE, ["claude-sonnet-4-5"])
        assert "does not have access" in formatted
        assert not is_transient_backend_error(formatted)
