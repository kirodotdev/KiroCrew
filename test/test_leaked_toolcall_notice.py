"""Acceptance tests for the leaked tool-call notice.

The bug: the model emits an invoke-block tool invocation into its TEXT channel
instead of executing it (observed when the target is a deferred MCP tool whose
schema is not bound, and with large nested arguments), the turn ends with zero
tool calls, and the session silently stalls — in a monitor/autonudge loop the
user only discovers it by noticing nothing happened.

The fix is NOTICE-ONLY, deliberately: an injected "re-issue that call"
continuation would carry runtime authority into sessions where the call
auto-approves (slot trust, global yolo, or a static agent tool allowlist — the
last invisible at the runner layer, so no fail-closed downgrade condition
exists), and the leaked block may be untrusted external content the model
merely reproduced. So the leaked turn is marked un-landed and the user gets a
visible card; nothing is queued and nothing can execute.

These tests exercise the pure detector (``has_leaked_tool_call``) and the
gating decision (``should_notice_leaked_tool_call``) directly, matching the
sibling promise-only suite.

The machine-syntax fixtures are ASSEMBLED from fragments rather than written
as literals: a raw invoke block in this file is exactly the byte sequence
agent tool-call parsers trip over (the defect under test), so spelling it out
verbatim makes the file itself hazardous to quote.
"""

from __future__ import annotations

import pathlib
import re

from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN
from kiro_crew.dashboard.chat_utils import (
    has_leaked_tool_call,
    should_notice_compaction_dropped_leak,
    should_notice_leaked_tool_call,
    should_notice_mixed_turn_leak,
)

_END = STOP_REASON_END_TURN

# Tag fragments, assembled at import time (see module docstring).
_INV_OPEN = "<" + 'invoke name="spawn_run">'
_INV_OPEN_SQ = "<" + "invoke name='spawn_run'>"
_INV_CLOSE = "</" + "invoke>"
_INV_OPEN_NS = "<" + 'antml:invoke name="spawn_run">'
_INV_CLOSE_NS = "</" + "antml:invoke>"


def _param(name: str, value: str) -> str:
    return "<" + f'parameter name="{name}">' + value + "</" + "parameter>"


# The issue's verbatim leak shape: the invocation written into the reply text.
_LEAK = (
    "call "
    + _INV_OPEN
    + " "
    + _param("__tool_use_purpose", "Loop cycle 5: reconcile the CR queue ...")
    + " "
    + _param("agent", "btdocs-ops")
    + " "
    + _param("task", "Oncall reconciliation pass ...")
    + " "
    + _INV_CLOSE
)


def _notice(**over):
    """should_notice_leaked_tool_call with leak-firing defaults, so each test
    flips exactly the one field it is about."""
    kw = dict(
        stop_reason=_END,
        end_turn_reason=_END,
        final_segment_text=_LEAK,
        prompt_depth=0,
        is_cancelled=False,
        refusal_reasons=[],
        turn_tool_calls=0,
    )
    kw.update(over)
    return should_notice_leaked_tool_call(**kw)


# ── Detector: what must match ──


def test_verbatim_issue_leak_is_detected():
    assert has_leaked_tool_call(_LEAK)
    assert _notice() is True


def test_multiline_leak_with_nested_arguments_is_detected():
    # The reported leaks carried large multi-paragraph task arguments with
    # URLs, ids, and bulleted instructions of their own.
    text = (
        "I'll dispatch the swarm.\n\n"
        + _INV_OPEN
        + "\n"
        + _param(
            "task",
            "Review PR #123:\n- read the diff\n- check CI\nURLs: https://example.test/pr/123",
        )
        + "\n"
        + _INV_CLOSE
        + "\n"
    )
    assert has_leaked_tool_call(text)


def test_namespace_prefixed_and_single_quoted_forms_are_detected():
    assert has_leaked_tool_call(_INV_OPEN_NS + _param("task", "x") + _INV_CLOSE_NS)
    assert has_leaked_tool_call(_INV_OPEN_SQ + _param("task", "x") + _INV_CLOSE)


