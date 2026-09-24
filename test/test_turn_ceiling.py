"""The channel turn ceiling, and the tripwire that keeps it wired everywhere.

Two halves, and the second is the one that matters over time.

The behaviour half pins what the ceiling does: it counts, it latches, it clears
on a key rotation, it stays bounded in memory, and it tells an observer once.

The discovery half pins WHERE it is. The loss this guard closes is a channel
whose runaway is unbounded, so a channel that silently misses the guard is the
bug returning with a new name. So this file enumerates every place in the package
that opens a channel turn and requires each one to be classified on purpose --
gated, or exempt with a reason. Adding a channel, or a second turn site to an
existing one, fails here until someone says which it is.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import time
from unittest.mock import MagicMock

import pytest
from source_corpus import candidate_sources, src_root

from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.renderer import TEXT_CHUNK, OutputEvent
from kiro_crew.messaging.turn_ceiling import (
    DEFAULT_MAX_TURNS,
    DEFAULT_WINDOW_SECS,
    ENV_MAX_TURNS,
    ENV_WINDOW_SECS,
    MAX_CONFIGURABLE_CEILING,
    MAX_RETAINED_KEY_CHARS,
    MAX_TRACKED_CONVERSATIONS,
    REFUSAL_TEXT,
    ConversationTurnCeiling,
    TurnCeilingExceeded,
    _store_key,
    gate,
    generated_turn,
    render_refusal,
    set_notification_sink,
    shared_ceiling,
    surface_of,
)
from kiro_crew.session import SessionManager
from kiro_crew.slack.renderer import _split_trailing_word

# ───────────────────────── behaviour ─────────────────────────


def _ceiling(**kw: object) -> ConversationTurnCeiling:
    kw.setdefault("max_turns", 3)
    kw.setdefault("window_secs", 60.0)
    return ConversationTurnCeiling(**kw)  # type: ignore[arg-type]


def test_turns_up_to_the_ceiling_are_allowed() -> None:
    ceiling = _ceiling()
    for _ in range(3):
        ceiling.check("slack:C1:T1")
    assert not ceiling.is_latched("slack:C1:T1")


def test_the_turn_past_the_ceiling_is_refused() -> None:
    ceiling = _ceiling()
    for _ in range(3):
        ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")


def test_the_refusal_carries_the_user_facing_text() -> None:
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded) as caught:
        ceiling.check("slack:C1:T1")
    assert str(caught.value) == REFUSAL_TEXT


def test_conversations_are_counted_independently() -> None:
    """One runaway conversation must not refuse turns in an unrelated one."""
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    ceiling.check("telegram:999")  # different conversation, unaffected
    assert not ceiling.is_latched("telegram:999")


def test_turns_older_than_the_window_do_not_count() -> None:
    """The window rolls, so a slow conversation is never refused."""
    ceiling = _ceiling(max_turns=2, window_secs=0.05)
    ceiling.check("slack:C1:T1")
    ceiling.check("slack:C1:T1")
    time.sleep(0.08)
    ceiling.check("slack:C1:T1")  # both earlier turns have aged out
    assert not ceiling.is_latched("slack:C1:T1")


def test_the_latch_outlives_the_window() -> None:
    """The discriminating test for latching.

    A rolling window alone bounds the burn RATE but never ends a loop: it
    resumes the moment the window slides. Once latched, waiting is not a way
    back in.
    """
    ceiling = _ceiling(max_turns=1, window_secs=0.05)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    time.sleep(0.08)  # long enough that the window would have cleared
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")


def test_a_reset_clears_the_latch() -> None:
    """The resume path. Reachable by resetting the conversation, not by waiting."""
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    ceiling.reset("slack:C1:T1")
    ceiling.check("slack:C1:T1")
    assert not ceiling.is_latched("slack:C1:T1")


def test_an_unrelated_key_has_its_own_window() -> None:
    """The count and the latch are per key, with no shared state between them."""
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    ceiling.check("slack:C1:T2")  # a different key is counted from zero
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T2")


def test_an_empty_session_key_is_not_counted() -> None:
    """Nothing to attribute a count to, and refusing it would be a silent drop."""
    ceiling = _ceiling(max_turns=1)
    for _ in range(5):
        ceiling.check("")


def test_tracking_is_bounded() -> None:
    """A host meeting many conversations must not turn a loop guard into a leak."""
    ceiling = _ceiling(max_turns=10, max_tracked=4)
    for index in range(50):
        ceiling.check(f"slack:C{index}")
    assert len(ceiling._windows) <= 4


def test_latches_are_bounded_too() -> None:
    ceiling = _ceiling(max_turns=1, max_tracked=3)
    for index in range(20):
        key = f"slack:C{index}"
        ceiling.check(key)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(key)
    assert len(ceiling._latched) <= 3


class TestTheCapSaysWhatItDropped:
    """Holding the cap costs something, and the bound has to admit it.

    A dropped latch is not a dropped row: the conversation loses its refusal, so
    its next message opens a turn and it gets a whole fresh window before it
    latches again. Silently, a truncated tail reads exactly like a population
    that was never latched at all.
    """

    def test_dropped_latches_are_counted(self) -> None:
        ceiling = _ceiling(max_turns=1, max_tracked=3)
        for index in range(20):
            key = f"slack:C-eviction-{index}"
            ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)
        assert ceiling.latches_evicted == 20 - 3

    def test_dropped_windows_are_counted_too(self) -> None:
        """The window half, which a latch-only counter left silent: a conversation
        whose window is evicted gets a fresh allowance and nothing said it."""
        ceiling = _ceiling(max_turns=10, max_tracked=4)
        for index in range(50):
            ceiling.check(f"slack:C-window-evict-{index}")
        assert ceiling.windows_evicted == 50 - 4
        assert ceiling.latches_evicted == 0

    def test_nothing_is_counted_while_the_cap_holds(self) -> None:
        """The counter-case: a store inside its cap reports no overflow."""
        ceiling = _ceiling(max_turns=1, max_tracked=50)
        for index in range(10):
            key = f"slack:C-room-{index}"
            ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)
        assert ceiling.latches_evicted == 0
        assert ceiling.windows_evicted == 0

    def test_the_drop_is_said_out_loud(self, caplog: pytest.LogCaptureFixture) -> None:
        ceiling = _ceiling(max_turns=1, max_tracked=1)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.turn_ceiling"):
            for index in range(3):
                key = f"slack:C-loud-{index}"
                ceiling.check(key)
                with pytest.raises(TurnCeilingExceeded):
                    ceiling.check(key)
        said = [r.getMessage() for r in caplog.records if "tracking cap reached" in r.getMessage()]
        assert said, "the cap dropped rows and said nothing"
        assert "cap=1" in said[-1]

    def test_the_announcement_is_throttled(self, caplog: pytest.LogCaptureFixture) -> None:
        """Eviction runs on every counted turn, so a host above the cap evicts
        continuously; one announcement per event would itself be the flood. The
        COUNTS stay exact -- only how often they are spoken is bounded."""
        ceiling = _ceiling(max_turns=10, max_tracked=1)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.turn_ceiling"):
            for index in range(200):
                ceiling.check(f"slack:C-flood-{index}")
        said = [r for r in caplog.records if "tracking cap reached" in r.getMessage()]
        assert len(said) == 1, f"200 evictions produced {len(said)} announcements"
        assert ceiling.windows_evicted == 199

    def test_an_evicted_conversation_answers_again(self) -> None:
        """Why the announcement matters: this is the behaviour it describes."""
        ceiling = _ceiling(max_turns=1, max_tracked=1)
        first = "slack:C-evicted"
        ceiling.check(first)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(first)
        assert ceiling.is_latched(first)
        second = "slack:C-newcomer"
        ceiling.check(second)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(second)
        assert not ceiling.is_latched(first)
        ceiling.check(first)  # answers again, from an empty window


class TestTheRetainedKeyIsBounded:
    """A cap on the ROW COUNT bounds memory only when each retained field is
    bounded, and the key is a field: 2048 rows of an unbounded string is
    unbounded."""

    def test_a_plausible_key_is_retained_verbatim(self) -> None:
        key = "slack:C0123ABC456:1712793600.123456"
        assert _store_key(key) == key

    def test_an_over_long_key_is_stored_bounded(self) -> None:
        key = "slack:" + "x" * (MAX_RETAINED_KEY_CHARS * 4)
        stored = _store_key(key)
        assert len(stored) <= MAX_RETAINED_KEY_CHARS
        assert stored != key

    def test_two_long_keys_sharing_a_prefix_stay_apart(self) -> None:
        """Truncation would have merged them, and a merged count bounds two
        unrelated conversations together -- the failure this module removes."""
        prefix = "slack:" + "x" * (MAX_RETAINED_KEY_CHARS * 4)
        assert _store_key(prefix + "-one") != _store_key(prefix + "-two")

    def test_a_long_key_is_still_counted_and_still_resets(self) -> None:
        key = "telegram:" + "9" * (MAX_RETAINED_KEY_CHARS * 2)
        ceiling = _ceiling(max_turns=1)
        ceiling.check(key)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(key)
        assert ceiling.is_latched(key)
        ceiling.reset(key)
        assert not ceiling.is_latched(key)
        ceiling.check(key)

    def test_the_store_never_holds_a_key_past_the_bound(self) -> None:
        ceiling = _ceiling(max_turns=10)
        ceiling.check("discord:" + "z" * (MAX_RETAINED_KEY_CHARS * 3))
        assert all(len(k) <= MAX_RETAINED_KEY_CHARS for k in ceiling._windows)


# ───────────────────────── the notice ─────────────────────────


def test_the_observer_is_told_once_at_the_latch() -> None:
    """Once, at the latch -- not again by every turn queued behind it."""
    seen: list[tuple[str, str]] = []
    set_notification_sink(lambda key, surface: seen.append((key, surface)))
    try:
        ceiling = _ceiling(max_turns=1)
        ceiling.check("slack:C1:T1")
        for _ in range(4):
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check("slack:C1:T1")
    finally:
        set_notification_sink(None)
    assert seen == [("slack:C1:T1", "slack")]


def test_a_failing_observer_does_not_change_the_refusal() -> None:
    """The sink runs inside a gate whose only job is to refuse the turn."""

    def _explode(key: str, surface: str) -> None:
        raise RuntimeError("notification feed is down")

    set_notification_sink(_explode)
    try:
        ceiling = _ceiling(max_turns=1)
        ceiling.check("slack:C1:T1")
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check("slack:C1:T1")
    finally:
        set_notification_sink(None)


def test_surface_is_read_from_the_key() -> None:
    assert surface_of("slack:C1:T1") == "slack"
    assert surface_of("telegram:42") == "telegram"


def test_a_key_without_a_surface_degrades_instead_of_raising() -> None:
    assert surface_of("") == "channel"
    assert surface_of("   ") == "channel"


def test_a_dashboard_key_degrades_rather_than_naming_itself() -> None:
    """A channel turn resuming a dashboard session runs under the dashboard key,
    which carries no ``:``. Labelling the operator's notice with that whole
    spelling would read as an opaque session id instead of a surface."""
    assert surface_of("chat-1-1721826000") == "channel"


def test_the_refusal_quotes_no_command() -> None:
    """In a self-chat the agent's own text returns as inbound.

    A refusal that quoted the command for resuming would hand the loop its own
    way out, so the text names the surface and quotes no command.
    """
    assert "/" not in REFUSAL_TEXT
    assert "dashboard" in REFUSAL_TEXT.lower()


# ───────────────────────── composition ─────────────────────────


def test_the_gate_runs_the_channels_own_gate_first() -> None:
    """Load-bearing in both directions.

    A shutdown refusal keeps behaving exactly as it does without a ceiling, and
    a turn the channel was never going to run is not counted against the
    conversation.
    """

    class Closing(Exception):
        pass

    def _inner() -> None:
        raise Closing

    ceiling = _ceiling(max_turns=1)
    composed = gate("slack:C1:T1", _inner, ceiling=ceiling)
    for _ in range(5):
        with pytest.raises(Closing):
            composed()
    # None of those refused turns were counted, so the conversation is untouched.
    assert not ceiling.is_latched("slack:C1:T1")
    ceiling.check("slack:C1:T1")


def test_the_gate_counts_when_the_channels_gate_passes() -> None:
    ceiling = _ceiling(max_turns=1)
    composed = gate("slack:C1:T1", lambda: None, ceiling=ceiling)
    composed()
    with pytest.raises(TurnCeilingExceeded):
        composed()


def test_the_gate_needs_no_inner() -> None:
    ceiling = _ceiling(max_turns=1)
    composed = gate("slack:C1:T1", ceiling=ceiling)
    composed()
    with pytest.raises(TurnCeilingExceeded):
        composed()


def test_the_gate_is_yield_free() -> None:
    """The gate it replaces must not await: the gate, monitor acceptance and the
    stream's turn registration are one event-loop span."""
    composed = gate("slack:C1:T1", ceiling=_ceiling())
    assert composed() is None  # a coroutine would be returned, not None


