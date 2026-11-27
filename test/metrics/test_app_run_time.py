"""``kirocrew.app.run_time``: a finished turn's seconds, added under the app that owns its session.

Driven through ``emit_turn_duration`` and both of its turn callers
(``_emit_turn_metric`` for the dashboard, ``persist_token_record_async`` for every
other surface), with the recorder patched, so the rounding, the bucket edges and
the "adds nothing" cases are the production code's.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.metrics import feature_usage as fu
from kiro_crew.metrics import turns


class _Rec:
    enabled = True

    def __init__(self, raise_on: str = "") -> None:
        self.raise_on = raise_on
        self.counters: list[dict] = []
        self.hist: list[dict] = []

    def counter(self, name, value=1, *, unit="1", attrs=None, **kwargs) -> None:
        if name == self.raise_on:
            raise RuntimeError("recorder down")
        self.counters.append(
            {"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})}
        )

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.hist.append({"name": name, "value": value, "attrs": dict(attrs or {})})


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setattr(fu, "_public_app_ids", {"weather-app": "2.1.0"})


def _run(duration_ms, *, owning_app="weather-app", elapsed_ms=None, rec=None):
    rec = rec or _Rec()
    with (
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
    ):
        turns.emit_turn_duration(
            duration_ms,
            session_key="chat-1-1",
            outcome="ok",
            elapsed_ms=elapsed_ms,
            owning_app=owning_app,
        )
    return rec


def _run_time(rec):
    return [c for c in rec.counters if c["name"] == fu.APP_RUN_TIME]


@pytest.mark.parametrize(
    "duration_ms,seconds,bucket",
    [
        (1, 1, "le_10s"),
        (10_000, 10, "le_10s"),
        (10_001, 11, "le_1m"),
        (11_000, 11, "le_1m"),
        (14_400_000, 14_400, "le_4h"),
        (14_401_000, 14_401, "gt_4h"),
    ],
)
def test_seconds_are_rounded_up_and_bucketed_at_the_edges(public, duration_ms, seconds, bucket):
    (point,) = _run_time(_run(duration_ms))
    assert point["value"] == seconds
    assert point["unit"] == "s"
    assert point["attrs"] == {"app": "weather-app", "entity_version": "2.1.0", "duration": bucket}


def test_the_wall_clock_is_used_when_the_provider_reports_zero(public):
    (point,) = _run_time(_run(0, elapsed_ms=2_500))
    assert point["value"] == 3


def test_a_private_app_is_counted_without_its_name_or_version(public):
    (point,) = _run_time(_run(5_000, owning_app="my-own-app"))
    assert point["attrs"]["app"] == "private"
    assert point["attrs"]["entity_version"] == "private"


@pytest.mark.parametrize("duration_ms,elapsed_ms", [(0, 0), (None, None), (0, None)])
def test_a_turn_with_no_duration_adds_nothing(public, duration_ms, elapsed_ms):
    assert _run_time(_run(duration_ms, elapsed_ms=elapsed_ms)) == []


@pytest.mark.parametrize("owning_app", ["", None, MagicMock()])
def test_a_turn_outside_an_app_adds_nothing(public, owning_app):
    assert _run_time(_run(5_000, owning_app=owning_app)) == []


def test_a_raising_recorder_leaves_the_turn_metric_unchanged(public):
    plain = _run(5_000, owning_app="")
    raising = _run(5_000, rec=_Rec(raise_on=fu.APP_RUN_TIME))
    assert raising.hist == plain.hist
    assert raising.counters == plain.counters
    assert any(h["name"] == turns.TURN_METRIC for h in raising.hist)


def test_the_bucket_of_no_number_is_other():
    for value in (0, -1, None, "10", True, float("nan"), float("inf")):
        assert fu.app_duration_bucket(value) == "other"


# -- both turn callers pass the live provider's owning_app ---------------------


def test_the_dashboard_caller_passes_owning_app(public):
    from kiro_crew.dashboard import chat_runner

    rec = _Rec()
    with (
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
    ):
        chat_runner._emit_turn_metric(4_000, "end_turn", "chat-1-1", owning_app="weather-app")
    (point,) = _run_time(rec)
    assert point["attrs"]["app"] == "weather-app"


class _AppProvider(ScriptedProvider):
    """The scripted session, allocated for an app slot."""

    owning_app = "weather-app"


@pytest.mark.asyncio
async def test_a_dashboard_turn_reads_owning_app_off_its_client(public):
    """A real dashboard turn on an app's provider adds its seconds once."""
    rec = _Rec()
    landed = AcpEvent(
        kind=EVENT_COMPLETE,
        stop_reason=STOP_REASON_END_TURN,
        usage=TurnUsage(duration_ms=4_200),
    )
    events = [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"), landed]
    with (
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
    ):
        await run_turn(TurnScript(events=events, provider=_AppProvider))
    assert [(p["value"], p["attrs"]["app"]) for p in _run_time(rec)] == [(5, "weather-app")]


def test_the_shared_boundary_reads_owning_app_off_model_source(public, monkeypatch):
    from kiro_crew.dashboard.handlers import usage

    rec = _Rec()
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: None)
    event = SimpleNamespace(duration_ms=0, stop_reason=None)
    with (
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
    ):
        for source in (SimpleNamespace(owning_app="weather-app"), SimpleNamespace(), None):
            asyncio.run(
                usage.persist_token_record_async(
                    "cron:job-1", "", event, "acp", elapsed_ms=1_200, model_source=source
                )
            )
    assert [p["attrs"]["app"] for p in _run_time(rec)] == ["weather-app"]
    assert [p["value"] for p in _run_time(rec)] == [2]


def test_a_provider_without_owning_app_falls_back_to_the_rows_app(public, monkeypatch):
    """A shared-session app subagent: its provider has no ``owning_app``, its row has ``app``."""
    from kiro_crew.dashboard.handlers import usage

    rec = _Rec()
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: None)
    event = SimpleNamespace(duration_ms=0, stop_reason=None)
    with (
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
    ):
        asyncio.run(
            usage.persist_token_record_async(
                "subagent:abc",
                "",
                event,
                "acp",
                surface="subagent",
                app="weather-app",
                elapsed_ms=1_200,
                model_source=SimpleNamespace(owning_app=""),
            )
        )
    assert [p["attrs"]["app"] for p in _run_time(rec)] == ["weather-app"]
