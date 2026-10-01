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

import sys
from datetime import datetime, timedelta
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
    def test_chronological_order_across_two_units(self):
        """A slot's units are folded oldest first, so turns come back in order."""
        _open("acp-older")
        _compose({"memory": 10}, unit="acp-older", turn=1)
        _flush()
        _open("acp-newer")
        _compose({"memory": 50}, unit="acp-newer", turn=1)
        _compose({"memory": 100}, unit="acp-newer", turn=2)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert [t["blocks"]["memory"] for t in out["turns"]] == [10, 50, 100]
        assert [t["ts"] for t in out["turns"]] == sorted(t["ts"] for t in out["turns"])

    def test_compositions_with_no_positive_block_are_skipped_not_zero_filled(self):
        _open()
        _compose({"memory": 100}, turn=1)
        _compose({}, turn=2)  # the writer drops an empty composition entirely
        _compose({"memory": 0}, turn=3)  # present but non-positive
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert len(out["turns"]) == 1
        assert out["turns"][0]["blocks"] == {"memory": 100}

    def test_other_slots_are_excluded(self):
        _open("acp-mine", slot=SLOT)
        _compose({"memory": 100}, unit="acp-mine")
        _flush()
        _open("acp-theirs", slot="chat-2")
        _compose({"memory": 999}, unit="acp-theirs")
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert len(out["turns"]) == 1
        assert out["totals"] == {"memory": 100}

    def test_totals_accumulate_across_turns(self):
        _open()
        _compose({"memory": 100, "lessons": 50}, turn=1)
        _compose({"memory": 30, "skill_index": 20}, turn=2)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["totals"] == {"memory": 130, "lessons": 50, "skill_index": 20}
        assert out["injected_chars"] == 200

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

    def test_the_phase_the_composer_stated_reaches_the_payload(self):
        """The panel splits these two populations apart before it draws."""
        _open()
        _compose({"memory": 30_000}, turn=1, phase=PHASE_SESSION_START)
        _compose({USER_LABEL: 120}, turn=2, phase=PHASE_PER_TURN)
        _flush()
        assert [t["phase"] for t in usage_mod.context_trace(SLOT, 14)["turns"]] == [
            "session_start",
            "per_turn",
        ]

    def test_a_composition_written_without_a_phase_still_folds(self):
        """``phase`` is ADDITIVE: an entry that predates it must fold, not refuse.

        The writer omits the key entirely when it has no phase to state, which is the
        shape every ``context/composed`` on disk already has. If that entry stopped
        folding, one upgrade would empty the panel for every session recorded before
        it -- so the case is pinned here rather than left to the field's optionality.
        The turn's phase reads as "" (unstated), and every other field is unaffected.
        """
        _open(window=200_000)
        crew_log_emit.on_context_composed(UNIT, 1, blocks={"memory": 100, USER_LABEL: 10})
        _billed(turn=1, used=4_200)
        _flush()
        # The entry really carries no ``phase`` key -- not an empty one.
        handle = crew_log.open_session_log(UNIT)
        composed = [
            entry
            for entry in handle.iter_from(1, known=crew_log.KNOWN_TYPES)
            if entry.type == "context/composed"
        ]
        assert len(composed) == 1, composed
        assert "phase" not in composed[0].data, composed[0].data

        out = usage_mod.context_trace(SLOT, 14)
        assert len(out["turns"]) == 1
        turn = out["turns"][0]
        assert turn["phase"] == ""
        assert turn["blocks"] == {"memory": 100, "your_message": 10}
        assert turn["total_chars"] == 110
        assert turn["context_window"] == 200_000
        assert out["peak_context_used"] == 4_200

    def test_a_turn_carries_the_window_its_prompt_was_measured_against(self):
        _open(window=200_000)
        _compose({"memory": 100})
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"][0]["context_window"] == 200_000
        assert out["context_window"] == 200_000

    def test_a_new_unit_does_not_inherit_the_previous_units_model_or_window(self):
        """A slot folds every unit into one state, so the model/window stamps must be
        cleared when a new unit opens -- otherwise a successor whose own configuration
        reports an empty model or a zero window (the auto/backend-default and
        provider-unreported cases, both ordinary) silently keeps the PREVIOUS unit's
        stamp and mislabels its rows.
        """
        _open("acp-first", window=200_000, model="opus-5")
        _compose({"memory": 100}, unit="acp-first", turn=1)
        _flush()
        # Second unit of the SAME slot opens with no configuration of its own
        # (window=0, model="" -> _open skips request/configured), the auto/unreported
        # case. Its row must read absent, not the first unit's opus-5 / 200k.
        _open("acp-second", window=0, model="")
        _compose({"memory": 50}, unit="acp-second", turn=1)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        first_row, second_row = out["turns"][0], out["turns"][1]
        assert first_row["blocks"]["memory"] == 100
        assert first_row["context_window"] == 200_000
        assert first_row["model"] == "opus-5"
        assert second_row["blocks"]["memory"] == 50
        assert second_row["context_window"] == 0
        assert second_row["model"] == ""

    def test_a_measured_row_reports_the_providers_window_not_the_configured_one(self):
        """A model switch moves the window, so a measured row uses the reading's own.

        The configured window is stamped on the row at composition from the newest
        request/configured; the provider's own ``used_window`` is the size the reading
        was actually taken against. Pairing a measured ``context_used`` with the
        configured size would divide the reading by a window it was never measured
        against once the two diverge. Only an UNMEASURED turn -- no reading, no window
        of its own -- falls back to the configured size.
        """
        _open(window=200_000)
        _compose({"memory": 100}, turn=1)
        # The turn is configured at 200k but the provider measured against 128k.
        _billed(turn=1, used=90_000, window=128_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"][0]["context_used"] == 90_000
        assert out["turns"][0]["context_window"] == 128_000

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

        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_runner.py"
        ).read_text(encoding="utf-8")
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
            if isinstance(target, ast.Tuple) and len(target.elts) == 2:
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

        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_runner.py"
        ).read_text(encoding="utf-8")
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

    def test_a_row_carries_the_reading_of_the_turn_it_belongs_to(self):
        """The closer arrives after the compositions it closes and stamps them.

        This is what lets the reader take a peak bounded to its day window: the
        reading sits beside a timestamp instead of in a session-wide scalar that no
        window could narrow.
        """
        _open()
        _compose({"memory": 100})
        _billed(used=9_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"][0]["context_used"] == 9_000

    def test_every_composition_of_one_turn_carries_that_turns_reading(self):
        """A turn with several steps has several rows, and occupancy is the turn's."""
        _open()
        _compose({"memory": 100}, turn=1)
        _compose({"memory": 50}, turn=1)
        _billed(turn=1, used=6_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert [t["context_used"] for t in out["turns"]] == [6_000, 6_000]

    def test_a_row_whose_turn_never_closed_reports_no_reading(self):
        """An open turn has composed but not closed, so it measured nothing yet."""
        _open()
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=6_000)
        _compose({"memory": 100}, turn=2)  # turn 2 is still running
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert [t["context_used"] for t in out["turns"]] == [6_000, 0]
        # The open turn must not drag the peak down to its own absent reading.
        assert out["peak_context_used"] == 6_000

    def test_a_later_units_reading_does_not_restamp_the_previous_units_turn(self):
        """Turn numbers restart per unit, so a match is not proof of the same turn.

        The fold merges every unit of a slot into one window, and each unit numbers
        its turns from 1. The closer walks the window back from the tail while the
        turn number matches -- so a later unit's turn 1 would reach the PREVIOUS
        unit's turn 1 and re-stamp its reading, the incoherence Opus flagged. The
        earlier row already carries its own unit's reading, and that is the boundary
        the walk must not cross.
        """
        _open("acp-first")
        _compose({"memory": 100}, unit="acp-first", turn=1)
        _billed(unit="acp-first", turn=1, used=3_000)
        _flush()
        _open("acp-second")
        _compose({"memory": 200}, unit="acp-second", turn=1)
        _billed(unit="acp-second", turn=1, used=7_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        # Each unit's turn 1 keeps its OWN reading; the second's 7_000 does not bleed
        # back onto the first's 3_000.
        assert [t["context_used"] for t in out["turns"]] == [3_000, 7_000]

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

    def test_a_later_zero_window_does_not_erase_the_size_stated_earlier(self):
        """A provider that reports no window writes 0, which is not a window of nothing.

        ``request/configured`` is written only when the configuration CHANGED, so
        taking a 0 as the current size would erase a figure the session is still
        running under and leave the occupancy pair with no denominator.
        """
        _open(window=200_000)
        _compose({"memory": 100}, turn=1)
        crew_log_emit.on_request_configured(
            UNIT, 2, model="opus-5", provider="acp", context_window=0
        )
        _compose({"memory": 100}, turn=2)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["context_window"] == 200_000
        assert [t["context_window"] for t in out["turns"]] == [200_000, 200_000]

    def test_a_slot_that_never_ran_reads_as_empty_rather_than_failing(self):
        out = usage_mod.context_trace("chat-never", 14)
        assert out["turns"] == []
        assert out["totals"] == {}
        assert out["injected_chars"] == 0

    def test_an_unreadable_fold_reads_as_nothing_folded(self, monkeypatch, caplog):
        """A damaged log is not a 500. The panel renders an empty trace instead."""

        def _boom(*_a: Any, **_k: Any):
            raise OSError("log is damaged")

        monkeypatch.setattr(crew_log, "read_slot_projection", _boom)
        with caplog.at_level("WARNING"):
            out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"] == []
        assert out["slot"] == SLOT
        assert any("usage fold unreadable" in r.message for r in caplog.records)


def _projection_with(rows: list[dict[str, Any]], **context: Any):
    """A stand-in ``usage`` projection carrying exactly *rows*.

    The reader's own guards -- an undated row, a non-positive block -- are reachable
    only from a log line the writer would never produce, which is precisely the input
    a fold is written to survive. Planting the folded value is how those branches get
    exercised without forging a crew log file.
    """

    class _Value:
        value = {"context": {"turns": rows, "window": 0, "peak_used": 0, **context}}

    def _read(*_a: Any, **_k: Any):
        return _Value()

    return _read


class TestContextTraceSurvivesARowTheWriterWouldNotProduce:
    """The reader's guards, driven from a planted fold value.

    Every one of these branches answers a damaged or planted log line. Reaching them
    through the writer is impossible by construction -- it filters non-positive blocks
    and stamps every entry itself -- so the fold's value is planted instead. A guard
    with no test is a guard that is one refactor from being deleted as dead.
    """

    def test_an_undated_row_is_excluded_rather_than_placed_in_the_window(self, monkeypatch):
        monkeypatch.setattr(
            crew_log,
            "read_slot_projection",
            _projection_with(
                [
                    {"ts": "2026-09-30T00:00:00Z", "chars": 10, "sources": {"memory": 10}},
                    {"ts": None, "chars": 20, "sources": {"memory": 20}},
                    {"ts": 1_700_000_000_000, "chars": 30, "sources": {"memory": 30}},
                ]
            ),
        )
        # Only the epoch-stamped row can be DATED, and only it is inside a 100-year
        # window; the string and the None date nothing at all.
        out = usage_mod.context_trace(SLOT, 36_500)
        assert [t["blocks"]["memory"] for t in out["turns"]] == [30]

    def test_a_non_positive_block_is_dropped_from_the_row(self, monkeypatch):
        monkeypatch.setattr(
            crew_log,
            "read_slot_projection",
            _projection_with(
                [
                    {
                        "ts": 1_700_000_000_000,
                        "chars": 100,
                        "sources": {"memory": 100, "empty": 0, "negative": -5},
                    }
                ]
            ),
        )
        out = usage_mod.context_trace(SLOT, 36_500)
        assert out["turns"][0]["blocks"] == {"memory": 100}
        assert out["totals"] == {"memory": 100}

    def test_a_row_whose_every_block_is_non_positive_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            crew_log,
            "read_slot_projection",
            _projection_with([{"ts": 1_700_000_000_000, "chars": 0, "sources": {"empty": 0}}]),
        )
        assert usage_mod.context_trace(SLOT, 36_500)["turns"] == []

    def test_a_row_reports_the_total_the_writer_measured_not_the_sum_of_its_blocks(
        self, monkeypatch
    ):
        """They differ whenever a row carries omitted detail, and the total is the truth.

        Summing the blocks would under-report the prompt by exactly the sources the
        per-row cap left out, and the panel would be told the prompt shrank.
        """
        monkeypatch.setattr(
            crew_log,
            "read_slot_projection",
            _projection_with(
                [
                    {
                        "ts": 1_700_000_000_000,
                        "chars": 48_000,
                        "sources": {"memory": 1_200},
                        "sources_omitted": 39,
                    }
                ]
            ),
        )
        out = usage_mod.context_trace(SLOT, 36_500)
        assert out["turns"][0]["total_chars"] == 48_000
        # ``totals`` stays the sum of what was LISTED, because that is all this reader
        # knows a label for; the per-turn total above is what says the prompt was bigger.
        assert out["totals"] == {"memory": 1_200}

    def test_a_fold_value_that_is_not_a_context_view_reads_as_empty(self, monkeypatch):
        class _Value:
            value = {"context": "not a mapping"}

        monkeypatch.setattr(crew_log, "read_slot_projection", lambda *a, **k: _Value())
        assert usage_mod.context_trace(SLOT, 14)["turns"] == []


class TestContextTraceWindowBounds:
    """Two bounds, and the payload states both because they cut differently."""

    def test_the_day_window_still_excludes_an_older_turn(self, monkeypatch):
        _open()
        _compose({"memory": 100})
        _flush()
        # The turn is stamped now, so a window that ENDS before now excludes it. Moving
        # the reader's clock forward is what makes the row old without forging a stamp.
        real = usage_mod.datetime

        class _Later(real):  # type: ignore[misc, valid-type]
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + timedelta(days=40)

        monkeypatch.setattr(usage_mod, "datetime", _Later)
        assert usage_mod.context_trace(SLOT, 14)["turns"] == []
        assert usage_mod.context_trace(SLOT, 90)["turns"] != []

    def test_the_fold_drops_the_oldest_turns_and_the_ordinals_say_so(self, monkeypatch):
        """Oldest first off the front, and each row's own ordinal is what makes it a window.

        No dropped-turn count rides on the payload. It does not need one: the rows kept
        carry the ordinals they were assigned BEFORE the trim, so the first one's number
        states exactly how many turns precede it. A count could not do that for a reader
        bounded to a narrower window than the fold's.
        """
        monkeypatch.setattr(crew_log, "CONTEXT_TURNS_LIMIT", 4)
        _open()
        for turn in range(1, 8):
            _compose({"memory": turn}, turn=turn)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert [t["blocks"]["memory"] for t in out["turns"]] == [4, 5, 6, 7]
        # Turns 1-3 fell off, and the survivors say so by their own numbers.
        assert [t["ordinal"] for t in out["turns"]] == [4, 5, 6, 7]

    def test_a_session_inside_the_bound_starts_at_ordinal_one(self, monkeypatch):
        """Nothing was dropped, so the first row retained is the session's first turn."""
        monkeypatch.setattr(crew_log, "CONTEXT_TURNS_LIMIT", 4)
        _open()
        for turn in range(1, 4):
            _compose({"memory": turn}, turn=turn)
        _flush()
        assert [t["ordinal"] for t in usage_mod.context_trace(SLOT, 14)["turns"]] == [1, 2, 3]

    def test_truncation_preserves_the_session_start_row(self, monkeypatch):
        """The session-start injection anchors the panel and must survive the window.

        A session-start composition is the one-off injection a unit opens with, many
        times the size of a per-turn one and the row the panel draws its history and
        totals against. Trimming it like an ordinary per-turn row once a session
        passes the bound would silently drop the largest bar and understate the
        totals the reader sums from the retained rows. The trim skips it: only the
        oldest PER-TURN rows fall off, and the gap in the retained rows' ordinals is
        what states which turns went.
        """
        monkeypatch.setattr(crew_log, "CONTEXT_TURNS_LIMIT", 4)
        _open()
        _compose({"memory": 9_000}, turn=1, phase=PHASE_SESSION_START)
        for turn in range(2, 8):
            _compose({"memory": turn}, turn=turn, phase=PHASE_PER_TURN)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        # The session-start row is still there (oldest), then the newest 3 per-turn
        # rows fill the window of 4. Three per-turn rows (turns 2-4) were dropped.
        assert [t["phase"] for t in out["turns"]] == [
            "session_start",
            "per_turn",
            "per_turn",
            "per_turn",
        ]
        assert [t["blocks"]["memory"] for t in out["turns"]] == [9_000, 5, 6, 7]
        # Each retained row carries its EXACT ordinal: the session-start is turn 1 and
        # the three survivors are turns 5, 6, 7. The gap (2, 3, 4) IS the record of what
        # was dropped, which is why no separate count is needed.
        assert [t["ordinal"] for t in out["turns"]] == [1, 5, 6, 7]

    def test_dropped_middle_turns_show_in_the_ordinal_gap(self, monkeypatch):
        """A per-turn row dropped BETWEEN the session-start and the survivors is hidden.

        The session-start row (ordinal 1) is preserved, so nothing precedes it and the
        backend's before-count is 0 -- but turns 2-4 did run and are gone. They are not
        lost: the survivors carry their exact ordinals (5, 6, 7), so the gap after 1 is
        exactly the hidden turns, which the panel counts without a separate signal.
        """
        monkeypatch.setattr(crew_log, "CONTEXT_TURNS_LIMIT", 4)
        _open()
        _compose({"memory": 9_000}, turn=1, phase=PHASE_SESSION_START)
        for turn in range(2, 8):
            _compose({"memory": turn}, turn=turn, phase=PHASE_PER_TURN)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        by_phase = {(t["phase"], t["ordinal"]) for t in rows}
        assert ("session_start", 1) in by_phase
        # The survivors are ordinals 5, 6, 7 -- turns 2, 3, 4 are absent from the window.
        assert {t["ordinal"] for t in rows if t["phase"] == "per_turn"} == {5, 6, 7}

    def test_a_turn_that_composed_nothing_still_advances_the_ordinal(self):
        """MUTATION-SENSITIVE: a turn that composed nothing occupies a turn-history slot.

        The ordinal is the row's position in the TURN history, not a count of the turns
        that happened to compose. A turn can close having composed nothing -- a refusal,
        or a turn that reused the prompt already in context -- and it still ran. If the
        counter only moved on compositions, the composition AFTER a composition-less turn
        would carry an ordinal one short of its real turn number, and the panel would
        show the wrong "Turn N" and the wrong "earlier turns not shown" count.

        Turn 1 composes (ordinal 1), turn 2 closes having composed NOTHING, turn 3
        composes. The turn-3 row must read ordinal 3, not 2 -- turn 2 consumed a slot.
        """
        _open()
        _compose({"memory": 10}, turn=1)
        _billed(turn=2, used=0)  # a turn that closed without composing a row
        _compose({"memory": 30}, turn=3)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        # Two composition rows survive (turn 2 wrote none), but their ordinals count the
        # composition-less turn between them: 1 and 3, never 1 and 2.
        assert [t["ordinal"] for t in rows] == [1, 3]

    def test_a_retried_composition_less_turn_takes_ONE_ordinal(self):
        """MUTATION-SENSITIVE: attempts of one turn share its ordinal, not one each.

        One turn is closed once per ATTEMPT -- that is what the per-attempt seal in the
        closer exists for -- so 'did this turn compose' is answered afresh at every
        attempt. A composition-less turn that is retried would then be handed an ordinal
        per attempt, and every later row would carry a turn number too high, with the
        "earlier turns not shown" count inflated by the same amount. The counter is
        marked when an ordinal is CONSUMED, which makes the second attempt a no-op.

        Turn 1 composes (ordinal 1), turn 2 composes nothing and closes TWICE, turn 3
        composes. The turn-3 row must read ordinal 3 -- never 4.
        """
        _open()
        _compose({"memory": 10}, turn=1)
        _billed(turn=2, used=0)
        _billed(turn=2, used=0)
        _compose({"memory": 30}, turn=3)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        assert [t["ordinal"] for t in rows] == [1, 3]

    def test_consecutive_composition_less_turns_each_take_an_ordinal(self):
        """Two turns in a row composing nothing advance the ordinal twice, not once.

        A single boolean 'this turn composed' flag has to be RESET at each turn close,
        or a run of composition-less turns would advance the ordinal only once for the
        whole run. Turn 1 composes (1), turns 2 and 3 compose nothing, turn 4 composes:
        the turn-4 row must read ordinal 4.
        """
        _open()
        _compose({"memory": 10}, turn=1)
        _billed(turn=2, used=0)
        _billed(turn=3, used=0)
        _compose({"memory": 40}, turn=4)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        assert [t["ordinal"] for t in rows] == [1, 4]

    def test_repeated_compositions_of_one_turn_share_one_ordinal(self):
        """MUTATION-SENSITIVE: a turn composed more than once is still ONE turn.

        A single turn can compose more than once -- a rebuild re-emits the session-start
        injection, a prompt is recomposed within the turn -- and every such row is the
        SAME turn, so it carries that turn's one ordinal. Advancing the counter per
        composition instead of per distinct (unit, turn) hands the second row a fresh
        number, pushing the counter past the real turn sequence so every later row's turn
        number and the panel's "earlier turns not shown" count read one too high per
        extra composition.

        Turn 1 composes TWICE, then turn 2 composes once. The two turn-1 rows must share
        ordinal 1, and the turn-2 row must read ordinal 2 -- never 2 and 3.
        """
        _open()
        _compose({"memory": 100}, turn=1)
        _compose({"memory": 50}, turn=1)
        _billed(turn=1, used=6_000)
        _compose({"memory": 40}, turn=2)
        _billed(turn=2, used=7_000)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        # Three rows: two for turn 1 (one ordinal between them), one for turn 2.
        assert [t["ordinal"] for t in rows] == [1, 1, 2]

    def test_a_refused_turn_consumes_an_ordinal(self):
        """MUTATION-SENSITIVE: a turn a dispatch gate refused still occupied the history.

        A refused turn never reaches the model, so it emits no ``turn/completed`` and
        composes nothing -- its ``turn/refused`` is its only durable entry. A dispatch
        gate declining a turn (an oversized or blocked ``@prompt``, a superseded replay)
        is ordinary operation, so if the fold does not account for it the next
        composition's ordinal -- and the panel's "earlier turns not shown" count derived
        from it -- reads one low per refusal, and nothing self-corrects.

        Turn 1 composes (ordinal 1), turn 2 is REFUSED, turn 3 composes. The turn-3 row
        must read ordinal 3 -- never 2.
        """
        _open()
        _compose({"memory": 100}, turn=1)
        _billed(turn=1, used=5_000)
        _refused(turn=2)
        _compose({"memory": 40}, turn=3)
        _billed(turn=3, used=6_000)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        # Two composition rows (turn 2 wrote none); their ordinals count the refused
        # turn between them: 1 and 3, never 1 and 2.
        assert [t["ordinal"] for t in rows] == [1, 3]

    def test_a_refused_turn_counts_once_even_retried(self):
        """A turn refused more than once (e.g. a retried oversized prompt) takes ONE.

        The accounting is keyed on the turn identity, not on the refusal event, so a
        turn refused twice consumes a single ordinal -- the same once-per-(unit, turn)
        rule a composition-less close obeys. Turn 2 is refused TWICE between turn 1's
        and turn 3's compositions; the turn-3 row must still read ordinal 3, never 4.
        """
        _open()
        _compose({"memory": 100}, turn=1)
        _refused(turn=2)
        _refused(turn=2)
        _compose({"memory": 40}, turn=3)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        assert [t["ordinal"] for t in rows] == [1, 3]

    def test_a_rewound_turn_reuses_its_ordinal_not_a_new_one(self):
        """A turn number is not monotonic within a unit: an edit-resend or a rewind
        truncates the row list the panel numbers from, so a turn already counted recurs
        at a lower number. That recurrence must reuse the turn's ORIGINAL ordinal, not
        advance the counter again -- otherwise the rewound turn is relabelled and the
        dropped-turn count inflates. Turns 1, 2, 3 compose (ordinals 1, 2, 3); turn 2
        then composes AGAIN (the resend). Its row must read ordinal 2, never 4, and the
        counter must not have advanced past 3.
        """
        _open()
        _compose({"memory": 100}, turn=1)
        _compose({"memory": 90}, turn=2)
        _compose({"memory": 80}, turn=3)
        # The edit-resend re-runs turn 2 in the same unit -- a lower turn number recurs.
        _compose({"memory": 70}, turn=2)
        _flush()
        rows = usage_mod.context_trace(SLOT, 14)["turns"]
        # The rendered row exposes no turn number; key on each composition's distinct
        # memory block instead. Both compositions of turn 2 (90 and 70) carry ordinal 2;
        # turn 3 keeps ordinal 3, and the counter never advanced past 3.
        by_memory = {t["blocks"]["memory"]: t["ordinal"] for t in rows}
        assert by_memory[100] == 1
        assert by_memory[90] == 2
        assert by_memory[70] == 2
        assert by_memory[80] == 3
        assert max(t["ordinal"] for t in rows) == 3

    #: The slot-fold cache's own ceiling, stated in ``projection.py`` against that
    #: cache's largest budgeted member (``radar``). A full context window has to fit
    #: under it or the cache's documented budget is wrong.
    CACHE_MEMBER_CEILING = 995_342

    def test_a_full_window_stays_under_the_slot_fold_cell_budget(self):
        """The figure ``CONTEXT_TURNS_LIMIT``'s comment states, re-measured here.

        Built at the CAP, not at a realistic row: every limit at its maximum, so what
        is measured is the largest state this fold can reach. A 40-source row measures
        511,192 and passes with room to spare, which would leave the real ceiling
        untested -- the cap is 64 sources and the labels can run to ``TEXT_LIMIT``.

        Values are DISTINCT per row, and the second assertion is what KEEPS them so.
        One shared int literal across every row understates the window by 45%
        (439,084 against 808,912), because the measurement counts each object once
        while the real fold holds a separately parsed int per source per turn -- and
        it understates it DOWNWARD, so a ceiling check alone still passes and the
        guard goes quiet. Comparing the two builds catches that as a failure instead:
        the moment the distinct build stops being distinct, the two measure the same.
        """
        measured = _deep_bytes(_worst_case_window()) + _deep_bytes(_worst_case_turn_keys())
        assert measured < self.CACHE_MEMBER_CEILING, (
            f"a full context window plus a full ordinal memo measures {measured:,} "
            f"bytes, over the {self.CACHE_MEMBER_CEILING:,} slot-fold cache-member ceiling"
        )
        shared = _deep_bytes(_worst_case_window(distinct_values=False)) + _deep_bytes(
            _worst_case_turn_keys()
        )
        assert measured > shared, (
            f"the window above measures {measured:,} bytes, no more than the "
            f"{shared:,} of one built with a single shared value -- so it is no "
            "longer measuring distinct per-source values and understates the real "
            "fold. Restore the distinct values rather than relaxing this."
        )

    def test_the_rejected_row_shape_would_not_have_fit(self):
        """Why the row stores ``label -> chars`` and not the entry's own source list.

        The reason is a measurement, so it is measured: the same worst-case window
        built as the entry's ``[{kind, chars, tokens}]`` list breaches the very ceiling
        the shipped shape fits under. That makes the shape a requirement rather than a
        preference, and pins it against a future change back.
        """
        measured = _deep_bytes(_worst_case_window(as_entry_list=True))
        assert measured > self.CACHE_MEMBER_CEILING, (
            f"the entry's own list shape measures {measured:,} bytes, which now FITS "
            f"under the {self.CACHE_MEMBER_CEILING:,} ceiling -- the stated reason for "
            "the row shape no longer holds, so re-derive it rather than keeping the "
            "comment"
        )


def _worst_case_turn_keys() -> str:
    """A FULL ordinal memo, the other large member of the ``usage`` fold state.

    ``CONTEXT_ORDINAL_MEMO_LIMIT`` keys, each a 3-digit unit and a 7-digit turn: wider
    than a real slot reaches (the turn is a row count, and the live list holds 10,000).
    """
    return "," + "".join(
        f"{100 + n % 900}:{1_000_000 + n}," for n in range(crew_log.CONTEXT_ORDINAL_MEMO_LIMIT)
    )


def _worst_case_window(
    *, distinct_values: bool = True, as_entry_list: bool = False
) -> list[dict[str, Any]]:
    """A full ``context.turns`` window with every limit at its maximum.

    One builder for the three measurements the budget tests compare, so they cannot
    drift into measuring different windows and reporting the difference as a size
    result. ``as_entry_list`` builds the REJECTED row shape -- the entry's own
    ``[{kind, chars, tokens}]`` -- for the comparison that decided against it.

    Labels are shared across rows because the real vocabulary recurs every turn.
    Values are not: each is separately parsed in the real fold, so ``distinct_values``
    builds them distinct, and passing ``False`` produces the understated build the
    budget test compares against.
    """
    labels = [
        f"src{i}".ljust(crew_log.TEXT_LIMIT - 1, "x")
        for i in range(crew_log.CONTEXT_SOURCES_PER_TURN_LIMIT)
    ]
    window: list[dict[str, Any]] = []
    for n in range(crew_log.CONTEXT_TURNS_LIMIT):
        sources: Any
        if as_entry_list:
            sources = [
                {
                    "kind": label,
                    "chars": int(f"{n + 1000}{j:03d}"),
                    "tokens": int(f"{n + 2000}{j:03d}"),
                }
                for j, label in enumerate(labels)
            ]
        elif distinct_values:
            sources = {label: int(f"{n + 1000}{j:03d}") for j, label in enumerate(labels)}
        else:
            sources = {label: 1200 for label in labels}
        window.append(
            {
                "turn": n,
                "ts": 1_700_000_000_000 + n * 1000,
                "chars": 48_000 + n,
                "tokens": 12_000 + n,
                "tokens_estimated": True,
                "sources": sources,
                "sources_omitted": 0,
                "phase": PHASE_PER_TURN,
                "model": "claude-opus-4-5-20260101",
                "window": 200_000,
                "step": 3,
            }
        )
    return window


def _deep_bytes(obj: Any, seen: set[int] | None = None) -> int:
    """Bytes *obj* holds, counting each shared object once."""
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            size += _deep_bytes(key, seen) + _deep_bytes(value, seen)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            size += _deep_bytes(item, seen)
    return size


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

    def test_the_payload_matches_the_scans_shape_field_for_field(self):
        _open(window=200_000, model="opus-5")
        _compose(
            {"memory": 30_000, "lessons": 5_000, USER_LABEL: 120}, turn=1, phase=PHASE_SESSION_START
        )
        _billed(turn=1, used=9_000)
        _compose({USER_LABEL: 340, "surface": 200}, turn=2, phase=PHASE_PER_TURN)
        _billed(turn=2, used=12_500)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)

        # Everything except the two stamps, which the scan took from the row's own
        # ISO string and this reader derives from the entry's epoch stamp.
        stamps = [t.pop("ts") for t in out["turns"]]
        assert out == {
            "slot": "chat-1",
            "turns": [
                {
                    "phase": "session_start",
                    "blocks": {"memory": 30_000, "lessons": 5_000, "your_message": 120},
                    "total_chars": 35_120,
                    "context_used": 9_000,
                    "context_window": 200_000,
                    "model": "opus-5",
                    "ordinal": 1,
                },
                {
                    "phase": "per_turn",
                    "blocks": {"your_message": 340, "surface": 200},
                    "total_chars": 540,
                    "context_used": 12_500,
                    "context_window": 200_000,
                    "model": "opus-5",
                    "ordinal": 2,
                },
            ],
            "totals": {"memory": 30_000, "lessons": 5_000, "your_message": 460, "surface": 200},
            "injected_chars": 35_660,
            "user_chars": 460,
            "peak_context_used": 12_500,
            "context_window": 200_000,
            "window_days": 14,
        }
        # A stamp is an ISO-8601 UTC string, which is what the declared shape says and
        # what the scan's rows carried.
        for stamp in stamps:
            assert datetime.fromisoformat(stamp).tzinfo is not None

    def test_the_occupancy_numbers_match_the_scans_meaning(self):
        """Both are the provider's figures, taken over the window, as the scan took them."""
        _open(window=200_000)
        _compose({"memory": 100})
        _billed(used=7_500, window=200_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        assert out["turns"][0]["context_used"] == 7_500
        assert out["peak_context_used"] == 7_500
        assert out["context_window"] == 200_000

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
        assert out["turns"] == []
        assert out["peak_context_used"] == 0, (
            "the peak must come from the rows inside the requested window, not from "
            "the whole session"
        )
        # With no reading in the span, the configured size is still what a reader can
        # be told the session runs under.
        assert out["context_window"] == 200_000


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

    def test_every_field_the_trace_interface_declares_is_sent(self):
        _open(window=200_000)
        _compose({"memory": 100, USER_LABEL: 10})
        _billed(used=1_000)
        _flush()
        out = usage_mod.context_trace(SLOT, 14)
        declared = self._declared("ContextTrace")
        # The control: the extraction found a real interface, not an empty string.
        assert "turns" in declared and "peak_context_used" in declared, declared
        assert declared <= set(
            out
        ), f"the panel declares fields this reader omits: {declared - set(out)}"

    def test_every_field_a_turn_declares_is_sent(self):
        _open(window=200_000)
        _compose({"memory": 100}, phase=PHASE_SESSION_START)
        _flush()
        turn = usage_mod.context_trace(SLOT, 14)["turns"][0]
        declared = self._declared("ContextTurn")
        assert "blocks" in declared and "phase" in declared, declared
        assert declared <= set(
            turn
        ), f"the panel declares turn fields this reader omits: {declared - set(turn)}"


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
    async def test_a_dashboard_caller_still_reads_the_trace(self):
        _open()
        _compose({"memory": 100})
        _flush()
        async with TestClient(TestServer(self._app(""))) as client:
            resp = await client.get(f"/api/telemetry/context-trace?slot={SLOT}")
            assert resp.status == 200
            body = await resp.json()
            assert body["turns"][0]["blocks"]["memory"] == 100
