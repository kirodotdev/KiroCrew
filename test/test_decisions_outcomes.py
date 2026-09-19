"""The hand-off registry: one outcome per session, read once, always bounded.

Four properties carry the weight, and each is a way the strip goes wrong without
them. ``consume`` POPS, or a decision made on one turn is attached to every later
reply in the session and reads as a decision made on THAT turn. The TTL expires an
entry, or an outcome whose turn was cancelled waits in memory for a reply it does
not describe. The ceiling evicts, or a producer with no reader leaks one dict per
turn for the life of the process. And both directions hold the lock, because
``publish`` runs on the prompt-assembly worker thread while ``consume`` runs on the
event loop -- the one pairing this module exists to make safe.
"""

from __future__ import annotations

import threading

import pytest

from kiro_crew.decisions import outcomes


@pytest.fixture(autouse=True)
def clean_registry():
    """No outcome survives into another test: the registry is process-wide."""
    outcomes.reset()
    yield
    outcomes.reset()


def _outcome(turn: str = "t-1") -> dict:
    """An outcome shaped like the one ``skills.select`` publishes."""
    return {
        "turn_id": turn,
        "ts": "2026-09-19T07:00:00+00:00",
        "point": "skills.select",
        "baseline": ["brazil"],
        "jev": ["crux-code-reviews"],
        "agree": False,
        "p": 0.82,
        "tokens_saved": 4120,
        "candidates": 37,
        "batches": 1,
        "history_chars": 8800,
        "truncated": 0,
        "error": None,
    }


class TestPublishAndConsume:
    def test_what_was_published_is_what_is_consumed(self):
        published = _outcome()
        outcomes.publish("chat-1", published)

        assert outcomes.consume("chat-1") == published

    def test_the_dict_is_handed_over_not_copied(self):
        """The finalizer stores it on the row; a copy would only cost an allocation."""
        published = _outcome()
        outcomes.publish("chat-1", published)

        assert outcomes.consume("chat-1") is published

    def test_consume_pops_so_the_next_reply_carries_nothing(self):
        outcomes.publish("chat-1", _outcome())

        assert outcomes.consume("chat-1") is not None
        assert outcomes.consume("chat-1") is None

    def test_an_unpublished_session_reads_as_none(self):
        assert outcomes.consume("chat-never") is None

    def test_sessions_do_not_read_each_other(self):
        outcomes.publish("chat-1", _outcome("t-1"))
        outcomes.publish("chat-2", _outcome("t-2"))

        assert outcomes.consume("chat-2")["turn_id"] == "t-2"
        assert outcomes.consume("chat-1")["turn_id"] == "t-1"

    def test_a_second_publish_replaces_the_first(self):
        """One entry per session: the newer decision owns the next reply."""
        outcomes.publish("chat-1", _outcome("old"))
        outcomes.publish("chat-1", _outcome("new"))

        assert outcomes.consume("chat-1")["turn_id"] == "new"
        assert outcomes.consume("chat-1") is None
        assert outcomes.pending_count() == 0

    def test_an_empty_key_is_never_stored_on_either_side(self):
        """An outcome filed under "" would be handed to the next keyless reader."""
        outcomes.publish("", _outcome())

        assert outcomes.pending_count() == 0
        assert outcomes.consume("") is None

    @pytest.mark.parametrize("bad", ["not a dict", 7, None, ["a"]])
    def test_a_non_dict_outcome_is_dropped_not_raised(self, bad):
        outcomes.publish("chat-1", bad)  # type: ignore[arg-type]

        assert outcomes.pending_count() == 0

    def test_a_non_string_key_is_dropped_not_raised(self):
        outcomes.publish(7, _outcome())  # type: ignore[arg-type]

        assert outcomes.pending_count() == 0


