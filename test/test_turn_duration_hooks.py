"""Turn wall-clock (``elapsed_ms``) recorded by the webhook hooks surface.

The acp provider never assigns ``TurnUsage.duration_ms`` (it stays 0), so
``_run_hook_inner`` measures the turn locally and passes ``elapsed_ms`` to
``persist_token_record_async``, which records ``duration_ms`` = the provider
value when non-zero ELSE ``elapsed_ms``.

Both tests drive the real persist path and capture the built record at
``_write_token_record``, so the fallback (local clock fills a 0-duration turn)
and its precedence (a provider-reported duration still wins) are exercised end
to end — the negative control is what keeps the positive assertion honest.
"""

from __future__ import annotations

import asyncio

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, TurnUsage
from kiro_crew.dashboard.handlers import usage
from kiro_crew.dashboard.handlers.hooks import _run_hook_inner

# A provider-reported duration no sub-second test turn could ever produce
# (~16 min). A record showing it proves the provider value won over the local
# wall clock; a record NOT showing it in the 0-duration case proves the local
# clock is genuinely the fallback source.
_PROVIDER_SENTINEL_MS = 987_654

# Long enough that int((monotonic delta) * 1000) is reliably > 0 (a sub-ms turn
# would floor to 0), short enough to keep the test fast. asyncio.sleep ensures at
# least this much wall time elapses inside the streamed turn.
_TURN_DELAY_S = 0.02


class _FakeClient:
    """Minimal stand-in for the ACP client ``_run_hook_inner`` streams from."""

    def __init__(self, complete_event: AcpEvent) -> None:
        self._complete_event = complete_event
        # read_effective_agent / _resolve_model walk the wrapper chain for these.
        self._agent = "kirocrew"
        self._model = "claude-test"

    async def stream(self, _message: str):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="hello ")
        # A real turn takes time; sleeping makes the measured wall clock non-zero.
        await asyncio.sleep(_TURN_DELAY_S)
        yield self._complete_event


class _FakeSessions:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    async def get_or_create(self, _session_key: str, agent: str | None = None, **_kw):
        # is_new=False so _run_hook_inner skips context building (no embed pool).
        return (self._client, False, False)

    def record_success(self, _session_key: str) -> None:
        pass


class _FakeState:
    def __init__(self, client: _FakeClient) -> None:
        self.sessions = _FakeSessions(client)
        self.context_builder = None


def _drive(monkeypatch, complete_event: AcpEvent) -> dict:
    """Run one webhook turn, returning the record handed to the writer.

    ``persist_token_record_async`` builds the record on-loop (applying the
    duration-or-elapsed precedence) then offloads the write to
    ``_write_token_record`` — intercept there to read the final record without
    touching a shard.
    """
    captured: dict = {}

    def _capture(record: dict, _now: object) -> None:
        captured["record"] = record

    monkeypatch.setattr(usage, "_write_token_record", _capture)
    asyncio.run(
        _run_hook_inner(_FakeState(_FakeClient(complete_event)), "hook:test:1", "go", None)
    )
    return captured["record"]


def test_webhook_turn_records_local_wall_clock(monkeypatch):
    """acp reports duration_ms=0 -> the row falls back to the local turn clock."""
    complete = AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=0, credits=1.0))
    record = _drive(monkeypatch, complete)

    assert record["surface"] == "webhook"
    assert record["duration_ms"] > 0, "local wall clock must fill a 0-duration turn"


def test_provider_duration_wins_over_local_clock(monkeypatch):
    """Negative control: a provider-reported duration is NOT overwritten by the
    local clock, so the positive test above cannot pass vacuously."""
    complete = AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=_PROVIDER_SENTINEL_MS))
    record = _drive(monkeypatch, complete)

    assert record["duration_ms"] == _PROVIDER_SENTINEL_MS
