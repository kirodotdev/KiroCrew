"""``kirocrew.turn.duration`` is emitted for EVERY dispatch surface.

The metric powers turn latency (p50/p90) and fault rate on the Telemetry page.
It used to be emitted from one call inside ``chat_runner._run_chat``, which made
it structurally unreachable from cron, the heartbeat, memory consolidation,
subagents, task-runner steps, workflow stages and every messaging channel: those
surfaces produced no sample at all, so a slow or erroring background turn read as
healthy rather than as missing data.

What these pin, in the order that matters:

1. The emit now happens at the shared per-turn boundary
   (``persist_token_record_async``), so a surface gets its sample by making the
   call it already made — no per-surface wiring to forget.
2. The sample's ``session_source`` distinguishes the surfaces, which is the whole
   point of widening the population: one anonymous bucket would answer "how many
   background turns ran" no better than silence.
3. The dashboard's existing series is NOT re-labelled by the move.
4. The outcome is never guessed as success — the failure mode that would bury
   exactly the turns this widening exists to expose.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.messaging.link import TELEMETRY_CHANNELS, telemetry_channel_of
from kiro_crew.metrics import turns as turns_mod

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


class _Recorder:
    """Captures histogram calls instead of exporting them."""

    def __init__(self) -> None:
        self.samples: list[tuple[str, float, dict]] = []

    def histogram(self, name, value, unit=None, attrs=None):  # noqa: D102
        self.samples.append((name, value, dict(attrs or {})))


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(turns_mod, "get_recorder", lambda: rec)
    return rec


class _Event:
    """An EVENT_COMPLETE-shaped object: what the chat and webhook paths pass."""

    def __init__(self, stop_reason: str = "end_turn") -> None:
        self.stop_reason = stop_reason


class _BareUsage:
    """A bare TurnUsage-shaped object: what the helper call sites pass.

    Deliberately carries NO ``stop_reason`` — that asymmetry between call sites
    is why the outcome cannot simply be read off the event everywhere.
    """


# --- 1. the boundary emits, for every surface's key -------------------------


@pytest.mark.parametrize(
    "slot_key,expected_source",
    [
        ("chat-12-1785445181", "dashboard"),
        ("cron:job-7", "cron"),
        ("subagent:abc123", "subagent"),
        ("taskrunner_run1:task2", "taskrunner"),
        ("_hb", "heartbeat"),
        ("_bg", "background"),
        ("wf:run9:1", "workflow"),
        ("slack:1712793600.123456", "slack"),
        ("telegram:555", "telegram"),
        ("discord:99", "discord"),
    ],
)
def test_every_surface_lands_in_the_histogram_under_its_own_source(
    recorder, slot_key, expected_source
) -> None:
    """The bug, in one assertion per surface.

    Before this, only the first row existed: every other surface emitted nothing,
    so the page's p50 was the interactive median wearing the whole system's name.
    """
    usage_mod._emit_turn_histogram({"duration_ms": 1234}, slot_key, _Event())

    assert len(recorder.samples) == 1, f"{slot_key} produced no sample"
    name, value, attrs = recorder.samples[0]
    assert name == "kirocrew.turn.duration"
    assert value == 1234
    assert attrs["session_source"] == expected_source
    assert attrs["outcome"] == "ok"


def test_the_dashboard_series_is_not_relabelled_by_the_move(recorder) -> None:
    """A real chat-slot key must still read ``dashboard``.

    The move swapped the source function to ``telemetry_channel_of`` (which knows
    the background surfaces) from ``infer_use_case`` (which did not). If a
    dashboard key had come out as anything else, the existing turn-latency series
    would have silently split in two at deploy — a break dressed as a widening.
    """
    usage_mod._emit_turn_histogram({"duration_ms": 500}, "chat-12-1785445181", _Event())
    assert recorder.samples[0][2]["session_source"] == "dashboard"


def test_source_labels_stay_inside_the_closed_set(recorder) -> None:
    """Cardinality guard: an unbounded label mints a series per conversation."""
    for key in ("chat-12-1785445181", "cron:job-7", "_hb", "wf:run9:1", "nonsense-key", ""):
        recorder.samples.clear()
        usage_mod._emit_turn_histogram({"duration_ms": 5}, key, _Event())
        assert recorder.samples[0][2]["session_source"] in TELEMETRY_CHANNELS


# --- 2. outcome is stated, never assumed ------------------------------------


def test_the_stall_refinement_travels_on_the_dashboard_emit(recorder) -> None:
    """The dashboard knows more than its stop reason does.

    Its spent stall-recovery budget makes the turn a terminal fault
    (``stall_exhausted``); a stop reason alone would have said ``stale_recover``,
    which fault_rate deliberately excludes — so the exclusion would have hidden a
    dead session.
    """
    usage_mod._emit_turn_histogram(
        {"duration_ms": 9}, "chat-12-1785445181", _Event("stale_recover")
    )
    assert recorder.samples[0][2]["outcome"] == "stale_recover"
    recorder.samples.clear()
    # The refinement now travels on the dashboard's OWN emit, which is the only
    # surface that maintains a recovery budget — and the only one that must emit
    # outside the usage_has_billing gate anyway. An override parameter on the
    # shared boundary was dead code: its only passer also disabled the branch
    # that read it.
    from kiro_crew.dashboard import chat_runner

    chat_runner._emit_turn_metric(
        9, "stale_recover", "chat-12-1785445181", elapsed_ms=9, exhausted=True
    )
    assert recorder.samples[0][2]["outcome"] == "stall_exhausted"


def test_a_stop_reason_bearing_event_is_classified(recorder) -> None:
    usage_mod._emit_turn_histogram({"duration_ms": 9}, "cron:j", _Event("some_timeout_thing"))
    assert recorder.samples[0][2]["outcome"] == "timeout"


def test_a_bare_usage_object_is_unclassified_and_not_a_fault(recorder) -> None:
    """The honest label for "this surface could not say" — and it must not be a fault.

    ``unknown`` was the first choice and was wrong: it is in
    ``telemetry._TERMINAL_FAULT_OUTCOMES`` (the aggregator mints it for
    attribute-less points), so every CLEAN cron/background/workflow turn would
    have landed in the fault-rate numerator the moment this metric started
    sampling them — inventing a fault spike out of a visibility fix. ``ok`` is
    equally wrong in the other direction: it claims a success nobody observed.
    """
    from kiro_crew.dashboard.handlers.telemetry import _TERMINAL_FAULT_OUTCOMES

    usage_mod._emit_turn_histogram({"duration_ms": 9}, "_bg", _BareUsage())
    outcome = recorder.samples[0][2]["outcome"]
    assert outcome == turns_mod.OUTCOME_UNCLASSIFIED
    assert outcome not in _TERMINAL_FAULT_OUTCOMES, (
        "an undeterminable outcome counted as a fault turns every clean "
        "background turn into a fault-rate data point"
    )


def test_an_empty_stop_reason_is_still_ok(recorder) -> None:
    """The distinction the label rests on: absent != empty.

    A clean ``LLMEvent`` turn can report ``""``; only a MISSING attribute means
    the surface had nothing to say. Collapsing them either buries real faults or
    invents them, depending on which way the collapse goes.
    """
    usage_mod._emit_turn_histogram({"duration_ms": 9}, "cron:j", _Event(""))
    assert recorder.samples[0][2]["outcome"] == "ok"


# --- 3. the zero-duration skip survives the move ---------------------------


def test_a_zero_duration_still_emits_nothing(recorder) -> None:
    """An absent sample reads as "no data"; a recorded 0 renders as a 0ms p50."""
    usage_mod._emit_turn_histogram({"duration_ms": 0}, "cron:job-7", _Event())
    assert recorder.samples == []


# --- 4. structural: the emit stays at the shared boundary -------------------


def test_the_emit_has_exactly_one_site_per_owner() -> None:
    """Exactly one sample per turn, with the two owners kept distinct.

    Every background surface is sampled by ``persist_token_record_async`` — the
    one call they all make once per turn, which is what ended this metric being a
    dashboard-only reading. The dashboard emits itself instead, because its
    persist call sits behind ``usage_has_billing``: a zero-billing timeout writes
    no row, and letting that swallow the sample would drop exactly the faults
    fault_rate counts.

    Both halves are asserted because the failure modes are opposite: a second
    emit in the persist path double-counts every surface, while the dashboard
    emitting AND letting persist emit double-counts its own turns. The
    ``emit_metric=False`` argument is what keeps the two from overlapping, so its
    presence is pinned too.
    """
    tree = ast.parse((SRC / "dashboard" / "handlers" / "usage.py").read_text(encoding="utf-8"))
    emits = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "emit_turn_duration"
    ]
    assert len(emits) == 1, f"expected one emit in the persist path, found {len(emits)}"

    runner_src = (SRC / "dashboard" / "chat_runner.py").read_text(encoding="utf-8")
    runner = ast.parse(runner_src)
    calls = [
        n
        for n in ast.walk(runner)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_emit_turn_metric"
    ]
    assert len(calls) == 1, (
        f"_run_chat should emit exactly once, found {len(calls)}. Two emits "
        "double-count every dashboard turn; none loses zero-billing timeouts."
    )

    persists = [
        n
        for n in ast.walk(runner)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "persist_token_record_async"
    ]
    assert len(persists) == 1
    kwargs = {kw.arg: kw.value for kw in persists[0].keywords if kw.arg}
    assert "emit_metric" in kwargs, (
        "the dashboard persist must opt out of the shared emit, or its turns are " "sampled twice"
    )
    assert getattr(kwargs["emit_metric"], "value", None) is False


def test_the_dashboard_emit_is_not_behind_the_billing_gate() -> None:
    """The regression this split exists to prevent.

    Folding the dashboard's emit into its persist call put it behind
    ``usage_has_billing``: a turn that timed out having billed nothing wrote no
    row and therefore produced no sample — silently removing the exact faults
    fault_rate is read for. Asserted structurally on nesting rather than by
    simulating a zero-billing turn, because the property is "this call is not
    inside that branch", which is what a reader needs to keep true.
    """
    runner = ast.parse((SRC / "dashboard" / "chat_runner.py").read_text(encoding="utf-8"))
    gated: list[str] = []
    for node in ast.walk(runner):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "usage_has_billing" not in test_src:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", getattr(inner.func, "attr", ""))
                == "_emit_turn_metric"
            ):
                gated.append(f"line {inner.lineno}")
    assert not gated, (
        f"_emit_turn_metric is inside a usage_has_billing branch ({gated}), so a "
        "zero-billing turn emits nothing at all"
    )


def test_workflow_keys_are_not_pooled_with_unrecognised_shapes() -> None:
    """``wf:`` had no label, so workflow turns read as ``other``.

    ``other`` is the bucket for key shapes nobody planned; a surface that lands
    there is indistinguishable from a bug in key construction.
    """
    assert telemetry_channel_of("wf:run9:1") == "workflow"
    assert telemetry_channel_of("wf-pool:1") == "workflow_pool"
    assert telemetry_channel_of("wf-author:1") == "workflow_author"
    assert telemetry_channel_of("totally-unknown") == "other"