# ──────────────── a turn this gateway generated ────────────────


class TestAGeneratedTurnIsNotCounted:
    """An armed loop's own cycles must not spend the conversation's budget.

    The loop already carries a cycle cap and a runtime budget, so counting its
    cycles here would latch the conversation it is watching and then refuse both
    the loop's next cycle and the human's next message -- the ceiling turned
    against the feature it was supposed to leave alone.
    """

    def test_a_turn_outside_the_span_is_counted(self) -> None:
        """The default: no marker, so an ordinary inbound turn is counted."""
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-default", ceiling=ceiling)
        composed()
        with pytest.raises(TurnCeilingExceeded):
            composed()

    def test_a_generated_turn_passes_without_counting(self) -> None:
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-1", ceiling=ceiling)
        with generated_turn():
            composed()  # the loop's own cycle, not counted
        composed()  # the human's first real turn is still the first one counted

    def test_only_the_first_gate_in_the_span_is_exempt(self) -> None:
        """The exemption is spent, not span-wide, and this is the whole point.

        A dispatcher drains the messages queued during a turn from that turn's own
        tail, in the same task, so a span-wide pass would exempt every drained
        INBOUND message too -- and the drain pump keeps answering whatever each
        drained turn queues next. That is the unbounded self-driving loop this
        module exists to bound, handed a way around it.
        """
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-drain", ceiling=ceiling)
        with generated_turn():
            composed()  # the generated turn itself
            composed()  # first drained inbound: counted, reaches the ceiling
            with pytest.raises(TurnCeilingExceeded):
                composed()  # second drained inbound: refused, as any inbound is

    def test_the_exemption_is_spent_even_before_the_span_ends(self) -> None:
        """Observed where it matters: a second gate INSIDE the span is counted."""
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-spend", ceiling=ceiling)
        with generated_turn():
            composed()  # spends the exemption
            composed()  # counted, so this one reaches the ceiling
            with pytest.raises(TurnCeilingExceeded):
                composed()

    def test_an_ordinary_turn_would_have_latched(self) -> None:
        """The counter-case: the same calls outside the marker do latch."""
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-2", ceiling=ceiling)
        composed()
        with pytest.raises(TurnCeilingExceeded):
            composed()

    def test_a_generated_turn_still_runs_the_channels_own_gate(self) -> None:
        """The exemption is the ceiling's alone. A shutdown refusal, or anything
        else the channel's gate raises, must still stop a generated turn."""
        calls: list[str] = []

        def inner() -> None:
            calls.append("inner")
            raise RuntimeError("channel says no")

        composed = gate("discord:DM-3", inner, ceiling=_ceiling())
        with generated_turn():
            with pytest.raises(RuntimeError):
                composed()
        assert calls == ["inner"]

    def test_the_exemption_does_not_outlive_its_span(self) -> None:
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-after", ceiling=ceiling)
        with generated_turn():
            composed()
        composed()  # counted: the first real turn
        with pytest.raises(TurnCeilingExceeded):
            composed()

    def test_the_exemption_is_withdrawn_after_a_raise(self) -> None:
        """The context manager's ``finally`` owns this, so a span that ends by
        raising must not leave a pass behind for the next turn."""
        ceiling = _ceiling(max_turns=1)
        composed = gate("discord:DM-raise", ceiling=ceiling)
        with pytest.raises(RuntimeError):
            with generated_turn():
                raise RuntimeError("boom")
        composed()  # counted
        with pytest.raises(TurnCeilingExceeded):
            composed()

    @pytest.mark.asyncio
    async def test_a_concurrent_human_turn_is_still_counted(self) -> None:
        """Context-local, not global: the exemption must not leak into a turn
        running beside it, which is the whole reason it is a ContextVar."""
        ceiling = _ceiling(max_turns=1)
        human = gate("discord:DM-4", ceiling=ceiling)
        started = asyncio.Event()
        release = asyncio.Event()

        async def generated() -> None:
            with generated_turn():
                started.set()
                await release.wait()
                gate("discord:DM-5", ceiling=ceiling)()

        async def person() -> None:
            await started.wait()
            try:
                human()
                with pytest.raises(TurnCeilingExceeded):
                    human()
            finally:
                release.set()

        await asyncio.gather(generated(), person())