def test_open_tag_with_close_but_no_parameter_is_detected():
    # A zero-argument call still leaks as an open+close pair.
    assert has_leaked_tool_call("run it: " + _INV_OPEN + _INV_CLOSE)


# ── Detector: what must NOT match ──


def test_fenced_code_block_is_quoted_content_not_a_leak():
    # A user pasting a leak transcript (or the model explaining the bug) puts
    # the block in a fence; that is quoted syntax, never a dispatch intent.
    assert not has_leaked_tool_call("Here is the leak I saw:\n\n```\n" + _LEAK + "\n```\n")


def test_tilde_fence_and_long_backtick_fence_are_quoted_content():
    # Markdown accepts ~~~ fences and fences longer than three backticks; both
    # are quoting, same as the plain triple-backtick form.
    assert not has_leaked_tool_call("Quoted:\n~~~\n" + _LEAK + "\n~~~\n")
    assert not has_leaked_tool_call("Quoted:\n````\n" + _LEAK + "\n````\n")


def test_longer_fence_carries_shorter_fence_content():
    # CommonMark: a closer must be at least as long as its opener. With a
    # SINGLE shorter run inside the quote, a wrong ">=3 closes anything"
    # pairing would end the fence at the inner run and expose the quoted
    # leak after it as unfenced text — the false-notice shape.
    text = "Quoted:\n````\nan inner ``` run, then the pasted leak:\n" + _LEAK + "\n````\ntail"
    assert not has_leaked_tool_call(text)


def test_corroboration_before_the_opener_does_not_count():
    # A stray parameter/close tag BEFORE a lone opener is unrelated markup,
    # not this invocation's body — corroboration must follow the opener.
    stray_close = "</" + "invoke>"
    assert not has_leaked_tool_call(stray_close + " earlier markup, then " + _INV_OPEN)


def test_multi_backtick_inline_span_is_quoted_content():
    # An inline span delimited by a matching run of 2+ backticks (the escape
    # for content that itself contains a backtick) is quoting too.
    assert not has_leaked_tool_call(
        "The model printed `` " + _INV_OPEN + _INV_CLOSE + " `` as text."
    )


def test_inline_code_span_is_quoted_content_not_a_leak():
    assert not has_leaked_tool_call("The model printed `" + _INV_OPEN + _INV_CLOSE + "` as text.")


def test_lone_open_tag_without_body_is_not_a_leak():
    # A truncated quote or typo'd example: no parameter tag, no close tag.
    assert not has_leaked_tool_call("it emitted " + _INV_OPEN + " and stopped")


def test_prose_and_empty_text_are_not_leaks():
    assert not has_leaked_tool_call("")
    assert not has_leaked_tool_call("I'll invoke the tool now.")
    assert not has_leaked_tool_call("Use spawn_run with name=spawn_run to dispatch.")


def test_unpaired_fence_fails_toward_detection_not_suppression():
    # Only PAIRED fences are stripped: an unpaired fence inside a genuinely
    # leaked payload must not hide the surrounding invoke tags.
    text = _INV_OPEN + _param("task", "```py\nprint(1)") + _INV_CLOSE
    assert has_leaked_tool_call(text)


def test_leak_after_an_unpaired_giant_delimiter_run_is_still_detected():
    # An unpaired run of any length is literal text; the leak after it must
    # stay visible (the linear scanner keeps unclosed-fence content in place).
    assert has_leaked_tool_call("`" * 5000 + "\n" + _LEAK)
    assert has_leaked_tool_call("~" * 5000 + "\n" + _LEAK)


