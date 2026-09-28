"""Unit coverage for the E2E harness's startup-recovery wait.

``_await_memory_recovery`` is what stops an E2E test from writing inside the
window between ``KIROCREW_READY`` and the end of memory recovery. It runs only
in the gated E2E job, so without these tests a change to it would be proven by
nothing until a real gateway race reappeared in CI.

The fake client raises exactly what ``_Client._open`` raises: an
``AssertionError`` whose ``__cause__`` is the ``HTTPError``.
"""

from __future__ import annotations

import urllib.error

import pytest
from e2e.test_gateway_boot_matrix import _await_memory_recovery


def _http_error(code: int) -> AssertionError:
    cause = urllib.error.HTTPError(
        "http://localhost/api/memory/stats?store=default", code, "refused", {}, None  # type: ignore[arg-type]
    )
    failure = AssertionError(f"GET -> HTTP {code}")
    failure.__cause__ = cause
    return failure


class _FakeClient:
    """Answers each GET from a scripted list; a callable entry raises."""

    def __init__(self, answers: list[object]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def get(self, path: str) -> dict:
        self.calls += 1
        answer = self._answers.pop(0) if self._answers else {"ok": True}
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]


@pytest.mark.parametrize("code", [503, 409])
def test_waits_through_a_recovery_refusal_then_returns(code, monkeypatch):
    """503 on a read and 409 on a write are the same startup refusal."""
    monkeypatch.setattr("e2e.test_gateway_boot_matrix.time.sleep", lambda _s: None)
    client = _FakeClient([_http_error(code), _http_error(code), {"ok": True}])
    _await_memory_recovery(client, secs=5)  # type: ignore[arg-type]
    assert client.calls == 3


def test_a_non_recovery_error_is_raised_at_once(monkeypatch):
    """A 500 is not "not yet": surface it instead of burning the whole budget."""
    monkeypatch.setattr("e2e.test_gateway_boot_matrix.time.sleep", lambda _s: None)
    client = _FakeClient([_http_error(500), {"ok": True}])
    with pytest.raises(AssertionError):
        _await_memory_recovery(client, secs=5)  # type: ignore[arg-type]
    assert client.calls == 1


def test_a_store_that_never_recovers_fails_with_the_gateways_own_body(monkeypatch):
    """The wait is bounded, and the failure carries the refusal it kept seeing."""
    monkeypatch.setattr("e2e.test_gateway_boot_matrix.time.sleep", lambda _s: None)
    client = _FakeClient([_http_error(503) for _ in range(50)])
    with pytest.raises(AssertionError) as caught:
        _await_memory_recovery(client, secs=0)  # type: ignore[arg-type]
    assert isinstance(caught.value.__cause__, urllib.error.HTTPError)
    assert caught.value.__cause__.code == 503


def test_a_ready_gateway_is_not_polled_twice(monkeypatch):
    """No sleep, no second request when recovery already finished."""
    monkeypatch.setattr("e2e.test_gateway_boot_matrix.time.sleep", lambda _s: None)
    client = _FakeClient([{"ok": True}])
    _await_memory_recovery(client)  # type: ignore[arg-type]
    assert client.calls == 1