# ───────────────────────── configuration ─────────────────────────


def test_the_shipped_default_separates_a_loop_from_a_person() -> None:
    """A loop sustains 180+ turns an hour; a fast human in a busy thread is in
    the tens. The default sits between them and latches a loop inside roughly
    half a window."""
    assert DEFAULT_MAX_TURNS == 90
    assert DEFAULT_WINDOW_SECS == 3600.0


def test_an_operator_can_retune_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_MAX_TURNS, "5")
    monkeypatch.setenv(ENV_WINDOW_SECS, "120")
    ceiling = ConversationTurnCeiling()
    assert ceiling.max_turns == 5
    assert ceiling.window_secs == 120.0


@pytest.mark.parametrize("bad", ["-4", "banana", ""])
def test_an_unusable_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(ENV_MAX_TURNS, bad)
    assert ConversationTurnCeiling().max_turns == DEFAULT_MAX_TURNS


class TestTheOperatorCanTurnItOff:
    """A guard that is ON by default has to stay declinable.

    Every conversation on every channel gets this bound without choosing it, so
    an operator who wants the old unbounded behaviour must be able to say so.
    Zero is that answer; a negative number is a typo and still falls back.
    """

    def test_zero_disables_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_MAX_TURNS, "0")
        ceiling = ConversationTurnCeiling()
        assert ceiling.max_turns == 0

    def test_a_disabled_ceiling_never_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The counter-case is the hazard this pins: with the bound at zero, a
        counter that still counted would find every window already at its limit
        and latch each conversation on its FIRST message."""
        monkeypatch.setenv(ENV_MAX_TURNS, "0")
        ceiling = ConversationTurnCeiling()
        for _ in range(DEFAULT_MAX_TURNS * 2):
            ceiling.check("slack:C-off:T-off")
        assert not ceiling.is_latched("slack:C-off:T-off")

    def test_a_disabled_ceiling_tracks_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not counted rather than counted-and-never-refused, so an operator who
        turned it off does not pay for windows that can never reach a bound."""
        monkeypatch.setenv(ENV_MAX_TURNS, "0")
        ceiling = ConversationTurnCeiling()
        ceiling.check("slack:C-off:T-untracked")
        assert not ceiling._windows
        assert not ceiling._latched

    def test_an_explicit_zero_from_a_caller_still_raises(self) -> None:
        """The off switch is the operator's, not a caller's: zero passed in code
        is a programming error, and the env reader is the only way to mean it."""
        with pytest.raises(ValueError):
            ConversationTurnCeiling(max_turns=0)


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_window_is_refused(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """A positivity test alone admits both non-finite values.

    ``inf > 0`` is True and every comparison against ``nan`` is False, so either
    slips through a bare ``value <= 0`` guard. Once accepted, no timestamp ever
    ages out and the rolling window silently becomes a lifetime counter.
    """
    monkeypatch.setenv(ENV_WINDOW_SECS, bad)
    assert ConversationTurnCeiling().window_secs == DEFAULT_WINDOW_SECS


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_an_explicit_non_finite_window_raises(bad: float) -> None:
    """The env reader falls back for an operator; a caller in this codebase raises."""
    with pytest.raises(ValueError):
        ConversationTurnCeiling(window_secs=bad)


def test_a_non_finite_window_would_have_made_the_window_a_lifetime_counter() -> None:
    """Names the consequence the guard prevents, so the guard's reason is testable.

    With an infinite window the expiry horizon is ``-inf``, so nothing is ever
    pruned. The guard is what stops that state existing at all.
    """
    horizon = 0.0 - float("inf")
    assert not (1.0 <= horizon)  # no timestamp would ever be pruned


def test_an_explicit_non_positive_ceiling_raises() -> None:
    with pytest.raises(ValueError):
        ConversationTurnCeiling(max_turns=0)


def test_the_store_is_shared_across_channels() -> None:
    """One conversation is one count however many dispatch objects a host builds."""
    assert shared_ceiling() is shared_ceiling()


def test_clearing_the_store_releases_every_conversation() -> None:
    """``clear()`` forgets the whole process-global store, counts and latches.

    One counter serves the whole process, so a count outlives whatever drove it.
    A test worker shares one interpreter across unrelated tests, and the suite's
    autouse fixture calls this between them: without a working clear, a worker
    that drives more gated turns under one key than the ceiling allows leaves that
    conversation latched and every later turn on the key refused.
    """
    ceiling = shared_ceiling()
    first, second = "slack:C-clear:T-a", "slack:C-clear:T-b"
    for _ in range(ceiling.max_turns):
        ceiling.check(first)
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check(first)
    ceiling.check(second)
    assert ceiling.is_latched(first), "precondition: nothing was latched to clear"

    ceiling.clear()

    assert not ceiling.is_latched(first)
    ceiling.check(first)  # the latched conversation answers again
    # The unlatched conversation's count is gone too, so it gets a whole window
    # again: a count that survived the clear would refuse inside this loop.
    for _ in range(ceiling.max_turns):
        ceiling.check(second)
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check(second)
    ceiling.clear()


def test_the_tracking_cap_is_set() -> None:
    assert MAX_TRACKED_CONVERSATIONS > 0


# ───────────────────── the resume path is real ─────────────────────


def _provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, cwd=None, **kwargs):
        provider = MagicMock()
        provider.start = MagicMock(return_value=asyncio.sleep(0))
        provider.shutdown = MagicMock(return_value=asyncio.sleep(0))
        provider.cwd = cwd or "/unset"
        provider.context_usage_pct = MagicMock(return_value=0.0)
        provider.is_alive = MagicMock(return_value=True)
        provider.is_process_alive = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.runtime_info = MagicMock(return_value=(None, None))
        return provider

    return factory