def test_adversarial_delimiter_runs_scan_in_linear_time():
    # The scan runs on the event loop at every turn completion, so it must
    # stay linear on ADVERSARIAL model-authored text: the previous
    # backreference regex took seconds at a few thousand consecutive
    # backticks (superlinear backtracking) — long enough for the liveness
    # watchdog to kill the gateway. The ceiling is generous by orders of
    # magnitude for a linear pass (microseconds); only a reintroduced
    # backtracking pattern can approach it (the old regex needed >6s at
    # 8k characters and grew ~8x per doubling).
    import time

    for payload in (
        "`" * 50_000,
        "`` " * 20_000,
        ("`a" * 30_000),
        ("```x~~~y" * 10_000),
    ):
        start = time.perf_counter()
        has_leaked_tool_call(payload + " tail text")
        assert time.perf_counter() - start < 2.0


# ── Gates (each test flips exactly one) ──


def test_a_turn_that_made_tool_calls_never_notices():
    # Not because a tool-heavy turn is a different shape — it is the same leak —
    # but because THIS path un-lands the turn, and a turn whose earlier calls had
    # side effects must not be marked unacted. That shape is noticed without
    # un-landing by should_notice_mixed_turn_leak, covered below.
    assert _notice(turn_tool_calls=1) is False


def test_cancelled_and_refused_turns_never_notice():
    assert _notice(is_cancelled=True, stop_reason=STOP_REASON_CANCELLED) is False
    assert _notice(refusal_reasons=["blocked"]) is False


def test_non_end_turn_stop_reasons_never_notice():
    # Error/stall exits own their own reporting paths.
    assert _notice(stop_reason="error: tool stall") is False


def test_nested_prompts_never_notice():
    assert _notice(prompt_depth=1) is False


def test_stage_execution_turns_never_notice():
    # The orchestrator's stage loop reads the turn result for stage accounting;
    # un-landing a stage turn would record an unfinished stage as complete.
    assert _notice(in_stage_execution=True) is False


def test_non_leak_text_never_notices():
    assert _notice(final_segment_text="All done — the queue is empty.") is False


# ── Mixed turn: dispatched tools, THEN leaked its final dispatch ──


def _mixed(**over):
    """should_notice_mixed_turn_leak with firing defaults, so each test flips
    exactly the one field it is about."""
    kw = dict(
        stop_reason=_END,
        end_turn_reason=_END,
        final_segment_text=_LEAK,
        prompt_depth=0,
        turn_tool_calls=4,
    )
    kw.update(over)
    return should_notice_mixed_turn_leak(**kw)


def test_a_turn_that_dispatched_tools_then_leaked_its_last_call_is_noticed():
    """The observed shape: read state over several calls, announce the write,
    leak the write itself. The turn lands looking like a completed action, so
    there is no stall to notice and no missing output to explain — which is why
    the sibling's silence here read as success.
    """
    assert _mixed() is True


def test_the_two_predicates_partition_by_tool_count():
    """Neither shape may fall through both, and neither may claim the other's.

    The runner chains them as if/elif, so an overlap would be an ordering bug
    and a gap would be a silent stall.
    """
    assert _notice(turn_tool_calls=0) is True
    assert _mixed(turn_tool_calls=0) is False
    assert _notice(turn_tool_calls=2) is False
    assert _mixed(turn_tool_calls=2) is True


def test_a_cancelled_mixed_turn_is_not_noticed():
    """Excluded via the stop reason rather than an is_cancelled flag: a cancelled
    turn never reports end_turn, and the user already knows they cancelled it.
    """
    assert _mixed(stop_reason=STOP_REASON_CANCELLED) is False


def test_non_end_turn_and_nested_mixed_turns_are_not_noticed():
    assert _mixed(stop_reason="error: tool stall") is False
    assert _mixed(prompt_depth=1) is False


def test_a_mixed_turn_whose_text_is_prose_is_not_noticed():
    # The count alone must never fire the card: it takes an actual leak.
    assert _mixed(final_segment_text="Read four resources; all consistent.") is False


