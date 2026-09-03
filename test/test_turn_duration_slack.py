"""Turn wall clock + timeout-spend recording on the Slack dispatch surface.

``gateway.py`` now measures a local ``time.monotonic()`` wall clock for each
background dispatch turn and passes it as ``elapsed_ms`` to
``persist_token_record_async``. The acp provider never assigns
``TurnUsage.duration_ms`` (it stays 0), so without this the row store recorded
``duration_ms=0`` for every real turn; the record builder now falls back to the
caller's ``elapsed_ms`` when the provider reports nothing (issue #647 / #874
follow-up).

The two ``asyncio.wait_for`` timeout branches (slack heartbeat and the monitor
auto-nudge) previously wrote NO row at all on timeout, silently dropping the
spend the cancelled turn had already incurred. They now record it.

These tests drive the monitor path (``GatewayOrchestrator._fire_slack_nudge``)
end to end because it is a directly-callable method. Its timeout branch has the
same shape as the heartbeat one -- read the provider's accumulated usage off the
still-alive client, then persist with the measured elapsed -- and both go
through the identical ``persist_token_record_async(..., elapsed_ms=...)`` seam.
The heartbeat runner is a closure nested inside ``_init_heartbeat`` and is not
callable in isolation, so it is covered by code review + this shared-shape test
rather than driven directly here.

Assertions read the real usage shard written under the per-test isolated
``KIROCREW_HOME`` (the autouse ``_isolate_kirocrew_home`` conftest fixture), so
they exercise the full chain gateway -> persist -> ``_build_token_record``
precedence, not a mock of it.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN, TurnUsage
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.usage import _token_usage_dir
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorState,
)
from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
from kiro_crew.slack import gateway as gw


class _StepClock:
    """Deterministic stand-in for the ``time`` module in ``gateway.py``.

    ``gateway.py`` reads ``time.monotonic()`` once to open the turn
    (``_turn_t0``) and once more when it builds the persist kwargs. The first
    read returns ``start`` and every later read returns ``later``, so the
    recorded elapsed is exactly ``(later - start) * 1000`` ms regardless of how
    many reads happen -- decoupling the asserted duration from real wall time.
    Any other attribute (e.g. ``time.time``) proxies to the real module so an
    unrelated call in the exercised path still behaves.
    """

    def __init__(self, start: float, later: float) -> None:
        self._start = start
        self._later = later
        self._opened = False

    def monotonic(self) -> float:
        if not self._opened:
            self._opened = True
            return self._start
        return self._later

    def __getattr__(self, name: str):
        return getattr(time, name)


def _fake_client() -> SimpleNamespace:
    """A minimal provider/client stub for the usage readers.

    ``read_context_tokens`` calls the two accessors; ``read_effective_agent``
    walks the wrapper chain and picks up ``_agent``. ``provider_last_turn_usage``
    is patched per test, so nothing here needs a ``last_prompt_stats``.
    """
    return SimpleNamespace(
        context_used_tokens=lambda: 4096,
        context_window_tokens=lambda: 200000,
        _agent="kirocrew-monitor",
    )


def _fake_sessions(client: object) -> MagicMock:
    s = MagicMock()
    s.is_busy = MagicMock(return_value=False)
    s.get_channel = MagicMock(return_value="C123")
    s.get_thread = MagicMock(return_value="111.222")
    s.get_or_create = AsyncMock(return_value=(client, True, False))
    s.cancel_current = AsyncMock()
    s.release = MagicMock()
    return s


def _build_orchestrator(client: object) -> gw.GatewayOrchestrator:
    """Construct a real orchestrator, then swap in the collaborators the
    monitor nudge path touches."""
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        orch = gw.GatewayOrchestrator(
            cfg, no_dashboard=True, no_crons=True, no_open=True
        )
    orch.sessions = _fake_sessions(client)
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock()
    orch.ctx_builder = SimpleNamespace(
        hooks=object(), build_message=lambda *a, **k: ("MSG", None)
    )
    orch.conv_log = None
    orch.autonudge_svc = None

    # The approval callback is never invoked (stream_and_collect is stubbed);
    # replace it with a harmless async approver so building it can't reach into
    # unset dashboard state.
    async def _approve(_event: object) -> bool:
        return True

    # Accepts the production signature's optional keywords (e.g. the monitor
    # loop's binding key) so this double does not pin the real method's arity.
    orch._interactive_approval = lambda _source, **_kw: _approve
    return orch


def _monitor_rows() -> list[dict]:
    """All ``surface == "monitor"`` records in the isolated usage shard(s)."""
    rows: list[dict] = []
    shard_dir = _token_usage_dir()
    if not shard_dir.exists():
        return rows
    for shard in shard_dir.glob("*.jsonl"):
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("surface") == "monitor":
                rows.append(rec)
    return rows


def test_monitor_turn_records_local_wall_clock(monkeypatch):
    """Normal path: acp reports no duration, so the row records the gateway's
    local wall clock -- a non-zero ``duration_ms``."""
    client = _fake_client()
    orch = _build_orchestrator(client)

    monkeypatch.setattr(gw, "time", _StepClock(1000.0, 1005.0))  # 5000 ms
    monkeypatch.setattr(
        gw, "provider_last_turn_usage", lambda _c: TurnUsage(credits=0.42)
    )

    async def _stream_ok(*_a, **_k):
        return "hello from the nudge turn"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

    loop = NudgeLoop(id="loop-normal", slot_key="slack:111.222", message="check it")
    result = asyncio.run(orch._fire_slack_nudge(loop))

    assert result is True
    rows = _monitor_rows()
    assert len(rows) == 1
    rec = rows[0]
    assert rec["duration_ms"] == 5000
    assert rec["duration_ms"] > 0
    assert rec["credits"] == pytest.approx(0.42)


def test_monitor_timeout_records_previously_dropped_row(monkeypatch):
    """Timeout path: the turn is cancelled by ``wait_for``, but the spend it had
    already incurred is now recorded (previously the row was never written)."""
    client = _fake_client()
    orch = _build_orchestrator(client)

    monkeypatch.setattr(gw, "time", _StepClock(1000.0, 1000.5))  # 500 ms
    monkeypatch.setattr(
        gw, "provider_last_turn_usage", lambda _c: TurnUsage(credits=0.17)
    )
    monkeypatch.setattr(gw, "_NUDGE_TURN_TIMEOUT", 0.05)

    async def _stream_hang(*_a, **_k):
        await asyncio.sleep(1.0)
        return "never returned"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_hang)

    loop = NudgeLoop(id="loop-timeout", slot_key="slack:111.222", message="slow")
    result = asyncio.run(orch._fire_slack_nudge(loop))

    assert result is False  # the timeout branch bails after recording
    rows = _monitor_rows()
    assert len(rows) == 1  # previously ZERO rows were written on timeout
    assert rows[0]["credits"] == pytest.approx(0.17)
    assert rows[0]["duration_ms"] == 500


def test_provider_duration_wins_over_local_clock(monkeypatch):
    """Negative control: when the provider DOES report a duration it wins over
    the local clock, so the normal-path assertion above is not vacuous."""
    client = _fake_client()
    orch = _build_orchestrator(client)

    # The local clock would record 1234 ms ...
    monkeypatch.setattr(gw, "time", _StepClock(1000.0, 1001.234))
    # ... but a provider-reported 5000 ms duration must win.
    monkeypatch.setattr(
        gw,
        "provider_last_turn_usage",
        lambda _c: TurnUsage(credits=0.42, duration_ms=5000),
    )

    async def _stream_ok(*_a, **_k):
        return "done"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

    loop = NudgeLoop(id="loop-neg", slot_key="slack:111.222", message="fast")
    result = asyncio.run(orch._fire_slack_nudge(loop))

    assert result is True
    rows = _monitor_rows()
    assert len(rows) == 1
    assert rows[0]["duration_ms"] == 5000  # provider wins
    assert rows[0]["duration_ms"] != 1234  # not the local-clock fallback


@pytest.mark.parametrize(
    ("stop_reason", "expected_disposition"),
    [
        (STOP_REASON_END_TURN, None),
        (STOP_REASON_CANCELLED, MonitorActionDisposition.CANCELLATION),
        ("max_tokens", MonitorActionDisposition.FAILURE),
    ],
)
def test_structured_monitor_fans_out_usage_only_from_raw_completion(
    monkeypatch,
    stop_reason: str,
    expected_disposition: MonitorActionDisposition | None,
):
    """One destructive usage read serves telemetry and the evidenced disposition."""
    client = _fake_client()
    orch = _build_orchestrator(client)
    usage = TurnUsage(input_tokens=40, output_tokens=10, credits=0.25)
    reads = 0

    def _usage_once(_client):
        nonlocal reads
        reads += 1
        return usage

    completions: list[MonitorActionCompletion] = []

    async def _capture(completion: MonitorActionCompletion) -> None:
        completions.append(completion)

    orch.autonudge_svc = SimpleNamespace(record_monitor_turn_completion=_capture)
    monkeypatch.setattr(gw, "provider_last_turn_usage", _usage_once)

    async def _stream_ok(*_a, on_complete=None, **_k):
        assert on_complete is not None
        on_complete(
            LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=stop_reason,
                synthetic_completion=stop_reason == STOP_REASON_END_TURN,
            )
        )
        return "done"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)
    loop = NudgeLoop(
        id="loop-structured",
        slot_key="slack:111.222",
        message="check it",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        ),
    )

    assert asyncio.run(orch._fire_slack_nudge(loop)) is True

    assert reads == 1
    if expected_disposition is None:
        assert completions == []
    else:
        assert len(completions) == 1
        assert completions[0].disposition is expected_disposition
        assert completions[0].input_tokens == 40
        assert completions[0].output_tokens == 10
    rows = _monitor_rows()
    assert len(rows) == 1
    assert rows[0]["input"] == 40
    assert rows[0]["output"] == 10


def test_structured_monitor_stream_exhaustion_charges_no_completion(monkeypatch):
    """A bare string return without EVENT_COMPLETE is not completion evidence."""
    client = _fake_client()
    orch = _build_orchestrator(client)
    completions: list[MonitorActionCompletion] = []

    async def _capture(completion: MonitorActionCompletion) -> None:
        completions.append(completion)

    orch.autonudge_svc = SimpleNamespace(record_monitor_turn_completion=_capture)
    monkeypatch.setattr(
        gw,
        "provider_last_turn_usage",
        lambda _client: TurnUsage(input_tokens=40, output_tokens=10),
    )

    async def _stream_exhausted(*_a, **_k):
        return "partial"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_exhausted)
    loop = NudgeLoop(
        id="loop-exhausted",
        slot_key="slack:111.222",
        message="check it",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        ),
    )

    assert asyncio.run(orch._fire_slack_nudge(loop)) is True
    assert completions == []
    assert len(_monitor_rows()) == 1


def test_structured_monitor_synthetic_timeout_charges_no_completion(monkeypatch):
    """A synthetic timeout event is not provider completion evidence."""
    client = _fake_client()
    orch = _build_orchestrator(client)
    completions: list[MonitorActionCompletion] = []

    async def _capture(completion: MonitorActionCompletion) -> None:
        completions.append(completion)

    orch.autonudge_svc = SimpleNamespace(record_monitor_turn_completion=_capture)
    monkeypatch.setattr(
        gw,
        "provider_last_turn_usage",
        lambda _client: TurnUsage(input_tokens=40, output_tokens=10),
    )

    async def _stream_timeout(*_a, on_complete=None, **_k):
        assert on_complete is not None
        on_complete(LLMEvent(kind=EVENT_COMPLETE, stop_reason="timeout"))
        return "partial"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_timeout)
    loop = NudgeLoop(
        id="loop-synthetic-timeout",
        slot_key="slack:111.222",
        message="check it",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        ),
    )

    assert asyncio.run(orch._fire_slack_nudge(loop)) is True
    assert completions == []
    assert len(_monitor_rows()) == 1


def test_structured_monitor_timeout_before_complete_charges_no_completion(monkeypatch):
    """Transport cancellation cannot stand in for a raw cancelled completion."""
    client = _fake_client()
    orch = _build_orchestrator(client)
    completions: list[MonitorActionCompletion] = []

    async def _capture(completion: MonitorActionCompletion) -> None:
        completions.append(completion)

    orch.autonudge_svc = SimpleNamespace(record_monitor_turn_completion=_capture)
    monkeypatch.setattr(gw, "provider_last_turn_usage", lambda _client: TurnUsage(credits=0.2))
    monkeypatch.setattr(gw, "_NUDGE_TURN_TIMEOUT", 0.01)

    async def _stream_hang(*_a, **_k):
        await asyncio.sleep(1.0)
        return "unreachable"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_hang)
    loop = NudgeLoop(
        id="loop-timeout-structured",
        slot_key="slack:111.222",
        message="check it",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        ),
    )

    assert asyncio.run(orch._fire_slack_nudge(loop)) is False
    assert completions == []
    assert len(_monitor_rows()) == 1


def test_structured_monitor_timeout_after_raw_completion_preserves_event(monkeypatch):
    """A timeout does not replace raw cancellation evidence with inference."""
    client = _fake_client()
    orch = _build_orchestrator(client)
    completions: list[MonitorActionCompletion] = []

    async def _capture(completion: MonitorActionCompletion) -> None:
        completions.append(completion)

    orch.autonudge_svc = SimpleNamespace(record_monitor_turn_completion=_capture)
    monkeypatch.setattr(gw, "provider_last_turn_usage", lambda _client: TurnUsage(credits=0.2))
    monkeypatch.setattr(gw, "_NUDGE_TURN_TIMEOUT", 0.01)

    async def _stream_complete_then_hang(*_a, on_complete=None, **_k):
        assert on_complete is not None
        on_complete(LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED))
        await asyncio.sleep(1.0)
        return "unreachable"

    monkeypatch.setattr(gw, "stream_and_collect", _stream_complete_then_hang)
    loop = NudgeLoop(
        id="loop-timeout-after-complete",
        slot_key="slack:111.222",
        message="check it",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        ),
    )

    assert asyncio.run(orch._fire_slack_nudge(loop)) is False
    assert len(completions) == 1
    assert completions[0].disposition is MonitorActionDisposition.CANCELLATION
    assert len(_monitor_rows()) == 1