class TestDiscardingTheConversationReleasesTheLatch:
    """The refusal text tells the user to reset the conversation, so the reset has
    to be what clears the latch.

    These drive the REAL ``discard_conversation``, not a stub. The channel key
    does NOT change on a discard -- channel linkage is retained by design -- so
    nothing about the key clears a latch, and only an explicit reset does. A stub
    would assert the wire the test itself wrote.
    """

    @pytest.mark.asyncio
    async def test_a_discard_clears_a_latched_conversation(self) -> None:
        key = "slack:C-ceiling:T-latched"
        ceiling = shared_ceiling()
        ceiling.reset(key)
        try:
            for _ in range(ceiling.max_turns):
                ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)
            assert ceiling.is_latched(key), "precondition: nothing was latched to clear"

            manager = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
            await manager.discard_conversation(key)

            assert not ceiling.is_latched(key)
            ceiling.check(key)  # the conversation answers again
        finally:
            ceiling.reset(key)

    @pytest.mark.asyncio
    async def test_a_discard_clears_the_latch_under_the_channels_own_key(self) -> None:
        """``_fold_key`` resolves an alias onto the live key, so the key the
        channel counted under and the key the discard folds to can differ.
        Clearing only the folded one would leave the latch standing."""
        key = "slack:C-ceiling:T-alias"
        ceiling = shared_ceiling()
        ceiling.reset(key)
        try:
            for _ in range(ceiling.max_turns):
                ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)

            manager = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
            folded = manager._fold_key(key)
            await manager.discard_conversation(key)

            assert not ceiling.is_latched(
                key
            ), f"latch survived under the channel's own key (folded to {folded!r})"
        finally:
            ceiling.reset(key)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["remove", "destroy"])
    async def test_every_reset_verb_clears_the_latch(self, verb: str) -> None:
        """``discard_conversation`` is not the only reset the product offers.

        Slack's ``!agent default`` answers "Reset to default agent" and reaches
        ``remove``; a latch that survived it would refuse the very first message
        of the conversation the user was just told had been reset.
        """
        key = f"slack:C-ceiling:T-{verb}"
        ceiling = shared_ceiling()
        ceiling.reset(key)
        try:
            for _ in range(ceiling.max_turns):
                ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)
            assert ceiling.is_latched(key), "precondition: nothing was latched to clear"

            manager = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
            await getattr(manager, verb)(key)

            assert not ceiling.is_latched(key), f"{verb} left the conversation latched"
            ceiling.check(key)  # the conversation answers again
        finally:
            ceiling.reset(key)


