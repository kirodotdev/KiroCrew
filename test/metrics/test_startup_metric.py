"""Tests for the kirocrew.session.startup.duration histogram contract.

Drives AcpClient.ensure_ready() through each exit path (success / auth_required /
error / unexpected / cancelled) and asserts the emitted histogram attributes -- in particular
that an unexpected (non-Acp) exception is NOT recorded as a healthy "ready"
outcome (regression guard for the outcome-default fix).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpAuthRequired, AcpClient, AcpError, AcpToolGateUnroutable


class _CapturingRecorder:
    """Stand-in recorder that records every histogram() and counter() call."""

    def __init__(self) -> None:
        self.calls: list = []
        self.counters: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.calls.append((name, dict(attrs or {})))

    def counter(self, name, value=1, *, unit="1", attrs=None, **kwargs) -> None:
        self.counters.append((name, dict(attrs or {})))


def _client() -> AcpClient:
    client = AcpClient()
    client._process = None
    client._session_id = None
    client._kill_process = AsyncMock()
    client._reset_state = MagicMock()
    client._snapshot_process_tree = AsyncMock()
    return client


def _spawn_ok(client):
    async def _fake():
        client._process = MagicMock()
        client._process.returncode = None

    return _fake


def _last_outcome(rec):
    assert rec.calls, "startup histogram must be emitted"
    name, attrs = rec.calls[-1]
    assert name == "kirocrew.session.startup.duration"
    return attrs


@pytest.mark.asyncio
async def test_success_outcome_ready():
    client = _client()
    client._spawn = _spawn_ok(client)

    async def _init():
        client._session_id = "sess-ok"

    client._initialize_session = _init
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await client.ensure_ready()
    attrs = _last_outcome(rec)
    assert attrs["outcome"] == "ready"
    assert attrs["spawned"] is True


@pytest.mark.asyncio
async def test_auth_required_outcome():
    client = _client()
    client._spawn = _spawn_ok(client)

    async def _init():
        raise AcpAuthRequired("not logged in")

    client._initialize_session = _init
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        with pytest.raises(AcpAuthRequired):
            await client.ensure_ready()
    assert _last_outcome(rec)["outcome"] == "auth_required"


@pytest.mark.asyncio
async def test_acp_error_outcome_error():
    client = _client()
    client._spawn = _spawn_ok(client)

    async def _init():
        raise AcpError("MCP server crashed")

    def _reset():
        client._process = None
        client._session_id = None

    client._initialize_session = _init
    client._reset_state = _reset
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        with pytest.raises(AcpError):
            await client.ensure_ready()
    assert _last_outcome(rec)["outcome"] == "error"


@pytest.mark.asyncio
async def test_unexpected_exception_not_ready():
    """A non-Acp exception must be recorded as a failure, never 'ready'."""
    client = _client()
    client._spawn = _spawn_ok(client)

    async def _init():
        raise RuntimeError("boom")

    client._initialize_session = _init
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        with pytest.raises(RuntimeError):
            await client.ensure_ready()
    outcome = _last_outcome(rec)["outcome"]
    assert outcome != "ready"
    assert outcome == "error"


async def _cancel_mid_start(start, entered: asyncio.Event) -> None:
    """Run ``start`` as a task, cancel it once it is inside the start, re-raise.

    The cancel lands on a real suspension point, the way Stop, a closed chat or
    a reset during start reaches a start in production.
    """
    task = asyncio.ensure_future(start())
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_cancelled_start_outcome_cancelled():
    """A start cancelled mid-way is a cancel, not a startup failure."""
    client = _client()
    client._spawn = _spawn_ok(client)
    entered = asyncio.Event()

    async def _init():
        entered.set()
        await asyncio.Event().wait()  # unblocked only by the cancel

    client._initialize_session = _init
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _cancel_mid_start(client.ensure_ready, entered)
    assert _last_outcome(rec)["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_during_failed_start_cleanup_keeps_the_failure():
    """A start that already failed stays a failure when its cleanup is cancelled."""
    client = _client()
    client._spawn = _spawn_ok(client)

    async def _init():
        raise AcpToolGateUnroutable("no route")

    client._initialize_session = _init
    client._cleanup_failed_live_spawn = AsyncMock(side_effect=asyncio.CancelledError)
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        with pytest.raises(asyncio.CancelledError):
            await client.ensure_ready()
    assert _last_outcome(rec)["outcome"] == "tool_gate_unroutable"


@pytest.mark.asyncio
async def test_kiro_cancelled_start_outcome_cancelled():
    """The kiro path files a cancelled cold start as ``cancelled`` on every point."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = SimpleNamespace(_session_key="dashboard:b016", backend="")
    entered = asyncio.Event()

    async def _impl(phases, meta):
        phases["spawn_init"] = 12.0
        entered.set()
        await asyncio.Event().wait()  # unblocked only by the cancel

    provider._start_kiro_runtime_impl = _impl
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _cancel_mid_start(provider._start_kiro_runtime, entered)
    assert {a["phase"] for _, a in rec.calls} == {"total", "spawn_init"}
    assert {a["outcome"] for _, a in rec.calls} == {"cancelled"}


@pytest.mark.asyncio
async def test_kiro_cancel_during_failed_start_cleanup_keeps_error():
    """A kiro start cancelled while it handles its own failure stays ``error``."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = SimpleNamespace(_session_key="dashboard:b016", backend="")
    entered = asyncio.Event()

    async def _impl(phases, meta):
        try:
            raise OSError("spawn failed")
        except OSError:
            entered.set()
            await asyncio.Event().wait()  # the failure's cleanup; unblocked by the cancel

    provider._start_kiro_runtime_impl = _impl
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _cancel_mid_start(provider._start_kiro_runtime, entered)
    assert {a["outcome"] for _, a in rec.calls} == {"error"}


class TestKiroStartupAttrs:
    """The kiro (default) backend's emit, driven through production.

    ``_start_kiro_runtime_impl`` always spawns a fresh AcpRuntime, so every point
    from this path is a cold start. The emit must carry a ``spawned`` attribute:
    the Telemetry aggregator splits cold from warm on exactly that key — so
    bool(None) files 100% of real cold starts as warm and cold reads 0ms / 0
    startups permanently.
    """

    def _emit(self, outcome="ready"):
        from kiro_crew.providers.acp import AcpProvider

        rec = _CapturingRecorder()
        provider = AcpProvider.__new__(AcpProvider)  # no spawn, just the emitter
        phases = {"spawn_init": 1400.0, "session_new": 1900.0, "set_model": 1.0}
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
            provider._emit_kiro_startup_metric(0.0, phases, outcome)
        return [attrs for name, attrs in rec.calls]

    def test_every_point_is_marked_spawned(self):
        emitted = self._emit()
        assert len(emitted) == 4  # total + 3 phases
        assert all(a["spawned"] is True for a in emitted)

    def test_exactly_one_end_to_end_point(self):
        assert [a["phase"] for a in self._emit()].count("total") == 1

    def test_phase_names_are_preserved(self):
        assert {a["phase"] for a in self._emit()} == {
            "total",
            "spawn_init",
            "session_new",
            "set_model",
        }

    def test_outcome_propagates_to_every_point(self):
        assert all(a["outcome"] == "auth_required" for a in self._emit("auth_required"))
