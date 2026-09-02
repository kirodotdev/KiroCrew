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

# kiro-cli >= 2.16 rewording of the capacity rejection, which names NO model.
# The exact frame from a live cron failure (gateway.log 2026-08-12 02:57), with
# the request id value swapped. Before its own pattern existed this fell
# through to the unknown-shape branch and classified TERMINAL, so unattended
# callers failed fast on a momentary blip instead of retrying.
_MODEL_TEMP_UNAVAILABLE = {
    "code": -32603,
    "message": "Internal error",
    "data": (
        "The model you've selected is temporarily unavailable. Please use "
        "'/model' to select a different model and try again. (request_id: "
        "863ae6fe-de1d-4149-b3ff-6ee02d8d58a2)"
    ),
}

# A structural "Improperly formed request" rejection (#6022). The backend
# passes this string through verbatim; before FEAT-001 it fell through to the
# unknown-shape branch (raw passthrough) with an incidental terminal verdict.
_MALFORMED_REQUEST = {
    "code": -32603,
    "message": "Internal error",
    "data": "Improperly formed request. (request_id: 863ae6fe-de1d-4149-b3ff-6ee02d8d58a2)",
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
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_prompt_busy_guidance_names_no_unknown_command(self, driver):
        """The busy branch must not name `!restart` either.

        Same defect class as the malformed-request branch, in the same
        surface-blind formatter: `!restart` is a Slack-only bang alias, it is
        owner-gated even there, and it restarts the GATEWAY rather than the
        session -- so on every other surface it is an inert instruction. It was
        also unnecessary, because the handlers reset and re-queue on
        AcpPromptBusy on their own. Asserted as an absence, and paired with a
        positive check that the recovery advice survived (#7213).
        """
        msg = str(await driver(_PROMPT_BUSY))

        assert "!restart" not in msg
        assert "clears on its own" in msg

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


class TestNamelessCapacityWording:
    """kiro-cli >= 2.16's nameless capacity rejection must stay retryable.

    The rewording dropped the model name from "The model 'X' is not
    available", so ``_RE_MODEL_UNAVAILABLE`` no longer matches and the error
    fell through to the unknown-shape branch: passthrough text (fine) with a
    TERMINAL verdict (not fine). A cron hit exactly this during a backend
    capacity blip — the run before and after both succeeded — and failed
    without spending a single retry attempt.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_verdict_is_transient(self, driver):
        """The retry ladder must get a shot at a momentary capacity blip."""
        assert (await driver(_MODEL_TEMP_UNAVAILABLE)).transient is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_formatted_is_actionable(self, driver):
        msg = str(await driver(_MODEL_TEMP_UNAVAILABLE))

        # Rewritten into the capacity guidance, not the raw passthrough.
        assert "model picker" in msg
        # The provider's '/model' TUI advice does nothing in the dashboard,
        # Slack, or a cron — it must not be echoed (same contract as the
        # named-model branch).
        assert "/model" not in msg
        # request_id survives for support correlation.
        assert "863ae6fe-de1d-4149-b3ff-6ee02d8d58a2" in msg

    def test_message_field_echo_does_not_flip_verdict(self):
        """The pattern is data-scoped, like its named sibling.

        A phrase echo in the JSON-RPC ``message`` alone must not reclassify an
        otherwise-terminal error as transient.
        """
        from kiro_crew.acp.client import _is_transient_raw_error

        echo_only = {
            "code": -32603,
            "message": "The model you've selected is temporarily unavailable.",
            "data": "ValidationException: malformed request",
        }
        assert _is_transient_raw_error(echo_only) is False

    def test_typographic_apostrophe_still_matches(self):
        """Providers flip between straight and curly quotes; both must match."""
        from kiro_crew.acp.client import _is_transient_raw_error

        curly = dict(_MODEL_TEMP_UNAVAILABLE, data="The model you\u2019ve selected is temporarily unavailable.")
        assert _is_transient_raw_error(curly) is True

    def test_pre_rewrite_passthrough_still_classifies_via_marker(self):
        """A transcript written by a pre-fix gateway carries the raw wording.

        The string-fallback path (``_TRANSIENT_MARKERS``) must recognise it so
        history-restored messages keep their retry verdict.
        """
        from kiro_crew.llm_helpers import is_transient_backend_error

        assert is_transient_backend_error(str(_MODEL_TEMP_UNAVAILABLE["data"]))


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
            _MODEL_TEMP_UNAVAILABLE,
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


class TestMalformedRequestReachesTheHandlePath:
    """The shared-runtime path must surface the structural-rejection guidance
    (#6022) and carry a terminal verdict, mirroring TestNoRawDictInUserFacingError.

    "Improperly formed request" is a DETERMINISTIC structural rejection: the
    identical payload cannot succeed on retry, so both handle raise sites must
    format it into repair guidance (never the raw dict) and mark it
    non-retryable so it is not re-sent in a loop.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_malformed_request_is_actionable(self, driver):
        msg = str(await driver(_MALFORMED_REQUEST))

        # The dict repr and the JSON-RPC boilerplate are gone.
        assert "'code': -32603" not in msg
        assert "ACP error: {" not in msg
        assert "Internal error" not in msg
        # It names the failure class and offers a repair affordance.
        assert "malformed" in msg.lower()
        assert "/compact" in msg
        # request_id survives for support correlation.
        assert "863ae6fe-de1d-4149-b3ff-6ee02d8d58a2" in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_malformed_request_guidance_names_no_unknown_command(self, driver):
        """Every command the guidance names must exist on the reader's surface.

        This error is TERMINAL, so a channel dispatcher hands the formatted
        string straight to the user (telegram's ``_user_safe_failure_reason``
        only surfaces ``transient is False``). The formatter does not know which
        surface will render it, so it may name only commands spelled the same
        everywhere. ``/chat new`` was not one: it exists nowhere in the product
        (``/new`` on Telegram and Discord, a new tab on the dashboard), so the
        one actionable instruction a stuck user received was inert -- and on a
        session this broken the non-command is forwarded as a prompt and
        re-fails with this same error (#7213).

        Asserted as an absence rather than by re-stating the sentence, so the
        test constrains the CLASS of mistake (a fabricated command) instead of
        pinning wording a later copy edit is entitled to change.
        """
        msg = str(await driver(_MALFORMED_REQUEST))

        assert "/chat new" not in msg
        # The reset affordance survives as prose, so the guidance still tells the
        # user what to do -- deleting the wrong command must not delete the advice.
        assert "new conversation" in msg.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", [_raise_via_wait, _raise_via_dispatch])
    async def test_malformed_request_verdict_is_terminal(self, driver):
        """A structurally-rejected payload must not be re-sent by the ladder."""
        assert (await driver(_MALFORMED_REQUEST)).transient is False