class TestTTL:
    """Absolute seconds against a patched window, so the LOGIC is what is pinned.

    Advancing by ``TTL_SECONDS + 1`` would pin only the relationship between the
    test and the constant: retune the constant and such a test follows it, and a
    window widened to a year would still read as green. So the window is patched
    to a known small value, the clock is advanced by absolute seconds, and the
    shipped value is pinned once on its own.
    """

    @pytest.fixture
    def clock(self, monkeypatch):
        """A frozen clock the test advances, with a 60-second claim window."""
        now = {"t": 1000.0}
        monkeypatch.setattr(outcomes.time, "monotonic", lambda: now["t"])
        monkeypatch.setattr(outcomes, "TTL_SECONDS", 60.0)
        return now

    def test_the_shipped_window_is_ten_minutes(self):
        """Long enough for a tool-using reply, short enough that nobody sees a stale strip."""
        assert outcomes.TTL_SECONDS == 600.0

    def test_an_outcome_older_than_the_window_is_not_handed_over(self, clock):
        outcomes.publish("chat-1", _outcome())

        clock["t"] += 61.0
        assert outcomes.consume("chat-1") is None

    def test_an_outcome_inside_the_window_still_is(self, clock):
        outcomes.publish("chat-1", _outcome())

        clock["t"] += 59.0
        assert outcomes.consume("chat-1") is not None

    def test_an_expired_entry_is_evicted_not_merely_hidden(self, clock):
        """A reader that never arrives must not pin the dict for the process's life."""
        outcomes.publish("chat-abandoned", _outcome())

        clock["t"] += 61.0
        outcomes.publish("chat-live", _outcome())

        assert outcomes.pending_count() == 1
        assert outcomes.consume("chat-abandoned") is None

    def test_a_republish_restarts_the_clock(self, clock):
        outcomes.publish("chat-1", _outcome("old"))

        clock["t"] += 59.0
        outcomes.publish("chat-1", _outcome("new"))
        clock["t"] += 59.0

        assert outcomes.consume("chat-1")["turn_id"] == "new"

    def test_consume_refuses_a_stale_entry_on_its_own(self, clock, monkeypatch):
        """The eviction sweep is for memory; the age test is for correctness.

        The sweep stops at the first live entry, so it happens to reach every
        stale one today. ``consume`` re-checks anyway, and that check is what
        actually guarantees a stale outcome is never handed over -- so it is
        tested with the sweep disabled rather than through it.
        """
        outcomes.publish("chat-1", _outcome())
        monkeypatch.setattr(outcomes, "_drop_expired", lambda _now: None)

        clock["t"] += 61.0
        assert outcomes.consume("chat-1") is None


class TestBound:
    def test_the_oldest_publish_is_evicted_at_the_ceiling(self, monkeypatch):
        monkeypatch.setattr(outcomes, "MAX_SESSIONS", 3)
        for n in range(4):
            outcomes.publish(f"chat-{n}", _outcome(f"t-{n}"))

        assert outcomes.pending_count() == 3
        assert outcomes.consume("chat-0") is None
        assert outcomes.consume("chat-3")["turn_id"] == "t-3"

    def test_a_runaway_producer_bounds_itself(self, monkeypatch):
        monkeypatch.setattr(outcomes, "MAX_SESSIONS", 10)
        for n in range(500):
            outcomes.publish(f"chat-{n}", _outcome())

        assert outcomes.pending_count() == 10


class TestThreads:
    def test_publishes_from_many_threads_lose_nothing_and_stay_bounded(self):
        """``publish`` is called from the prompt-assembly executor, not the loop."""
        keys = [f"chat-{n}" for n in range(200)]
        barrier = threading.Barrier(8)

        def worker(shard: int) -> None:
            barrier.wait()
            for key in keys[shard::8]:
                outcomes.publish(key, _outcome(key))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcomes.pending_count() == len(keys)
        assert all(outcomes.consume(key)["turn_id"] == key for key in keys)

    def test_a_publisher_thread_and_a_consumer_race_hand_over_each_outcome_once(self):
        """Exactly-once is the property: a strip shown twice is a wrong strip."""
        keys = [f"chat-{n}" for n in range(300)]
        seen: list[str] = []
        done = threading.Event()

        def producer() -> None:
            for key in keys:
                outcomes.publish(key, _outcome(key))
            done.set()

        def consumer() -> None:
            while not done.is_set() or outcomes.pending_count():
                for key in keys:
                    got = outcomes.consume(key)
                    if got is not None:
                        seen.append(got["turn_id"])

        threads = [threading.Thread(target=producer), threading.Thread(target=consumer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(seen) == sorted(keys)
        assert outcomes.pending_count() == 0


class TestReset:
    def test_reset_forgets_everything(self):
        outcomes.publish("chat-1", _outcome())
        outcomes.reset()

        assert outcomes.pending_count() == 0
        assert outcomes.consume("chat-1") is None
