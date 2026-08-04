"""Tests for the kirocrew.session.startup.duration histogram contract.

Drives AcpClient.ensure_ready() through each exit path (success / auth_required /
error / unexpected) and asserts the emitted histogram attributes -- in particular
that an unexpected (non-Acp) exception is NOT recorded as a healthy "ready"
outcome (regression guard for the outcome-default fix).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpAuthRequired, AcpClient, AcpError


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


class TestKiroStartupAttrs:
    """The kiro (default) backend's emit, driven through production.

    ``_start_kiro_runtime_impl`` always spawns a fresh AcpRuntime, so every point
    from this path is a cold start. Regression guard: the emit previously carried
    no ``spawned`` attribute, and the Telemetry aggregator splits cold from warm
    on exactly that key — so bool(None) filed 100% of real cold starts as warm
    and cold read 0ms / 0 startups permanently.
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
