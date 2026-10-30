"""Per-session per-turn injection breakdown: the read side.

``usage.context_trace(slot, days)`` is served from the slot's ``usage`` PROJECTION
-- one memoised fold of the crew log's ``context/composed`` entries;
``telemetry.api_context_trace`` is the thin HTTP wrapper. These drive the REAL
reader over a real crew log, so what is exercised is the writer, the fold and the
reader together rather than three agreeing fixtures.

The load-bearing one is :class:`TestContextTraceParityWithTheShardScan`, which pins
the payload against the shape a token-shard scan produces, written out by hand. That
shape is a LITERAL rather than a second live implementation: a comparison between two
implementations proves the two agree, which is not the same as proving either right.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.context_blocks import PHASE_PER_TURN, PHASE_SESSION_START, USER_LABEL
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.dashboard.handlers.telemetry import api_context_trace

SLOT = "chat-1"
UNIT = "acp-ctx-trace"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no warm fold carried between tests.

    The warm slot folds are forgotten on both sides: this reader is memoised per
    (home, slot, fold), so a cell left by a previous test would answer the next one
    from entries it never wrote.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


#: The window the most recent :func:`_open` stated, so :func:`_billed` reports
#: occupancy against the size the session was actually configured with. Mirroring the
#: real writer here rather than defaulting each separately is what stops a fixture
#: from measuring a reading against a window no turn ever ran under -- the exact
#: incoherence the occupancy pair exists to prevent.
_CONFIGURED_WINDOW = 1_000_000


def _open(unit: str = UNIT, *, slot: str = SLOT, window: int = 1_000_000, model: str = "opus-5"):
    """Open one unit and state its configuration, which is where the window lives."""
    global _CONFIGURED_WINDOW
    _CONFIGURED_WINDOW = window
    crew_log_emit.on_session_opened(unit, slot=slot)
    if window or model:
        crew_log_emit.on_request_configured(
            unit, 1, model=model, provider="acp", context_window=window
        )


def _compose(blocks, *, unit: str = UNIT, turn: int = 1, phase: str = PHASE_PER_TURN) -> None:
    crew_log_emit.on_context_composed(unit, turn, blocks=blocks, phase=phase)


def _billed(
    *,
    unit: str = UNIT,
    turn: int = 1,
    used: int = 0,
    window: int | None = None,
    model: str = "opus-5",
    billed_input: int = 999_999,
) -> None:
    """Close a turn reporting occupancy *used* against *window*.

    *window* defaults to the size the last :func:`_open` stated, which is what the real
    provider reports against. A test that needs the two to DIVERGE -- a model switch, a
    provider that states no window -- passes it explicitly.

    ``billed_input`` is set high and unequal to *used* on purpose: the peak must come
    from the provider's occupancy reading, so a fixture passing one number could not
    tell the two apart and a peak taken from billed input tokens would still pass.
    """
    if window is None:
        window = _CONFIGURED_WINDOW
    crew_log_emit.on_turn_completed(
        unit,
        turn,
        model=model,
        input_tokens=billed_input,
        output_tokens=1,
        context_used=used,
        context_window=window,
    )


def _refused(*, unit: str = UNIT, turn: int = 1, reason: str = "oversized") -> None:
    """A turn a dispatch gate declined: it never ran, so it only emits turn/refused."""
    crew_log_emit.on_turn_refused(unit, turn, reason)


def _flush() -> None:
    crew_log_emit.flush(timeout=5.0)
    crew_log.forget_slot_folds()


class TestContextTrace:

    def test_user_chars_comes_from_your_message_label(self):
        _open()
        _compose({USER_LABEL: 42, "memory": 100})
        _flush()
        assert usage_mod.context_trace(SLOT, 14)["user_chars"] == 42

    def test_user_chars_zero_when_no_your_message_label(self):
        _open()
        _compose({"memory": 100})
        _flush()
        assert usage_mod.context_trace(SLOT, 14)["user_chars"] == 0

    def test_the_runner_hands_the_providers_reading_to_the_turn_closer(self):
        """The wiring, pinned at the source, because nothing else here reaches it.

        Every other test drives the emitter directly, so a runner that stopped passing
        the reading would leave them all green while the panel silently lost occupancy
        -- the fold would see no ``context`` object and report 0. Running a real turn to
        cover one dict literal is not worth its cost, so the literal is read instead:
        the same pattern ``test_crew_log_types.py`` uses to check emitter call sites.

        Three things are asserted, and the third matters most. The keys must exist; they
        must carry the names ``read_context_tokens`` was unpacked into (a hardcoded 0
        satisfies mere presence and records every turn as unmeasured); and the unpacking
        must sit at the SAME nesting depth as the closer that reads it, not inside a
        branch. A read nested under a billing gate leaves the names unbound on a turn
        that billed nothing -- a fake backend, an unmetered provider -- so the
        completion path raises ``UnboundLocalError`` instead of closing the turn. That
        breaks every caller of the turn path, and no test driving the emitter directly
        can see it.
        """
        import ast

        runner = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_runner.py"
        )
        source = "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [*sorted(runner.with_name("chat_turn").glob("*.py")), runner]
            if turn_file.name != "__init__.py"
        )
        tree = ast.parse(source)

        # Parent links, so a node's enclosing branches can be walked upward: ast
        # supplies no parent pointers, so they are built once here.
        parent: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[id(child)] = node

        def guards(node: ast.AST) -> list[str]:
            """The branches and blocks *node* sits INSIDE, innermost first."""
            out: list[str] = []
            cur: ast.AST | None = node
            while cur is not None:
                up = parent.get(id(cur))
                if isinstance(up, ast.If) and any(cur is s for s in up.body + up.orelse):
                    out.append(ast.unparse(up.test))
                elif isinstance(up, (ast.For, ast.While, ast.Try, ast.With)):
                    out.append(type(up).__name__)
                cur = up
            return out

        def function_of(node: ast.AST) -> ast.AST | None:
            cur = parent.get(id(node))
            while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cur = parent.get(id(cur))
            return cur

        # The function that builds the turn closer's payload: the reading it passes is
        # the one that function unpacks, wherever the turn's phases put it.
        closer_fns = [
            function_of(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            and {"stop_reason", "context_used", "context_window"}
            <= {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        ]

        # The pair the provider reading is unpacked into, read from the runner rather
        # than assumed, so a rename moves both halves of this check together.
        unpacked: tuple[str, ...] = ()
        unpack_guards: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "read_context_tokens":
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Tuple)
                and len(target.elts) == 2
                and any(function_of(node) is fn for fn in closer_fns)
            ):
                unpacked = tuple(el.id for el in target.elts if isinstance(el, ast.Name))
                unpack_guards = guards(node)
        assert len(unpacked) == 2, (
            "could not find `used, window = read_context_tokens(...)` in chat_runner; "
            "this check cannot verify the wiring it exists for"
        )

        wired: dict[str, str] = {}
        closer_guards: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if not {"stop_reason", "context_used", "context_window"} <= keys:
                continue
            closer_guards = guards(node)
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in ("context_used", "context_window")
                    and isinstance(value, ast.Name)
                ):
                    wired[key.value] = value.id
        assert set(wired) == {"context_used", "context_window"}, (
            "the turn-closer payload does not pass both occupancy fields as the names "
            f"read_context_tokens was unpacked into; found {wired}"
        )
        assert set(wired.values()) == set(unpacked), (
            f"the turn closer passes {sorted(wired.values())}, not the "
            f"{sorted(unpacked)} that read_context_tokens produced"
        )
        # The binding must not be conditional on anything the closer is not. A guard
        # the read sits inside but the closer does not is a path on which the closer
        # runs with the names unbound.
        extra = [g for g in unpack_guards if g not in closer_guards]
        assert not extra, (
            "read_context_tokens is bound inside branch(es) the turn closer is not: "
            f"{extra}. Every completed turn reaches that closer, so on a turn where "
            "that branch does not run the names are unbound and the completion path "
            "raises UnboundLocalError. Move the read out to the closer's own level."
        )

    def test_no_composition_is_recorded_before_the_dispatch_gates(self):
        """A refused turn must not leave a composition the chart draws a bar for.

        ``context/composed`` states what the gateway PUT IN FRONT OF THE MODEL, so it
        belongs after every gate that can refuse the dispatch. Emitted before them, a
        refusal leaves a composition behind, the fold publishes it as a per-turn row, and
        the panel draws a turn that never ran -- unretractably, since ``USAGE_TYPES``
        carries no ``turn/refused``.

        Pinned at the source, because reaching it through a real refusal needs a dispatch
        gate to fire while the cheap invariant is positional. ``on_turn_started`` already
        sits after those gates for the same stated reason, so it is the control: if the
        scan cannot place it after any refusal either, the scan is broken rather than the
        code correct.
        """
        import ast

        runner = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_runner.py"
        )
        source = "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [*sorted(runner.with_name("chat_turn").glob("*.py")), runner]
            if turn_file.name != "__init__.py"
        )
        tree = ast.parse(source)

        lines: dict[str, list[int]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in (
                "on_context_composed",
                "on_turn_refused",
                "on_turn_started",
            ):
                lines.setdefault(node.func.attr, []).append(node.lineno)

        refusals = sorted(lines.get("on_turn_refused", []))
        composed = sorted(lines.get("on_context_composed", []))
        started = sorted(lines.get("on_turn_started", []))
        assert refusals and composed and started, (
            f"could not find all three call sites (refused={refusals}, "
            f"composed={composed}, started={started}); this check cannot verify the "
            "ordering it exists for"
        )
        # The control: the turn opener already clears the dispatch gates, so the gates
        # it clears are the ones the composition must clear too.
        dispatch_gates = [ln for ln in refusals if ln < max(started)]
        assert dispatch_gates, "no refusal precedes on_turn_started; the control failed"
        too_early = [ln for ln in composed if any(ln < gate for gate in dispatch_gates)]
        assert not too_early, (
            f"on_context_composed at line(s) {too_early} is emitted BEFORE dispatch gate "
            f"refusal(s) at {sorted(dispatch_gates)}. A refused turn would leave a "
            "composition the usage fold publishes as a per-turn row, and the fold has no "
            "retraction path. Move the emit down beside on_turn_started."
        )

    def test_peak_is_zero_when_no_turn_reported_occupancy(self):
        _open()
        _compose({"memory": 100})
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 0
        assert "estimated_other_chars" not in out

    def test_peak_is_the_providers_reading_not_the_billed_input(self):
        """Billing and occupancy are two quantities, and the pair needs the second.

        ``tokens.input`` is summed over every model call a turn made, so on a
        tool-using turn it exceeds the window it would be divided by -- and the
        Session Breakdown tree DOES divide. The turns below bill far more input than
        they occupy, so a peak taken from billing would be visible here.
        """
        _open()
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=200, billed_input=500_000)
        _compose({"memory": 100}, turn=2)
        _billed(turn=2, used=900, billed_input=700_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 900
        assert out["injected_chars"] == 200
        # Characters and tokens are never combined into one derived number.
        assert "estimated_other_chars" not in out

    def test_peak_keeps_the_later_turn_among_equally_full_readings(self):
        """``>=`` on the ratio keeps the LATEST equally-full turn, as before.

        Two turns are exactly 50% full on different windows (a switch between them).
        The later one describes the model currently running, so its pair is reported.
        """
        _open(window=100_000, model="opus-5")
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=50_000, window=100_000, model="opus-5")
        crew_log_emit.on_request_configured(
            UNIT, 2, model="haiku-9", provider="acp", context_window=200_000
        )
        _compose({"memory": 100}, turn=2)
        _billed(turn=2, used=100_000, window=200_000, model="haiku-9")
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        # Both are 50% full; the later turn's pair wins the tie.
        assert out["peak_context_used"] == 100_000
        assert out["context_window"] == 200_000

    def test_the_occupancy_pair_comes_from_one_turn(self):
        """The reading and its window must describe the SAME turn.

        The Session Breakdown tree divides ``peak_context_used`` by
        ``context_window``. A model switch moves the window, so taking the peak from
        the fullest turn and the window from the newest configuration would produce a
        ratio for a turn that never ran. Here the fullest turn ran on the SMALL window
        and a later, emptier turn moved to a large one: the pair must report the
        fullest turn's own window, and the ratio must not be diluted by the later one.
        """
        _open(window=100_000, model="opus-5")
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=90_000, window=100_000, model="opus-5")
        crew_log_emit.on_request_configured(
            UNIT, 2, model="haiku-9", provider="acp", context_window=1_000_000
        )
        _compose({"memory": 100}, turn=2)
        _billed(turn=2, used=10_000, window=1_000_000, model="haiku-9")
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 90_000
        assert out["context_window"] == 100_000, (
            "the window must be the one the peak reading was measured against, not "
            "the newest configured size"
        )
        # What the tree actually renders: 90% full, not 9%.
        assert out["peak_context_used"] / out["context_window"] == 0.9

    def test_the_window_falls_back_to_the_configured_size_with_no_reading_yet(self):
        """No turn has reported occupancy, so there is no pair to be coherent with."""
        _open(window=200_000)
        _compose({"memory": 100})
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 0
        assert out["context_window"] == 200_000

    def test_a_reading_whose_window_is_unknown_is_not_a_peak_candidate(self):
        """A used count with no window has no fullness ratio, so it cannot be the peak.

        The peak is the fullest turn -- the highest ``used / used_window`` -- and a
        reading whose window is 0 has no denominator to be a fraction of. Crowning it
        would report a peak over a zero window (a broken ratio the frontend then has to
        hide). Here the later turn reports MORE used tokens (80k) but no window, while
        an earlier turn is a valid pair (40k / 100k): the earlier, valid turn is the
        peak, and its own window travels with it.
        """
        _open(window=100_000)
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=40_000, window=100_000)
        _compose({"memory": 100}, turn=2)
        _billed(turn=2, used=80_000, window=0)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 40_000, (
            "the windowless 80k reading has no ratio and must not win the peak over "
            "the valid 40k/100k pair"
        )
        assert out["context_window"] == 100_000

    def test_peak_is_the_fullest_ratio_not_the_largest_used_across_a_switch(self):
        """A smaller ``used`` against a smaller window can be fuller than a larger one.

        This is the exact case GPT flagged: comparing ``used`` alone ignores the
        window a model switch moves. Turn 1 uses 90k of a 100k window (90% full); turn
        2 switches to a 1M window and uses 200k (20% full). 200k is the larger absolute
        reading, but turn 1 is the fuller turn -- the peak and its window must be turn
        1's, and the rendered ratio 90%, not turn 2's 20%.
        """
        _open(window=100_000, model="opus-5")
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=90_000, window=100_000, model="opus-5")
        crew_log_emit.on_request_configured(
            UNIT, 2, model="haiku-9", provider="acp", context_window=1_000_000
        )
        _compose({"memory": 100}, turn=2)
        _billed(turn=2, used=200_000, window=1_000_000, model="haiku-9")
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert (
            out["peak_context_used"] == 90_000
        ), "the fuller 90k/100k turn wins over the larger-but-emptier 200k/1M turn"
        assert out["context_window"] == 100_000
        assert out["peak_context_used"] / out["context_window"] == 0.9

    def test_a_peak_outside_the_day_window_is_not_reported(self):
        """The caller asked about a span of days, so the peak comes from that span.

        On a long session the fullest turn is frequently older than every row in the
        span; a session-wide maximum would answer a question nobody asked.
        """
        _open(window=200_000)
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=180_000, window=200_000)
        _flush()
        # Move the reader's clock past the window so the only reading falls outside it.
        real = usage_mod.datetime

        class _Later(real):  # type: ignore[misc, valid-type]
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + timedelta(days=40)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(usage_mod, "datetime", _Later)
            out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 0, (
            "the peak must come from the rows inside the requested window, not from "
            "the whole session"
        )
        # With no reading in the span, the configured size is still what a reader can
        # be told the session runs under.
        assert out["context_window"] == 200_000

    def test_an_open_turn_does_not_drag_the_peak_down(self):
        """An open turn has composed but not closed, so it measured nothing yet."""
        _open()
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=6_000)
        _compose({"memory": 100}, turn=2)  # turn 2 is still running
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["peak_context_used"] == 6_000

    def test_a_slot_that_never_ran_reads_as_empty_rather_than_failing(self):
        out = usage_mod.context_trace("chat-never", 14)
        assert out["turns"] == []
        assert out["totals"] == {}
        assert out["injected_chars"] == 0

    def test_the_payload_says_whether_the_crew_log_is_recording(self, monkeypatch):
        """An empty list cannot say why it is empty, so the payload does.

        With the switch on, an empty session fills on its next turn; with it off,
        nothing will ever arrive, and the panel must not promise otherwise.
        """
        monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
        assert usage_mod.context_trace("chat-never", 14)["recording"] is True
        assert "env_file" not in usage_mod.context_trace("chat-never", 14)
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        off = usage_mod.context_trace("chat-never", 14)
        assert off["recording"] is False
        # Names the file to edit, the same reading the Crew Log panel's off state uses.
        assert off["env_file"] == usage_mod.env_file_display()

    def test_an_unreadable_fold_reads_as_nothing_folded(self, monkeypatch, caplog):
        """A damaged log is not a 500. The panel renders an empty trace instead."""

        def _boom(*_a: Any, **_k: Any):
            raise OSError("log is damaged")

        monkeypatch.setattr(crew_log, "read_slot_state", _boom)
        with caplog.at_level("WARNING"):
            out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"] == []
        assert out["slot"] == SLOT
        assert any("usage fold unreadable" in r.message for r in caplog.records)


def _ran(turn: int, memory: int = 100, *, phase: str = PHASE_PER_TURN) -> None:
    """One turn that composed and closed: the only kind that writes a chart row."""
    _compose({"memory": memory}, turn=turn, phase=phase)
    _billed(turn=turn, used=5_000)


def _ordinals() -> list[int]:
    return [t["ordinal"] for t in usage_mod.context_trace(SLOT, 14)["turns"]]


class TestEveryTurnTakesAnOrdinal:
    """The ordinal is the turn's place in the conversation, not a count of the turns
    that wrote a row. A turn that ran without composing (a slash command) and a turn a
    dispatch gate refused both hold a number, so every later row counts past them;
    otherwise the panel shows the wrong "Turn N" and nothing self-corrects."""

    def test_a_turn_that_composed_nothing_still_advances_the_ordinal(self):
        """MUTATION-SENSITIVE: composed 1, slash command 2, composed 3 -> rows 1 and 3."""
        _open()
        _ran(1)
        _billed(turn=2, used=0)  # a turn that closed without composing
        _ran(3)
        _flush()
        assert _ordinals() == [1, 3]

    def test_a_retried_composition_less_turn_takes_one_ordinal(self):
        """A turn closed once per attempt counts once: turn 3 reads 3, never 4."""
        _open()
        _ran(1)
        _billed(turn=2, used=0)
        _billed(turn=2, used=0)
        _ran(3)
        _flush()
        assert _ordinals() == [1, 3]

    def test_consecutive_composition_less_turns_each_take_an_ordinal(self):
        """Two such turns in a row advance the count twice: turn 4 reads 4."""
        _open()
        _ran(1)
        _billed(turn=2, used=0)
        _billed(turn=3, used=0)
        _ran(4)
        _flush()
        assert _ordinals() == [1, 4]

    def test_a_composition_less_first_turn_takes_ordinal_one(self):
        """A slash command before any composed turn still comes first."""
        _open()
        _billed(turn=1, used=0)
        _ran(2)
        _flush()
        assert _ordinals() == [2]

    def test_repeated_compositions_of_one_turn_share_one_ordinal(self):
        """MUTATION-SENSITIVE: a turn composed twice is one turn and one row."""
        _open()
        _compose({"memory": 100}, turn=1)
        _compose({"memory": 50}, turn=1)
        _billed(turn=1, used=6_000)
        _ran(2)
        _flush()
        assert _ordinals() == [1, 2]

    def test_a_refused_turn_consumes_an_ordinal(self):
        """MUTATION-SENSITIVE: composed 1, refused 2, composed 3 -> rows 1 and 3."""
        _open()
        _ran(1)
        _refused(turn=2)
        _ran(3)
        _flush()
        assert _ordinals() == [1, 3]

    def test_a_refused_turn_counts_once_even_retried(self):
        """A turn refused twice takes one ordinal: turn 3 reads 3, never 4."""
        _open()
        _ran(1)
        _refused(turn=2)
        _refused(turn=2)
        _ran(3)
        _flush()
        assert _ordinals() == [1, 3]

    def test_a_session_start_after_a_refused_first_turn_reads_ordinal_two(self):
        """A refused turn 1 pushes the session start to ordinal 2, still the start."""
        _open()
        _refused(turn=1)
        _ran(2, 30_000, phase=PHASE_SESSION_START)
        _ran(3)
        _flush()
        turns = usage_mod.context_trace(SLOT, 14)["turns"]
        assert [(t["ordinal"], t["phase"], t["first_start"]) for t in turns] == [
            (2, PHASE_SESSION_START, True),
            (3, PHASE_PER_TURN, False),
        ]


def _state_with(state: Any):
    """A stand-in ``read_slot_state`` answering with *state* as the usage fold state.

    The reader's own guards are reachable only from a state the fold would never
    hold, which is precisely the input a reader is written to survive. Planting the
    state is how those branches get exercised without forging a crew log file.
    """

    def _read(*_a: Any, **_k: Any):
        return crew_log.Checkpoint(name="usage", last_seq=0, state=state), 7

    return _read


class TestContextTraceSurvivesARowTheWriterWouldNotProduce:
    """The reader's guards, driven from a planted fold value.

    Every one of these branches answers a damaged or planted log line. Reaching them
    through the writer is impossible by construction -- it filters non-positive blocks
    and stamps every entry itself -- so the fold's value is planted instead. A guard
    with no test is a guard that is one refactor from being deleted as dead.
    """

    def test_a_row_whose_every_block_is_non_positive_is_skipped(self, monkeypatch):
        _open()
        _ran(1)
        _flush()
        state = crew_log.read_slot_state(SLOT, "usage")[0].state
        ctx = dict(state["ctx"])
        trend = [list(row) for row in ctx["trend"]]
        trend[-1][crew_log._BLOCKS] = [["empty", 0]]
        ctx["trend"] = trend
        monkeypatch.setattr(crew_log, "read_slot_state", _state_with({**state, "ctx": ctx}))
        assert usage_mod.context_trace(SLOT, 36_500)["turns"] == []

    def test_a_state_that_holds_no_context_reads_as_empty(self, monkeypatch, caplog):
        monkeypatch.setattr(crew_log, "read_slot_state", _state_with({"ctx": "not a mapping"}))
        with caplog.at_level("WARNING"):
            out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"] == [] and out["latest"] is None
        assert usage_mod.context_turn_detail(SLOT, 1) is None


class TestContextTraceWindowBounds:
    """Two bounds, and the payload states both because they cut differently."""

    #: The slot-fold cache's own ceiling, stated in ``projection.py`` against that
    #: cache's largest budgeted member (``radar``). A full context window has to fit
    #: under it or the cache's documented budget is wrong.
    CACHE_MEMBER_CEILING = 995_342


class TestContextTraceParityWithTheShardScan:
    """The payload the shard scan produced, written out by hand and compared.

    The scan is gone, so the expected shape here is a LITERAL rather than a second
    live implementation: comparing two implementations proves they agree, which says
    nothing about whether either matches what the panel was built against.

    Every field carries the scan's own meaning, occupancy included: the provider's
    reading now rides on ``turn/completed``, so ``turns[].context_used`` and the
    ``peak_context_used`` / ``context_window`` pair are the same figures the row store
    held, bounded to the same day window.
    """


class TestTheContractTheFrontendDeclares:
    """The payload against the TypeScript interface, read out of the panel itself.

    This is the seam the whole read-path change rests on: the panel was not touched,
    so a field it declares and this reader stops sending is a blank chart with no
    error anywhere. Pinning it against the interface's own text -- rather than
    against a list retyped here -- is what makes the two move together, and it holds
    without a node toolchain, which is the other reason it lives on this side.
    """

    @staticmethod
    def _declared(block: str) -> set[str]:
        panel = (
            Path(__file__).resolve().parents[2]
            / "website"
            / "src"
            / "pages"
            / "ContextBreakdownPanel.tsx"
        ).read_text(encoding="utf-8")
        body = panel.split(f"export interface {block} {{", 1)[1].split("\n}", 1)[0]
        return {
            line.split(":", 1)[0].strip().rstrip("?")
            for line in body.splitlines()
            if ":" in line and not line.strip().startswith(("*", "/*", "//"))
        }

    def test_every_field_the_trace_interface_declares_is_sent(self, monkeypatch):
        _open(window=200_000)
        _compose({"memory": 100, USER_LABEL: 10})
        _billed(used=1_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        # ``env_file`` rides only on a payload with recording off, so the union of
        # both payloads is what the interface must be covered by.
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        out = {**out, **usage_mod.context_trace(SLOT, 14)}
        declared = self._declared("ContextTrace")
        # The control: the extraction found a real interface, not an empty string.
        assert "turns" in declared and "peak_context_used" in declared, declared
        assert declared <= set(
            out
        ), f"the panel declares fields this reader omits: {declared - set(out)}"

    def test_every_field_a_turn_declares_is_sent(self):
        _open(window=200_000)
        _compose({"memory": 100}, phase=PHASE_SESSION_START)
        _billed(used=1_000)
        _flush()
        turn = usage_mod.context_trace(SLOT, 14)["turns"][0]
        declared = self._declared("ContextTurn")
        assert "blocks" in declared and "phase" in declared, declared
        assert declared <= set(
            turn
        ), f"the panel declares turn fields this reader omits: {declared - set(turn)}"

    def test_a_turn_carries_only_fields_with_a_value_and_a_reader(self):
        """A row is what the kept turn sent. The state keeps no per-turn time, model or
        reading for it, so the row carries none rather than fixed blanks."""
        _open(window=200_000)
        _compose({"memory": 100})
        _billed(used=1_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert set(out["turns"][0]) == {"phase", "blocks", "total_chars", "ordinal", "first_start"}
        assert "engine_unknown" not in out


class TestApiContextTrace:
    @staticmethod
    def _app():
        app = web.Application()
        app.router.add_get("/api/telemetry/context-trace", api_context_trace)
        return app

    @pytest.mark.asyncio
    async def test_missing_slot_returns_400(self):
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/telemetry/context-trace")
            body = await resp.json()
            assert resp.status == 400
            # `code` is the contract the localized dashboard branches on; the
            # prose in `error` is advisory and untranslatable on its own.
            assert body["code"] == "slot_required"

    @pytest.mark.asyncio
    async def test_blank_slot_returns_400(self):
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/telemetry/context-trace", params={"slot": "   "})
            assert resp.status == 400
            assert (await resp.json())["code"] == "slot_required"

    @pytest.mark.asyncio
    async def test_returns_trace_payload_for_slot(self):
        _open()
        _compose({"memory": 100, USER_LABEL: 10})
        _billed(used=1_000)
        _flush()
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/telemetry/context-trace", params={"slot": SLOT})
            assert resp.status == 200
            body = await resp.json()
            assert body["slot"] == SLOT
            assert len(body["turns"]) == 1
            assert body["turns"][0]["blocks"]["memory"] == 100
            assert body["user_chars"] == 10
            assert body["peak_context_used"] == 1_000
            assert "estimated_other_chars" not in body
            # The harness is the selection-capabilities route's to answer, not this one.
            assert "harness" not in body
            assert isinstance(body["revision"], int)

    @pytest.mark.asyncio
    async def test_a_kept_turn_reads_whole_and_one_not_kept_is_404(self):
        _open()
        for turn in range(1, 36):
            _compose({"memory": 100 + turn, USER_LABEL: 10}, turn=turn)
            _billed(turn=turn, used=1_000 + turn)
        _flush()
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get(
                "/api/telemetry/context-trace", params={"slot": SLOT, "turn": "20"}
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["slot"] == SLOT and isinstance(body["revision"], int)
            assert body["turn"]["turn"] == 20
            assert body["turn"]["blocks"] == {"memory": 120, USER_LABEL: 10}
            assert body["turn"]["context_used"] == 1_020
            # Turn 5 left the 30-turn ring.
            resp = await client.get(
                "/api/telemetry/context-trace", params={"slot": SLOT, "turn": "5"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "turn_not_kept"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("turn", ["", "abc", "0", "-1", "1.5", "1e3", "\u0661", "9" * 12])
    async def test_a_turn_that_is_not_a_positive_integer_is_400(self, turn):
        _open()
        _compose({"memory": 100})
        _billed(used=1_000)
        _flush()
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get(
                "/api/telemetry/context-trace", params={"slot": SLOT, "turn": turn}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "turn_invalid"


class TestContextTraceCarriesNoBilling:
    """The trace answers "what was injected"; billing has its own reader."""

    def test_billing_fields_do_not_reach_the_turn(self):
        _open()
        _compose({"memory": 10})
        crew_log_emit.on_turn_completed(
            UNIT, 1, model="opus-5", credits=3.5, duration_ms=42_000, input_tokens=5
        )
        _flush()
        turns = usage_mod.context_trace(SLOT)["turns"]
        assert "credits" not in turns[0]
        assert "duration_ms" not in turns[0]
        assert turns[0]["total_chars"] == 10


class TestApiContextTraceAppDenial:
    """The trace is dashboard-only: an app caller is refused, not filtered.

    Unlike ``/api/usage/turns`` this reader has no row-ownership model, so the
    endpoint denies app identities outright with the standard indistinguishable 404,
    and SEL-audits the refusal.
    """

    @staticmethod
    def _app(request_app: str = "") -> web.Application:
        app = web.Application()

        @web.middleware
        async def stamp(request, handler):
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(stamp)
        app.router.add_get("/api/telemetry/context-trace", api_context_trace)
        return app

    @pytest.fixture(autouse=True)
    def _quiet_sel(self, monkeypatch):
        import kiro_crew.sel as sel_mod

        calls: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        self.sel_calls = calls

    @pytest.mark.asyncio
    async def test_an_app_caller_is_refused_with_the_standard_404(self):
        _open()
        _compose({"memory": 100})
        _flush()
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get(f"/api/telemetry/context-trace?slot={SLOT}")
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "not_found"
        assert any(
            c.get("outcome") == "denied" and c.get("caller") == "acme-app" for c in self.sel_calls
        )

    @pytest.mark.asyncio
    async def test_an_app_caller_asking_for_a_turn_is_refused_the_same_way(self):
        _open()
        _compose({"memory": 100})
        _billed(used=1_000)
        _flush()
        async with TestClient(TestServer(self._app("acme-app"))) as client:
            resp = await client.get(f"/api/telemetry/context-trace?slot={SLOT}&turn=1")
            assert resp.status == 404
            assert (await resp.json())["code"] == "not_found"
        assert any(c.get("outcome") == "denied" for c in self.sel_calls)

    @pytest.mark.asyncio
    async def test_a_dashboard_caller_still_reads_the_trace(self):
        _open()
        _compose({"memory": 100})
        _billed(used=1_000)
        _flush()
        async with TestClient(TestServer(self._app(""))) as client:
            resp = await client.get(f"/api/telemetry/context-trace?slot={SLOT}")
            assert resp.status == 200
            body = await resp.json()
            assert body["turns"][0]["blocks"]["memory"] == 100