def test_a_mixed_turn_quoting_a_fenced_block_is_not_noticed():
    # Same structural exclusion the sibling gets: explaining a leak is not one.
    fenced = "Here is what a leak looks like:\n\n```\n" + _LEAK + "\n```\n"
    assert _mixed(final_segment_text=fenced) is False


# ── The compaction boundary ───────────────────────────────────────────────
# A REAL (non-synthesized) mid-turn compaction terminal is a segment boundary,
# so the runner resets `assistant_text` there. Both predicates above read that
# accumulator AT TURN END, so a leak that streamed BEFORE the boundary is gone
# by the time either runs and both decline on an empty segment — while the raw
# block was already on the user's screen (chunks stream to the wire as they
# arrive) and the boundary does not flush it anywhere. The fact is therefore
# captured at the boundary and only the boolean travels.


def _dropped(**over):
    """should_notice_compaction_dropped_leak with firing defaults, so each test
    flips exactly the one field it is about."""
    kw = dict(
        dropped_leak=True,
        leak_already_noticed=False,
        stop_reason=_END,
        end_turn_reason=_END,
        prompt_depth=0,
        is_cancelled=False,
        refusal_reasons=[],
    )
    kw.update(over)
    return should_notice_compaction_dropped_leak(**kw)


def test_both_turn_end_predicates_are_blind_to_a_wiped_segment():
    """The defect itself, stated as the gap it is.

    This is what the boundary leaves behind: the leak really happened and
    really reached the user, but the only input either turn-end predicate reads
    is now empty, so neither can report it. Whatever the tool count was, the
    answer is the same — which is why the fact has to be captured earlier.
    """
    wiped = ""
    assert has_leaked_tool_call(wiped) is False
    assert _notice(final_segment_text=wiped, turn_tool_calls=0) is False
    assert _mixed(final_segment_text=wiped, turn_tool_calls=4) is False


def test_a_leak_dropped_at_the_compaction_boundary_is_noticed():
    """The fix: the turn-end card comes from the recorded fact, not the text."""
    assert _dropped() is True


def test_the_leak_is_detectable_at_the_boundary_before_the_reset():
    """What the runner scans: the accumulator as it stands at the boundary.

    Pins the pairing the fix depends on — the same detector the turn-end
    predicates use, run one step earlier, is what makes the recorded fact true.
    """
    assert has_leaked_tool_call(_LEAK) is True


def test_a_clean_segment_at_the_boundary_records_nothing():
    """A compaction alone is not a leak. Only a segment carrying one is."""
    assert has_leaked_tool_call("Summarizing the session so far.") is False
    assert _dropped(dropped_leak=False) is False


def test_a_dropped_leak_needs_no_tool_count():
    """The signature is a closed set, and each absence is load-bearing.

    ``turn_tool_calls`` is absent because that gate exists on the zero-call
    sibling to protect UN-LANDING, and this predicate un-lands nothing.
    ``in_stage_execution`` is absent for the reason its mixed-turn sibling omits
    it: a notice changes no turn result the stage loop reads.
    ``final_segment_text`` is absent because the text is already gone at turn
    end, which is the defect -- the boundary fact travels instead.

    The closed set is the load-bearing half. Nothing here can describe a
    recovery's trigger (no tool result, no infra-error state, no progress-claim
    text), so this predicate cannot be made to stand down for a recovery even in
    principle -- which is precisely why it must not hold an exclusive slot ahead
    of one, and why ``TestTheNoticeDoesNotStarveARecovery`` pins that placement.
    """
    import inspect

    params = set(inspect.signature(should_notice_compaction_dropped_leak).parameters)
    assert params == {
        "dropped_leak",
        "leak_already_noticed",
        "stop_reason",
        "end_turn_reason",
        "prompt_depth",
        "is_cancelled",
        "refusal_reasons",
    }


