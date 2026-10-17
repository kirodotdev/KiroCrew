"""Network-drop classification and the connection recovery window policy."""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.transport_errors import (
    PROVIDER_ERROR_CONNECTION,
    _raise_acp_error,
    classify_provider_error,
)
from kiro_crew.llm_helpers import (
    CONNECTION_RECOVERY_WINDOW_SECS,
    CONNECTION_RETRY_MAX_DELAY,
    acp_error_is_connection_failure,
    connection_recovery_open,
    connection_retry_delay,
    transient_retry_delay,
)


def _raised(data: str, message: str = "Internal error") -> AcpError:
    try:
        _raise_acp_error({"code": -32603, "message": message, "data": data})
    except AcpError as exc:
        return exc
    raise AssertionError("_raise_acp_error did not raise")


@pytest.mark.parametrize(
    "data",
    [
        "dispatch failure: io error: connection reset by peer",
        "DispatchFailure(DispatchFailure { source: ConnectorError })",
        "connect ECONNREFUSED 127.0.0.1:443",
        "getaddrinfo EAI_AGAIN bedrock-runtime.example",
        "socket hang up",
        "connection timed out",
    ],
)
def test_network_path_drops_are_tagged(data):
    exc = _raised(data)
    assert exc.transient is True
    assert exc.connection_failure is True
    assert acp_error_is_connection_failure(exc)


@pytest.mark.parametrize(
    "data",
    [
        "InternalServerException: internal server error, please try again",
        "ThrottlingException: Too many requests",
        "ExpiredTokenException: the security token included in the request is expired",
    ],
)
def test_provider_answers_are_not_network_drops(data):
    exc = _raised(data)
    assert exc.connection_failure is False
    assert not acp_error_is_connection_failure(exc)


def test_unclassified_error_falls_back_to_message_text():
    drop = AcpError("Could not reach the model backend (connection refused, reset, or timed out).")
    assert drop.connection_failure is None
    assert acp_error_is_connection_failure(drop)
    assert not acp_error_is_connection_failure(AcpError("Bedrock authentication failed"))


def test_terminal_verdict_wins_over_connection_wording():
    exc = AcpError("connection reset", transient=False)
    exc.connection_failure = True
    assert not acp_error_is_connection_failure(exc)


def test_tag_and_shared_classifier_agree():
    for data in ("dispatch failure", "connection reset", "status code 503 connection reset"):
        assert classify_provider_error(data).kind == PROVIDER_ERROR_CONNECTION
        assert _raised(data).connection_failure is True
    throttled = "ThrottlingException after connection reset"
    assert classify_provider_error(throttled).kind != PROVIDER_ERROR_CONNECTION
    assert _raised(throttled).connection_failure is False


def test_window_is_measured_from_the_first_retry():
    drop = _raised("dispatch failure")
    start = 1000.0
    assert connection_recovery_open(drop, start, now=start + 1)
    assert connection_recovery_open(drop, start, now=start + CONNECTION_RECOVERY_WINDOW_SECS - 1)
    assert not connection_recovery_open(drop, start, now=start + CONNECTION_RECOVERY_WINDOW_SECS)
    # No ladder in progress: never open.
    assert not connection_recovery_open(drop, 0.0, now=start)


def test_window_never_opens_for_a_provider_error():
    five_xx = _raised("InternalServerException: internal server error")
    assert not connection_recovery_open(five_xx, 1000.0, now=1001.0)


def test_connection_backoff_matches_the_ladder_then_caps():
    import kiro_crew.llm_helpers as helpers

    rng_state = helpers._JITTER_RNG.getstate()
    try:
        for attempt in (1, 2, 3):
            helpers._JITTER_RNG.seed(attempt)
            expected = transient_retry_delay(attempt)
            helpers._JITTER_RNG.seed(attempt)
            assert connection_retry_delay(attempt) == expected
        for attempt in (5, 6, 20, 1000):
            delay = connection_retry_delay(attempt)
            assert CONNECTION_RETRY_MAX_DELAY <= delay <= CONNECTION_RETRY_MAX_DELAY * 1.25
    finally:
        helpers._JITTER_RNG.setstate(rng_state)
