"""``kirocrew.app.credits``: a finished turn's credits, added under the app that owns its session.

Driven through ``emit_turn_usage`` and both of its turn callers
(``_emit_turn_metric`` for the dashboard, ``persist_token_record_async`` for every
other surface), with the recorder patched, so the gate and the "adds nothing"
cases are the production code's.
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


def _patched(rec):
    return (
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
    )


def _run(credits, *, owning_app="weather-app", rec=None, **usage):
    rec = rec or _Rec()
    p1, p2 = _patched(rec)
    with p1, p2:
        turns.emit_turn_usage(credits=credits, owning_app=owning_app, **usage)
    return rec


def _credits(rec):
    return [c for c in rec.counters if c["name"] == fu.APP_CREDITS]


def test_credits_are_added_with_the_apps_entity_and_version(public):
    (point,) = _credits(_run(1.25))
    assert point["value"] == 1.25
    assert point["unit"] == "credit"
    assert point["attrs"] == {"app": "weather-app", "entity_version": "2.1.0"}


def test_a_private_app_is_counted_without_its_name_or_version(public):
    (point,) = _credits(_run(0.5, owning_app="my-own-app"))
    assert point["attrs"] == {"app": "private", "entity_version": "private"}


@pytest.mark.parametrize("credits", [0, 0.0, -1.0, None, "x", float("nan"), float("inf")])
def test_a_turn_with_no_credits_adds_nothing(public, credits):
    assert _credits(_run(credits, cost_usd=0.02, input_tokens=10)) == []


@pytest.mark.parametrize("owning_app", ["", None, MagicMock()])
def test_a_turn_outside_an_app_adds_nothing(public, owning_app):
    assert _credits(_run(1.0, owning_app=owning_app)) == []


def test_the_recorder_gate_refuses_what_the_turn_gate_would(public):
    rec = _Rec()
    p1, p2 = _patched(rec)
    with p1, p2:
        for value in (0, -1, True, "1", None, float("nan"), float("inf")):
            fu.record_app_credits("weather-app", value)
        fu.record_app_credits("", 1.0)
    assert _credits(rec) == []


def test_a_raising_recorder_leaves_the_turn_metrics_unchanged(public):
    usage = {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}
    plain = _run(1.5, owning_app="", **usage)
    raising = _run(1.5, rec=_Rec(raise_on=fu.APP_CREDITS), **usage)
    assert raising.hist == plain.hist
    assert raising.counters == plain.counters
    assert any(h["name"] == turns.TURN_CREDITS_METRIC for h in raising.hist)


# -- both turn callers pass the live provider's owning_app ---------------------


def test_the_dashboard_caller_passes_owning_app(public):
    from kiro_crew.dashboard import chat_runner

    rec = _Rec()
    p1, p2 = _patched(rec)
    with p1, p2:
        chat_runner._emit_turn_metric(
            4_000,
            "end_turn",
            "chat-1-1",
            usage=SimpleNamespace(credits=0.75),
            owning_app="weather-app",
        )
    assert [(p["value"], p["attrs"]["app"]) for p in _credits(rec)] == [(0.75, "weather-app")]


class _AppProvider(ScriptedProvider):
    """The scripted session, allocated for an app slot."""

    owning_app = "weather-app"


@pytest.mark.asyncio
async def test_a_dashboard_turn_reads_owning_app_off_its_client(public):
    """A real dashboard turn on an app's provider adds its credits once."""
    rec = _Rec()
    landed = AcpEvent(
        kind=EVENT_COMPLETE,
        stop_reason=STOP_REASON_END_TURN,
        usage=TurnUsage(duration_ms=4_200, credits=0.42),
    )
    events = [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"), landed]
    p1, p2 = _patched(rec)
    with p1, p2:
        await run_turn(TurnScript(events=events, provider=_AppProvider))
    assert [(p["value"], p["attrs"]["app"]) for p in _credits(rec)] == [(0.42, "weather-app")]


@pytest.mark.asyncio
async def test_a_dashboard_turn_outside_an_app_adds_nothing(public):
    rec = _Rec()
    landed = AcpEvent(
        kind=EVENT_COMPLETE,
        stop_reason=STOP_REASON_END_TURN,
        usage=TurnUsage(duration_ms=4_200, credits=0.42),
    )
    events = [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"), landed]
    p1, p2 = _patched(rec)
    with p1, p2:
        await run_turn(TurnScript(events=events))
    assert _credits(rec) == []
    assert any(h["name"] == turns.TURN_CREDITS_METRIC for h in rec.hist)


def test_the_shared_boundary_reads_owning_app_off_model_source(public, monkeypatch):
    from kiro_crew.dashboard.handlers import usage

    rec = _Rec()
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: None)
    event = SimpleNamespace(duration_ms=0, stop_reason=None, credits=0.3)
    p1, p2 = _patched(rec)
    with p1, p2:
        for source in (SimpleNamespace(owning_app="weather-app"), SimpleNamespace(), None):
            asyncio.run(
                usage.persist_token_record_async(
                    "cron:job-1", "", event, "acp", elapsed_ms=1_200, model_source=source
                )
            )
    assert [(p["value"], p["attrs"]["app"]) for p in _credits(rec)] == [(0.3, "weather-app")]


def test_a_provider_without_owning_app_falls_back_to_the_rows_app(public, monkeypatch):
    """A shared-session app subagent: its provider has no ``owning_app``, its row has ``app``."""
    from kiro_crew.dashboard.handlers import usage

    rec = _Rec()
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: None)
    event = SimpleNamespace(duration_ms=0, stop_reason=None, credits=0.3)
    p1, p2 = _patched(rec)
    with p1, p2:
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
    assert [p["attrs"]["app"] for p in _credits(rec)] == ["weather-app"]