class _HoldbackRenderer:
    """A renderer double that keeps the product's OWN streaming contract.

    An append is final on the wire, so a throttled flush holds the trailing word
    back rather than risk appending half of one. The rule here IS the Slack
    renderer's ``_split_trailing_word``, imported rather than restated, so the
    double cannot drift into a friendlier contract than production has. Only the
    final flush in ``on_done`` releases a held tail; ``close()`` tears the turn
    down and flushes nothing, which is exactly what production's ``close()``
    does.
    """

    def __init__(self, *, fail_on_done: bool = False) -> None:
        self.appended: list[str] = []
        self.sealed = False
        self._held = ""
        self._fail_on_done = fail_on_done

    async def dispatch(self, event: OutputEvent) -> None:
        ready, self._held = _split_trailing_word(self._held + event.text)
        if ready:
            self.appended.append(ready)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._fail_on_done:
            raise RuntimeError("stream could not be sealed")
        self.appended.append(self._held)
        self._held = ""
        self.sealed = True

    async def close(self) -> None:
        return None

    def shown(self) -> str:
        return "".join(self.appended)


class TestTheNoticeArrivesWhole:
    """The notice is the entire point of the guard, so a truncated one is a bug.

    The ceiling latches, so a notice cut short is not one bad message: every
    later turn in that conversation repeats the same cut-short text.
    """

    @pytest.mark.asyncio
    async def test_the_user_sees_all_of_it(self) -> None:
        renderer = _HoldbackRenderer()
        await render_refusal(renderer, TurnCeilingExceeded(REFUSAL_TEXT))
        await renderer.close()  # the channel's finally, which flushes nothing
        assert renderer.shown() == REFUSAL_TEXT

    @pytest.mark.asyncio
    async def test_the_stream_is_ended_not_left_open(self) -> None:
        renderer = _HoldbackRenderer()
        await render_refusal(renderer, TurnCeilingExceeded(REFUSAL_TEXT))
        assert renderer.sealed

    @pytest.mark.asyncio
    async def test_dispatching_alone_would_have_cut_the_last_word(self) -> None:
        """The counter-case, so the two above cannot pass for a trivial reason.

        Without the final flush the notice loses the run after its last space --
        for this text the verb and the full stop, which is the difference between
        an instruction and a fragment.
        """
        renderer = _HoldbackRenderer()
        await renderer.dispatch(OutputEvent(kind=TEXT_CHUNK, text=REFUSAL_TEXT))
        await renderer.close()
        assert renderer.shown() != REFUSAL_TEXT
        assert REFUSAL_TEXT.startswith(renderer.shown())
        assert not renderer.sealed

    @pytest.mark.asyncio
    async def test_a_renderer_that_cannot_end_the_stream_still_shows_the_text(self) -> None:
        """Best effort by construction: the turn is already over, so a renderer
        that fails to seal must not replace a clean refusal with a traceback."""
        renderer = _HoldbackRenderer(fail_on_done=True)
        await render_refusal(renderer, TurnCeilingExceeded(REFUSAL_TEXT))  # must not raise
        assert renderer.shown()


