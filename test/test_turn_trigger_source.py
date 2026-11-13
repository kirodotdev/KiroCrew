"""Every turn surface states who started the turn, and the exported pair records it.

``kirocrew.turn.completed`` splits turns by ``trigger_source``. A surface that
forgets to pass one records ``other``, which is safe but hides that surface's
turns from every "who started it" reading. So this file holds the call sites to
the rule structurally (an AST walk over ``src/``, the same style as
``test_turn_duration_recorded.py``) and drives the real emit with a capturing
recorder.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.metrics import turn_completed as tc
from kiro_crew.metrics import turns
from kiro_crew.metrics.usage_attrs import ORIGIN_VALUES, TRIGGER_SOURCE_VALUES

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _calls_named(tree: ast.AST, names: tuple[str, ...]) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in names:
                out.append(node)
    return out


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _all_src_calls(names: tuple[str, ...]):
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls_named(tree, names):
            yield path.relative_to(SRC).as_posix(), call


def test_every_emitting_persist_site_passes_a_trigger():
    """A persist call that emits the turn metric must name its trigger.

    A call with ``emit_metric=False`` is exempt: it emits nothing, and its
    caller emits the turn itself (the dashboard does, with its own trigger).
    """
    sites = list(_all_src_calls(("persist_token_record_async",)))
    assert len(sites) >= 12, "the walk found too few persist call sites"
    for rel, call in sites:
        emit = _kw(call, "emit_metric")
        if isinstance(emit, ast.Constant) and emit.value is False:
            continue
        trig = _kw(call, "trigger")
        assert trig is not None, f"{rel}:{call.lineno} persists a turn without trigger="
        if isinstance(trig, ast.Constant):
            assert trig.value in TRIGGER_SOURCE_VALUES, f"{rel}:{call.lineno}"
            assert (
                trig.value != "user"
            ), f"{rel}:{call.lineno}: a background surface must not claim a person"


def test_heartbeat_helper_callers_pass_a_trigger():
    sites = list(_all_src_calls(("_persist_turn_row",)))
    callers = [(rel, c) for rel, c in sites if c.args]  # the def is not a Call
    assert len(callers) == 4
    seen = set()
    for rel, call in callers:
        trig = _kw(call, "trigger")
        assert isinstance(trig, ast.Constant), f"{rel}:{call.lineno}"
        seen.add(trig.value)
    assert seen == {"heartbeat", "monitor"}


class _Rec:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def counter(self, name, value=1, *, unit="1", attrs=None, **kw):
        self.calls.append({"kind": "counter", "name": name, "value": value, "attrs": dict(attrs)})

    def histogram(self, name, value, *, unit="ms", attrs=None, **kw):
        self.calls.append({"kind": "hist", "name": name, "value": value, "attrs": dict(attrs)})


def _emit(**kw):
    rec = _Rec()
    with (
        patch("kiro_crew.metrics.turns.get_recorder", return_value=rec),
        patch("kiro_crew.metrics.turn_completed.get_recorder", return_value=rec),
    ):
        turns.emit_turn_duration(kw.pop("duration_ms", 1500), **kw)
    return rec


def _named(rec, name):
    return [c for c in rec.calls if c["name"] == name]


def test_completed_pair_is_emitted_with_closed_attrs():
    rec = _emit(session_key="cron:job1", outcome="ok", trigger="cron", harness="kiro")
    (count,) = _named(rec, tc.TURN_COMPLETED_METRIC)
    (dur,) = _named(rec, tc.TURN_COMPLETED_DURATION_METRIC)
    assert count["value"] == 1
    assert count["attrs"] == {
        "outcome": "ok",
        "trigger_source": "cron",
        "harness": "kiro",
        "origin": "internal",
    }
    assert dur["value"] == 1500
    assert dur["attrs"] == {"outcome": "ok", "trigger_source": "cron", "harness": "kiro"}
    # The local instrument keeps its own attributes and gains none of these.
    (local,) = _named(rec, turns.TURN_METRIC)
    assert "trigger_source" not in local["attrs"]
    assert "origin" not in local["attrs"]


def test_missing_trigger_is_other_never_user():
    rec = _emit(session_key="chat-1-2", outcome="ok")
    (count,) = _named(rec, tc.TURN_COMPLETED_METRIC)
    assert count["attrs"]["trigger_source"] == "other"
    assert count["attrs"]["origin"] == "dashboard"


def test_open_values_are_closed():
    rec = _emit(
        session_key="anything/at/all",
        outcome="weird",
        trigger="person",
        harness="my-harness",
        origin="https://example.invalid",
    )
    (count,) = _named(rec, tc.TURN_COMPLETED_METRIC)
    assert count["attrs"] == {
        "outcome": "unclassified",
        "trigger_source": "other",
        "harness": "other",
        "origin": "other",
    }


def test_webhook_origin_is_explicit():
    rec = _emit(session_key="chat-1-2", outcome="ok", trigger="webhook", origin="webhook")
    (count,) = _named(rec, tc.TURN_COMPLETED_METRIC)
    assert count["attrs"]["origin"] == "webhook"
    assert count["attrs"]["trigger_source"] == "webhook"


def test_zero_duration_counts_the_turn_but_records_no_duration():
    rec = _emit(duration_ms=0, session_key="_hb", outcome="timeout", trigger="heartbeat")
    assert len(_named(rec, tc.TURN_COMPLETED_METRIC)) == 1
    assert _named(rec, tc.TURN_COMPLETED_DURATION_METRIC) == []
    assert _named(rec, turns.TURN_METRIC) == []


def test_recorder_failure_never_raises():
    class _Boom:
        def counter(self, *a, **k):
            raise RuntimeError("boom")

        histogram = counter

    with (
        patch("kiro_crew.metrics.turns.get_recorder", return_value=_Boom()),
        patch("kiro_crew.metrics.turn_completed.get_recorder", return_value=_Boom()),
    ):
        turns.emit_turn_duration(10, session_key="x", outcome="ok", trigger="cron")


def test_outcome_values_cover_turn_outcome():
    for stop in (
        "",
        "end_turn",
        "cancelled",
        "error: tool stall",
        "stale_recover",
        "x timeout",
        "boom",
    ):
        assert turns.turn_outcome(stop) in tc.TURN_OUTCOME_VALUES
    assert turns.turn_outcome("error: tool stall", exhausted=True) in tc.TURN_OUTCOME_VALUES
    assert turns.OUTCOME_UNCLASSIFIED in tc.TURN_OUTCOME_VALUES
    assert len(tc.TURN_OUTCOME_VALUES) == 8


def test_outcome_values_equal_turn_outcome_returns():
    """Harvested by AST, as the telemetry drift gate does: exact equality."""
    src = pathlib.Path(turns.__file__).read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "turn_outcome"
    )
    returned = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            for c in ast.walk(node.value):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    returned.add(c.value)
    assert returned | {turns.OUTCOME_UNCLASSIFIED} == set(tc.TURN_OUTCOME_VALUES)


def test_completed_duration_uses_the_turn_buckets():
    from kiro_crew.metrics import provider

    bounds = provider.histogram_bounds()
    assert bounds[tc.TURN_COMPLETED_DURATION_METRIC] == bounds[turns.TURN_METRIC]


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"_crew_log_actor": "user", "_directive_user_origin": True}, "user"),
        ({"_crew_log_actor": "user", "_directive_channel_origin": True}, "user"),
        ({"_crew_log_actor": "user"}, "other"),
        (
            {
                "_crew_log_actor": "user",
                "_directive_user_origin": True,
                "_turn_provenance_restored": True,
            },
            "other",
        ),
        ({"_crew_log_actor": "autonudge"}, "monitor"),
        ({"_crew_log_actor": "crew"}, "agent"),
        ({"_crew_log_actor": "app"}, "app"),
        ({"_crew_log_actor": "gateway"}, "system"),
        ({}, "other"),
    ],
)
def test_dashboard_turn_trigger(fields, expected):
    from kiro_crew.dashboard.chat_turn.turn_stats import _dashboard_turn_trigger

    assert _dashboard_turn_trigger(SimpleNamespace(**fields)) == expected


def test_origin_values_bound():
    assert len(ORIGIN_VALUES) == 17