def test_cancelled_refused_and_nested_dropped_leaks_are_not_noticed():
    """The same turn-shape exclusions its siblings use, for the same reasons:
    those paths own their own reporting, and a nested prompt is not the user's
    turn.
    """
    assert _dropped(is_cancelled=True) is False
    assert _dropped(stop_reason=STOP_REASON_CANCELLED) is False
    assert _dropped(refusal_reasons=["tool_denied"]) is False
    assert _dropped(stop_reason="error: tool stall") is False
    assert _dropped(prompt_depth=1) is False


def test_the_dropped_leak_card_does_not_double_up_on_a_sibling():
    """One turn gets one leak card, decided by the flag rather than by ordering.

    The caller evaluates this predicate OUTSIDE the chain its siblings sit in,
    so position cannot be what prevents a second card. A turn whose
    post-boundary segment also leaks is carded by a sibling, which sets the
    flag; this predicate then declines on the same recorded boundary fact.
    """
    # Post-boundary segment leaks, nothing ran after the boundary.
    assert _notice(final_segment_text=_LEAK, turn_tool_calls=0) is True
    # Post-boundary segment leaks after dispatches earlier in the turn.
    assert _mixed(final_segment_text=_LEAK, turn_tool_calls=4) is True
    # Either of those carding is what silences this one -- not its position.
    assert _dropped(leak_already_noticed=True) is False
    assert _dropped(leak_already_noticed=False) is True


def test_the_card_owns_no_turn_outcome_so_it_gates_on_nothing_a_recovery_reads():
    """A recovery-shaped turn that also dropped a leak gets BOTH.

    The predicate's answer is unchanged by anything a recovery keys on, so
    evaluating it independently is safe in both directions: the card fires, and
    the arm that owns the turn still gives the turn its outcome.
    """
    assert _dropped() is True
    assert _dropped(leak_already_noticed=True) is False


class TestTheNoticeDoesNotStarveARecovery:
    """Source-level guard: the card must stay OUT of the outcome-owning chain.

    The chain that ends a turn runs the two leak arms, the L1 infrastructure
    retry and the promise-only guard as one ``if``/``elif``. The last two
    RE-DRIVE the work, so an arm placed among them takes an exclusive slot: a
    turn that both dropped a leak at its boundary and needs a recovery would get
    the card and lose the recovery. A notice owns no outcome and so belongs
    outside, which is a structural property no predicate test can observe.
    """

    RUNNER = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/chat_runner.py"
    CALL = "should_notice_compaction_dropped_leak("

    def _call_line(self) -> str:
        for line in self.RUNNER.read_text(encoding="utf-8").splitlines():
            if self.CALL in line:
                return line
        raise AssertionError("the runner never calls the dropped-leak predicate")

    def test_the_card_is_reached_by_its_own_if_not_by_an_elif(self):
        line = self._call_line().strip()
        assert line.startswith("if "), (
            "the dropped-leak notice is chained with `elif`, so it takes an "
            "exclusive slot in the turn-outcome chain and a turn that also needs "
            "the L1 infra retry or the promise-only recovery loses it. A "
            f"notice-only card belongs in its own `if`. Found: {line!r}"
        )

    def test_both_recovery_arms_still_have_their_own_conditions(self):
        """The premise: those two arms are recoveries this card could shadow."""
        source = self.RUNNER.read_text(encoding="utf-8")
        assert "should_recover_promise_only(" in source
        assert "_queue_recovery(" in source

    def test_the_card_sets_no_flag_a_recovery_reads(self):
        """`_noticed_leak` suppresses recovery arms, so this card must not set it."""
        lines = self.RUNNER.read_text(encoding="utf-8").splitlines()
        start = next(i for i, ln in enumerate(lines) if self.CALL in ln)
        # The card's own block: from its call to the end of its slot.append.
        window = "\n".join(lines[start : start + 40])
        assert "_noticed_leak = True" not in window, (
            "the dropped-leak card marks the turn un-landed, which suppresses the "
            "recovery arms it was just decoupled from"
        )


