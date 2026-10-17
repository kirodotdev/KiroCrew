"""The ``/api/sessions/usage`` readiness gate must not re-probe on every poll.

``/api/sessions/usage`` is polled every 30s by the credit pill. It passes
through ``reject_if_kiro_unverified`` -> ``verified_ready``, which re-runs a full
``whoami`` probe inline whenever the readiness latch is older than its max-age. A
max-age equal to the poll interval means the latch is expired at almost every
poll, so each poll pays for a forced probe inline -- the 5-12 s server waits the
credit pill exhibits.

The fix: ``/api/sessions/usage`` reads on a max-age comfortably longer than its
poll interval (and safe because its spawn is already throttled to one fetch per
600s), so a healthy latch authorizes many polls between probes. Everything
else -- the destructive-rerun paths (regenerate / edit-resend / rewind),
``/v1/chat/completions``, and the degraded-only ``/api/models`` poll -- keeps the
tight 30s max-age.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard import kiro_readiness
from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, PrerequisiteStatus


class _Clock:
    """A monotonic clock a test advances by hand."""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _probed_ready_service(clock: _Clock) -> KiroPrerequisiteService:
    """A ready service whose latch was stamped at the clock's current time."""

    service = KiroPrerequisiteService(assume_ready=False, clock=clock, home=None)
    service._status = PrerequisiteStatus(
        platform="linux",
        installed=True,
        authenticated=True,
        ready=True,
        initial_setup_complete=True,
    )
    service._has_probed = True
    service._last_probe_at = clock()
    return service


def _request(service: KiroPrerequisiteService) -> MagicMock:
    tasks: set[object] = set()
    app: dict[str, object] = {
        "kiro_prerequisite_service": service,
        "state": SimpleNamespace(
            kiro_prerequisite_service=service,
            _background_tasks=tasks,
        ),
    }
    request = MagicMock()
    request.app = app
    return request


@pytest.fixture(autouse=True)
def _reset_refusal_warning():
    kiro_readiness._clear_refusal_warning()
    yield
    kiro_readiness._clear_refusal_warning()


@pytest.fixture(autouse=True)
def _empty_catalog_cache(_floor_monkeypatch: pytest.MonkeyPatch):
    # ``api_models`` serves a warm module-level catalog before it would reach the
    # unresolved-binary 503 this file asserts. Another test in the same worker
    # can leave that cache warm, which turned the 503 into a 200 on the release
    # runner. A fresh cache per test makes the outcome independent of order.
    _floor_monkeypatch.setattr(agents, "_catalog_cache", agents._CatalogCache())


@pytest.mark.asyncio
async def test_api_models_keeps_the_tight_bound_and_reprobes_a_stale_latch() -> None:
    """``/api/models`` is polled only while degraded and has no spawn cooldown, so
    it keeps the 30 s destructive bound: a latch a minute old must re-probe before
    it can authorize the browser-opening spawn."""
    clock = _Clock()
    service = _probed_ready_service(clock)
    # Older than the 30 s bound /api/models reads.
    clock.advance(60.0)

    with patch.object(service, "_probe", AsyncMock()) as probe:
        with patch(
            "kiro_crew.acp.client._resolve_kiro_bin_for_spawn",
            AsyncMock(return_value=""),
        ):
            resp = await agents.api_models(_request(service))

    probe.assert_awaited_once()
    # The stubbed probe left the latch ready=True, so the gate authorizes and
    # falls through to the pre-existing degraded branch (binary unresolved).
    assert resp.status == 503
    assert json.loads(cast(bytes, resp.body)) == {"error": "kiro binary not resolved"}


@pytest.mark.asyncio
async def test_api_sessions_usage_does_not_reprobe_within_the_poll_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latch a minute old still authorizes ``/api/sessions/usage`` with no probe."""
    clock = _Clock()
    service = _probed_ready_service(clock)
    clock.advance(60.0)
    monkeypatch.setattr(sessions, "_usage_cache_ts", 0.0)

    with patch.object(service, "_probe", AsyncMock()) as probe:
        with patch.object(sessions, "_fetch_usage_bg", AsyncMock()):
            resp = await sessions.api_sessions_usage(_request(service))

    probe.assert_not_awaited()
    assert resp.status == 200


@pytest.mark.asyncio
async def test_destructive_rerun_gate_still_reprobes_a_stale_latch() -> None:
    """The 30 s max-age survives for the paths that rewrite persisted history."""
    clock = _Clock()
    service = _probed_ready_service(clock)
    clock.advance(60.0)

    with patch.object(service, "_probe", AsyncMock()) as probe:
        # Default (destructive) max-age: a latch older than 30 s must re-probe.
        blocked = await kiro_readiness.reject_if_kiro_unverified(_request(service))

    probe.assert_awaited_once()
    # The stubbed probe left the latch ready=True, so the gate authorizes.
    assert blocked is None


@pytest.mark.asyncio
async def test_poll_gate_max_age_is_longer_than_the_poll_interval() -> None:
    """The constant ``/api/sessions/usage`` reads must exceed its poll cadence."""
    # /api/sessions/usage polls every 30 s.
    assert kiro_readiness._POLL_GATE_MAX_AGE_SECS > 30.0