class TestTheCeilingItselfIsBounded:
    """The ceiling is the only length bound on each conversation's retained
    deque, so a cap on the row COUNT alone leaves every row unbounded."""

    def test_an_operator_cannot_set_it_past_the_maximum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_MAX_TURNS, str(MAX_CONFIGURABLE_CEILING * 100))
        assert ConversationTurnCeiling().max_turns == MAX_CONFIGURABLE_CEILING

    def test_a_setting_inside_the_maximum_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counter-case: the clamp binds only where it must."""
        monkeypatch.setenv(ENV_MAX_TURNS, str(MAX_CONFIGURABLE_CEILING - 1))
        assert ConversationTurnCeiling().max_turns == MAX_CONFIGURABLE_CEILING - 1

    def test_the_clamp_is_said_out_loud(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(ENV_MAX_TURNS, str(MAX_CONFIGURABLE_CEILING * 100))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.turn_ceiling"):
            ConversationTurnCeiling()
        assert any("retained-window maximum" in r.getMessage() for r in caplog.records)

    def test_a_caller_past_the_maximum_raises(self) -> None:
        """The env reader clamps for an operator; a caller in this codebase raises."""
        with pytest.raises(ValueError):
            ConversationTurnCeiling(max_turns=MAX_CONFIGURABLE_CEILING + 1)

    def test_the_retained_window_cannot_outgrow_the_maximum(self) -> None:
        ceiling = ConversationTurnCeiling(max_turns=MAX_CONFIGURABLE_CEILING, window_secs=3600.0)
        key = "slack:C-window-bound"
        for _ in range(MAX_CONFIGURABLE_CEILING):
            ceiling.check(key)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(key)
        # Latching drops the window, so the deque never outlives the bound either.
        assert not ceiling._windows.get(_store_key(key))


class TestTheNoticeDoesNotAnswerItsOwnReflection:
    """In a self-chat the notice IS output, so it returns as the next inbound
    message and is refused in turn. A notice on every refusal would answer its
    own reflection forever: no model turns, but an endless exchange of channel
    messages, which is the shape this module exists to end."""

    def test_the_turn_that_closes_the_latch_announces(self) -> None:
        ceiling = _ceiling(max_turns=1)
        key = "slack:C-announce"
        ceiling.check(key)
        with pytest.raises(TurnCeilingExceeded) as caught:
            ceiling.check(key)
        assert caught.value.announce

    def test_every_later_refusal_is_silent(self) -> None:
        ceiling = _ceiling(max_turns=1)
        key = "slack:C-silent"
        ceiling.check(key)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(key)
        for _ in range(4):
            with pytest.raises(TurnCeilingExceeded) as caught:
                ceiling.check(key)
            assert not caught.value.announce

    @pytest.mark.asyncio
    async def test_a_silent_refusal_renders_nothing(self) -> None:
        renderer = _HoldbackRenderer()
        await render_refusal(renderer, TurnCeilingExceeded(REFUSAL_TEXT, announce=False))
        assert not renderer.shown()
        assert not renderer.sealed

    @pytest.mark.asyncio
    async def test_an_announcing_refusal_still_renders(self) -> None:
        """The counter-case, so the silence above is a decision and not a break."""
        renderer = _HoldbackRenderer()
        await render_refusal(renderer, TurnCeilingExceeded(REFUSAL_TEXT, announce=True))
        assert renderer.shown() == REFUSAL_TEXT
        assert renderer.sealed


# ───────────────────── the discovery tripwire ─────────────────────

#: Channel turn sites that MUST compose the ceiling, by path relative to the
#: package root. These are the inbound arms: a message arrived from a chat
#: surface and is about to drive a model turn.
GATED = {
    "messaging/dispatch.py",
    "slack/transport_dispatch.py",
    "slack/handler.py",
    "telegram/transport_dispatch.py",
    "discord/transport_dispatch.py",
}

#: Turn sites that must NOT compose the ceiling, each with the reason. A reason
#: is required because an unexplained exemption is how a channel goes unbounded.
EXEMPT = {
    "dashboard/chat_runner.py": (
        "dashboard turns are driven by a human watching and clicking through them"
    ),
    "slack/gateway.py": (
        "generates nudge turns rather than receiving them, and dispatches each "
        "through a channel's own gated dispatcher inside "
        "turn_ceiling.generated_turn, which is where the exemption is decided"
    ),
    "session.py": "forwards to the gate's definition rather than opening a channel turn",
}


def _calls_begin_turn(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "begin_turn"
        ):
            return True
    return False


def _turn_site_files() -> set[str]:
    root = src_root()
    found: set[str] = set()
    for path, text in candidate_sources(require_any=("begin_turn",)):
        if "_vendor" in path.parts:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - a parse gate owns this
            continue
        if _calls_begin_turn(tree):
            found.add(path.relative_to(root).as_posix())
    return found


def test_every_turn_site_is_classified() -> None:
    """The tripwire.

    A new channel, or a second turn site in an existing one, lands here first.
    Deciding it is gated or exempt is a one-line edit; forgetting to decide is
    what this test refuses.
    """
    found = _turn_site_files()
    classified = GATED | set(EXEMPT)
    unclassified = found - classified
    assert not unclassified, (
        "these files open a channel turn but are neither gated nor exempt: "
        f"{sorted(unclassified)} -- add each to GATED (and compose "
        "turn_ceiling.gate at the site) or to EXEMPT with a reason"
    )


def test_the_classification_has_no_stale_entries() -> None:
    """An entry naming a site that is absent is a tripwire that cannot fire."""
    found = _turn_site_files()
    stale = (GATED | set(EXEMPT)) - found
    assert not stale, f"classified but absent from the tree: {sorted(stale)}"


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_composes_the_ceiling(relative: str) -> None:
    text = (src_root() / relative).read_text(encoding="utf-8")
    assert "turn_ceiling.gate(" in text


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_catches_the_refusal(relative: str) -> None:
    """Composing the gate without catching its refusal would turn a bounded
    pause into an unhandled error, which is a worse outcome than the loop."""
    text = (src_root() / relative).read_text(encoding="utf-8")
    assert "except TurnCeilingExceeded" in text


def _ceiling_branch_source(tree: ast.Module) -> str:
    """Everything a site's ``except TurnCeilingExceeded`` handler does.

    Read as an AST handler rather than sliced out of the text between one
    ``except`` and the next: these branches wrap their own sends in an inner
    ``try``, so a textual window closes at that inner ``except`` and hides the
    rest of the branch -- which is exactly how a missing step passes a pin.
    """
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if "TurnCeilingExceeded" not in ast.dump(node.type or ast.Pass()):
            continue
        out.extend(ast.unparse(stmt) for stmt in node.body)
    return "\n".join(out)


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_does_not_spool_the_refused_message(relative: str) -> None:
    """The spool replays a message our own restart dropped. A message refused on
    purpose must not be replayed, or it is answered after all."""
    tree = ast.parse((src_root() / relative).read_text(encoding="utf-8"))
    branch = _ceiling_branch_source(tree)
    assert branch, f"{relative} has no TurnCeilingExceeded handler to inspect"
    assert "spool_refused_turn" not in branch


@pytest.mark.parametrize("relative", sorted(EXEMPT))
def test_an_exempt_site_stays_ungated(relative: str) -> None:
    text = (src_root() / relative).read_text(encoding="utf-8")
    assert "turn_ceiling.gate(" not in text, (
        f"{relative} is listed EXEMPT ({EXEMPT[relative]}) but composes the "
        "ceiling; move it to GATED or remove the composition"
    )


def test_every_exemption_states_a_reason() -> None:
    for relative, reason in EXEMPT.items():
        assert reason.strip(), relative


#: Gated sites that substitute a ``SilentRenderer`` when the dashboard has muted
#: delivery. The refusal has to go through that substitute: a muted conversation
#: is one the user disconnected, and the latch does not clear on its own, so the
#: concrete renderer would post into it once per inbound message forever.
#:
#: Slack is absent on purpose: its dispatcher builds its own driver and never
#: consults ``delivery_is_muted``, so it has no substitute to miss.
MUTE_AWARE = ("messaging/dispatch.py", "telegram/transport_dispatch.py")


def _muted_renderer_name(tree: ast.Module) -> str:
    """The name a site binds its ``SilentRenderer`` substitute to.

    Read from the site's own source rather than written down here, so the pin
    cannot drift from the product: renaming the local renames what is required.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if "SilentRenderer" not in ast.dump(node.value or ast.Pass()):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                return target.id
    return ""