class TestTheBoundaryScanRunsBeforeTheReset:
    """Source-level guard over the runner half, which no unit test can reach.

    The predicate above takes a boolean, so the whole fix rests on WHERE that
    boolean is computed: the scan reads ``assistant_text`` and the compaction
    boundary clears it, so a scan placed after the clear reads an empty string,
    records False, and silently restores the defect while every predicate test
    here still passes. Same doctrine as
    ``test_deny_audit_first.TestEveryDenySiteAuditsBeforeTheWire``: the ordering
    claim has to be checkable.

    Scoped to the compaction boundary alone. The runner clears
    ``assistant_text`` at several other sites (tool boundaries, clear, steer
    cut) and those are not segment-summarization boundaries, so they are
    deliberately out of scope -- the window is bounded by the
    ``event.synthesized`` guard that identifies this one.
    """

    RUNNER = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/chat_runner.py"
    GUARD = re.compile(r"^\s*if not event\.synthesized:")
    RESET = re.compile(r'^\s*assistant_text = ""\s*$')
    SCAN = "has_leaked_tool_call("
    FLAG = "_compaction_dropped_leak"
    # The assignment must ACCUMULATE. A turn can cross more than one boundary,
    # and a plain `= has_leaked_tool_call(...)` lets a later clean segment
    # overwrite an earlier boundary's recorded leak, losing a card the user is
    # owed. Matching the `or` is what makes that rewrite fail here.
    ACCUMULATE = re.compile(r"_compaction_dropped_leak\s*=\s*_compaction_dropped_leak\s+or\b")

    def _lines(self) -> list[str]:
        return self.RUNNER.read_text(encoding="utf-8").splitlines()

    def _boundary(self, lines: list[str]) -> int:
        sites = [i for i, line in enumerate(lines) if self.GUARD.match(line)]
        assert len(sites) == 1, (
            f"expected exactly one `if not event.synthesized:` guard, saw {len(sites)} "
            "-- the compaction-boundary window is no longer identifiable, so this "
            "ratchet cannot say which reset it is guarding"
        )
        return sites[0]

    def test_the_boundary_window_still_resets_the_accumulator(self):
        """The premise: this window is where the segment text is discarded."""
        lines = self._lines()
        start = self._boundary(lines)
        window = lines[start : start + 40]
        assert any(self.RESET.match(line) for line in window), (
            "the compaction boundary no longer clears `assistant_text` in its own "
            "window -- if the reset moved, the scan below must move with it"
        )

    def test_the_leak_scan_precedes_the_reset_it_guards(self):
        lines = self._lines()
        start = self._boundary(lines)
        window = lines[start : start + 40]
        reset_at = next(i for i, line in enumerate(window) if self.RESET.match(line))
        before = "\n".join(window[:reset_at])
        assert self.SCAN in before and self.FLAG in before, (
            "the compaction boundary clears `assistant_text` without scanning it "
            f"for a leaked tool call first. A scan after line "
            f"{start + reset_at + 1} reads an empty string and records no leak, so "
            "a block the user already saw is reported nowhere."
        )
        assert self.ACCUMULATE.search(before), (
            "the boundary OVERWRITES the recorded leak instead of accumulating it. "
            "A turn crossing two boundaries then loses the first one's leak when "
            "the second segment is clean, and the user gets no card for a block "
            "they saw. Keep `= _compaction_dropped_leak or ...`."
        )

    def test_the_recorded_fact_reaches_the_turn_end_predicate(self):
        """The flag is useless unless the notice arm actually reads it."""
        source = self.RUNNER.read_text(encoding="utf-8")
        assert "should_notice_compaction_dropped_leak(" in source, (
            "the runner records the dropped leak but never consults the predicate "
            "that would surface it"
        )
        assert f"dropped_leak={self.FLAG}" in source, (
            "the notice arm does not receive the boundary's recorded fact, so the "
            "card can never fire"
        )
