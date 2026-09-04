"""The fire doorbell: a queued fire is announced, and never lost if announcing fails.

Latency, not correctness. The desktop app this was ported from kept reminders in its
main process and pushed them straight to the pet window, so a due reminder showed
within its 1s tick. Here the store lives in the gateway and the overlay polls over
HTTP, which stacked the poll interval on top of the tick — the reminder was late by
seconds and the user noticed.

The rule that matters most here is the failure direction: the fire is appended to the
queue BEFORE anyone is told, so a broken notifier costs latency (the poll still finds
it) and never a missed reminder. These tests pin that, because the tempting shape —
publish and then enqueue, or let the publish raise — turns a cosmetic delay into a
silently dropped notification.
"""

from __future__ import annotations

import datetime

from kiro_crew.apps.builtins.crew_companion.store import MAX_PENDING, CompanionStore


def _store(tmp_path, on_fire=None) -> CompanionStore:
    return CompanionStore(tmp_path, on_fire=on_fire)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class TestFireDoorbell:
    def test_a_due_reminder_announces_itself(self, tmp_path):
        rings: list[int] = []
        store = _store(tmp_path, on_fire=lambda: rings.append(1))
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        store.add("stand up", _iso(past))

        store.tick()

        assert rings, "a fire was queued but nothing was announced"
        assert store.drain(0)["fires"], "the fire itself is missing from the queue"

    def test_the_fire_survives_a_failing_announcement(self, tmp_path):
        # The whole point: a push that raises must not cost the notification.
        def boom() -> None:
            raise RuntimeError("no websocket clients")

        store = _store(tmp_path, on_fire=boom)
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        store.add("drink water", _iso(past))

        store.tick()  # must not raise

        fires = store.drain(0)["fires"]
        assert [f["text"] for f in fires] == ["drink water"]

    def test_no_notifier_is_a_supported_configuration(self, tmp_path):
        # The manifest may not grant the events permission, and every existing test
        # constructs the store bare. That path must keep working, just slower.
        store = _store(tmp_path)
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        store.add("stretch", _iso(past))

        store.tick()

        assert [f["text"] for f in store.drain(0)["fires"]] == ["stretch"]

    def test_a_reminder_not_yet_due_rings_nothing(self, tmp_path):
        rings: list[int] = []
        store = _store(tmp_path, on_fire=lambda: rings.append(1))
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        store.add("later", _iso(future))

        store.tick()

        assert not rings, "announced a fire that has not happened"
        assert not store.drain(0)["fires"]


class TestBacklogNeverDropsAReminder:
    """The pending cap must trim ambient nudges, never a reminder the user set.

    The cap stops an overlay that has been closed for days from growing an unbounded
    queue. Trimming the oldest indiscriminately meant a reminder could be discarded to
    make room for break nudges -- and a nudge is ambient while a reminder is a promise
    the user made to themselves. This asserts which one gives way.
    """

    def test_reminders_survive_a_flood_of_nudges(self, tmp_path):
        store = _store(tmp_path)
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        store.add("take the pills", _iso(past))
        store.tick()  # queues the reminder

        # Now bury it under far more nudges than the cap allows.
        for _ in range(MAX_PENDING + 20):
            store._queue_locked("break", key="break.water.1")  # noqa: SLF001

        texts = [f["text"] for f in store.drain(0)["fires"] if f["kind"] == "reminder"]
        assert texts == ["take the pills"], "the user's reminder was trimmed away"

    def test_the_cap_is_still_enforced(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(MAX_PENDING + 30):
            store._queue_locked("break", key="break.water.1")  # noqa: SLF001

        assert len(store.drain(0)["fires"]) <= MAX_PENDING

    def test_order_is_preserved_after_a_trim(self, tmp_path):
        # The overlay shows only the NEWEST fire, so a shuffled queue would show the
        # wrong one.
        store = _store(tmp_path)
        for _ in range(10):
            store._queue_locked("break", key="break.water.1")  # noqa: SLF001
        seqs = [f["seq"] for f in store.drain(0)["fires"]]
        assert seqs == sorted(seqs)