def _refusal_renderer_name(tree: ast.Module) -> str:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "render_refusal"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            return node.args[0].id
    return ""


@pytest.mark.parametrize("relative", MUTE_AWARE)
def test_a_mute_aware_site_renders_the_refusal_through_the_substitute(relative: str) -> None:
    tree = ast.parse((src_root() / relative).read_text(encoding="utf-8"))
    substitute = _muted_renderer_name(tree)
    assert substitute, f"{relative} is listed MUTE_AWARE but binds no SilentRenderer"
    assert _refusal_renderer_name(tree) == substitute, (
        f"{relative} renders the ceiling refusal through a renderer other than "
        f"{substitute!r}, so a muted conversation would be posted into"
    )


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_leaves_no_progress_block_behind(relative: str) -> None:
    """A route that returns at the gate must undo its own "working" affordance.

    Its own, not the general case: only a site that posts one before the gate has
    something to undo. Left behind it is a live Stop button for a turn that never
    opened, one per refused message for as long as the latch holds.
    """
    text = (src_root() / relative).read_text(encoding="utf-8")
    if "_working_ts = await" not in text:
        return
    branch = _ceiling_branch_source(ast.parse(text))
    assert "delete_message(channel, _working_ts)" in branch, (
        f"{relative} posts a working block before the gate but its ceiling "
        "branch returns without deleting it"
    )
