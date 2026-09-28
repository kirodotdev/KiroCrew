"""A control-plane handshake timeout is transient by exception TYPE.

``_send_and_await`` serves only control-plane requests — ``initialize`` /
``session/new`` / ``session/load`` / ``set_mode`` / teardown — and raises
``AcpRequestTimeout`` when the runtime is slow to answer one. No prompt is
dispatched at any of those stages, so a timeout there means the work was never
attempted, not that it failed. A cron cold-start stall is transient host
weather that the retry layer should retry rather than count as a job failure.

The verdict rides on the type: ``AcpRequestTimeout.transient`` is ``True``,
which ``acp_error_is_transient`` reads structurally (the same channel
``AcpError.transient`` uses), so the cron callback's transient-retry arm
(``acp_error_is_transient(exc) and not _prompt_dispatched``) fires. Deciding by
type rather than by a ``"timed out"`` string in ``_TRANSIENT_MARKERS`` keeps a
reword of the message from flipping retryability.
"""

from kiro_crew.acp.client import AcpTimeoutError
from kiro_crew.acp.session_handle import AcpRequestTimeout, AcpRuntimeError
from kiro_crew.llm_helpers import acp_error_is_transient


class TestHandshakeTimeoutIsStructurallyTransient:
    def test_request_timeout_is_transient(self) -> None:
        # The message the runtime raises (runtime.py:_send_and_await).
        exc = AcpRequestTimeout("Request initialize timed out after 30s")
        assert acp_error_is_transient(exc)

    def test_verdict_comes_from_type_not_wording(self) -> None:
        # Message carries no word in _TRANSIENT_MARKERS ("timed out" is not a
        # marker), so a True verdict can only come from the structural flag.
        from kiro_crew.llm_helpers import is_transient_backend_error

        exc = AcpRequestTimeout("Request initialize took too long")
        assert not is_transient_backend_error(str(exc))  # string classifier says no
        assert acp_error_is_transient(exc)  # type says yes

    def test_flag_lives_on_the_class(self) -> None:
        # Attribute is on the type, so every instance and subclass-constructed
        # replacement inherits it without the raise site setting it.
        assert AcpRequestTimeout.transient is True

    def test_enriched_replacement_stays_transient(self) -> None:
        # runtime._session_start_stalled raises a fresh AcpRequestTimeout
        # wrapping the original plus MCP progress; the class attribute survives.
        original = AcpRequestTimeout("Request session/new timed out after 90s")
        enriched = AcpRequestTimeout(f"{original} (mcp-server-foo: awaiting)")
        assert acp_error_is_transient(enriched)

    def test_bare_runtime_error_is_not_transient(self) -> None:
        # The base class carries no flag and no transient wording, so a plain
        # AcpRuntimeError fails fast — only the timeout subclass is retried.
        assert not acp_error_is_transient(AcpRuntimeError("runtime is dead"))

    def test_prompt_timeout_unaffected(self) -> None:
        # The prompt stream raises AcpTimeoutError (an AcpError), not
        # AcpRequestTimeout. Its transient verdict is decided elsewhere (raw
        # error / message); the default is unclassified.
        assert getattr(AcpTimeoutError("ACP prompt timed out"), "transient", None) is None


class TestCronTransientArmFires:
    """The gateway cron callback retries when
    ``acp_error_is_transient(exc) and not _prompt_dispatched`` — pin that a
    handshake timeout satisfies both halves."""

    def test_handshake_timeout_satisfies_the_retry_condition(self) -> None:
        exc = AcpRequestTimeout("Request initialize timed out after 30s")
        _prompt_dispatched = False  # no prompt is dispatched at handshake
        assert acp_error_is_transient(exc) and not _prompt_dispatched
