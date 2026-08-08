"""The tick and the store — behaviour that the pure rules cannot cover alone.

Every test drives :meth:`CompanionStore.tick` directly with an injected clock and
an injected ``rand``, so nothing here sleeps or races a real timer. The threaded
loop is exercised separately and only for start/stop idempotence.
"""

from __future__ import annotations

import json
import os
import time as time_mod
from datetime import datetime, timedelta

import pytest

from kiro_crew.apps.builtins.crew_companion.reminders import parse_iso, to_iso
from kiro_crew.apps.builtins.crew_companion.store import (
    MAX_PENDING,
    CompanionStore,
)


class Clock:
    """A movable clock, so 'a reminder came due' needs no waiting."""

    def __init__(self, start: str = "2026-07-31T14:00:00") -> None:
        self.now = parse_iso(start)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture()
def store(tmp_path):
    s = CompanionStore(tmp_path, rand=lambda: 0.0, now=Clock())
    s.load()
    # Presence, so break nudges are not suppressed as "away" in tests that want them.
    s.note_presence()
    return s


def _fires(store: CompanionStore, since: int = 0) -> list[dict]:
    return store.drain(since)["fires"]


# ── reminders ───────────────────────────────────────────────────────────────


class TestReminderFiring:
    def test_a_due_reminder_is_queued_for_the_overlay(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("drink water", to_iso(clock.now + timedelta(minutes=5)))

        s.tick()
        assert _fires(s) == []  # not due yet

        clock.advance(minutes=6)
        s.tick()
        fires = _fires(s)
        assert [f["kind"] for f in fires] == ["reminder"]
        assert fires[0]["text"] == "drink water"

    def test_a_one_time_reminder_is_dropped_after_firing(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("call the dentist", to_iso(clock.now - timedelta(minutes=1)))
        s.tick()
        assert s.snapshot()["reminders"] == []

    def test_a_recurring_reminder_survives_and_rolls_forward(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("stretch", to_iso(clock.now - timedelta(minutes=1)), every_minutes=60)
        s.tick()

        rows = s.snapshot()["reminders"]
        assert len(rows) == 1
        assert parse_iso(rows[0]["fireAt"]) > clock.now

    def test_a_reminder_fires_even_when_the_user_is_away(self, tmp_path):
        """Break nudges are suppressed while away; a reminder the user set for a
        specific time must still arrive — late, on return — rather than be dropped."""
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        # No note_presence() at all, so the store reads the user as away.
        s.add("take pills", to_iso(clock.now - timedelta(minutes=1)))
        s.tick()
        assert [f["kind"] for f in _fires(s)] == ["reminder"]

    def test_firing_does_not_repeat_on_the_next_tick(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("once", to_iso(clock.now - timedelta(minutes=1)))
        s.tick()
        first = s.drain(0)
        s.tick()
        assert s.drain(first["cursor"])["fires"] == []


# ── the delivery queue ──────────────────────────────────────────────────────


class TestDeliveryQueue:
    def test_drain_is_cursor_based_not_destructive(self, store):
        """A lost HTTP response must not lose a reminder, so reading does not clear."""
        store.add("a", to_iso(store._now() - timedelta(minutes=1)))
        store.tick()
        first = store.drain(0)
        assert len(first["fires"]) == 1
        # Same cursor again returns the same item — the overlay may retry.
        assert len(store.drain(0)["fires"]) == 1
        # Past it, nothing.
        assert store.drain(first["cursor"])["fires"] == []

    def test_the_queue_is_bounded_and_drops_the_oldest(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        for i in range(MAX_PENDING + 10):
            s.add(f"r{i}", to_iso(clock.now - timedelta(minutes=1)))
            s.tick()
        fires = s.drain(0)["fires"]
        assert len(fires) == MAX_PENDING
        # The newest survived; the oldest were dropped.
        assert fires[-1]["text"] == f"r{MAX_PENDING + 9}"


# ── break nudges ────────────────────────────────────────────────────────────


class TestBreakNudges:
    def test_no_nudge_while_the_user_is_away(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()  # deliberately no presence ping
        s.tick()          # arms
        clock.advance(hours=2)
        s.tick()
        assert [f for f in _fires(s) if f["kind"].startswith("break")] == []

    def test_a_nudge_fires_once_the_interval_has_passed(self, store, monkeypatch):
        # The break schedule uses a monotonic clock (it must not be affected by the
        # wall clock moving), so the test moves monotonic time rather than `now`.
        base = time_mod.monotonic()
        fake = {"t": base}
        monkeypatch.setattr(time_mod, "monotonic", lambda: fake["t"])
        store.note_presence()

        store.tick()  # arms the first break
        fake["t"] = base + 60 * 60 * 3  # three hours later
        # Re-ping: presence has a 90s TTL, and a real overlay pings continuously.
        # Without this the store correctly reads the user as away and stays quiet,
        # which is the behaviour the away test below pins.
        store.note_presence()
        store.tick()

        kinds = [f["kind"] for f in _fires(store)]
        assert any(k.startswith("break") for k in kinds)

    def test_disabling_break_nudges_silences_them(self, store, monkeypatch):
        base = time_mod.monotonic()
        fake = {"t": base}
        monkeypatch.setattr(time_mod, "monotonic", lambda: fake["t"])
        store.patch_config({"breakNudgesEnabled": False})
        store.note_presence()

        store.tick()
        fake["t"] = base + 60 * 60 * 3
        store.note_presence()
        store.tick()
        assert [f for f in _fires(store) if f["kind"].startswith("break")] == []

    def test_a_nudge_carries_a_catalogue_key_not_english_prose(self, store, monkeypatch):
        """The backend has no business holding UI copy: it names the phrasing and
        the renderer translates, so a nudge is not English-only."""
        base = time_mod.monotonic()
        fake = {"t": base}
        monkeypatch.setattr(time_mod, "monotonic", lambda: fake["t"])
        store.note_presence()
        store.tick()
        fake["t"] = base + 60 * 60 * 3
        store.note_presence()
        store.tick()

        breaks = [f for f in _fires(store) if f["kind"].startswith("break")]
        assert breaks, "expected a break nudge"
        assert breaks[0]["key"].startswith("break.")
        assert breaks[0]["text"] == ""

    def test_presence_going_stale_is_read_as_away(self, store, monkeypatch):
        """Silence from the overlay means nobody is there to nudge. Pinned because
        the two tests above have to re-ping to get a nudge at all — that is the
        mechanism, not a workaround."""
        base = time_mod.monotonic()
        fake = {"t": base}
        monkeypatch.setattr(time_mod, "monotonic", lambda: fake["t"])
        store.note_presence()
        store.tick()

        fake["t"] = base + 60 * 60 * 3  # interval elapsed, but no further ping
        store.tick()
        assert [f for f in _fires(store) if f["kind"].startswith("break")] == []

    def test_snapshot_present_flips_when_the_overlay_goes_quiet(self, tmp_path, monkeypatch):
        """The dashboard's RUNNING-vs-ENABLED signal.

        `snapshot()["present"]` is what the page reads to show the companion as
        on-screen or not. It must track the same presence the nudge gate uses:
        true while the overlay is pinging, false once the ping goes stale — which
        is exactly what "the user closed the companion" looks like to the backend.
        A fresh store (its own, not the pre-pinged fixture) so "never heard from an
        overlay" can be asserted too.
        """
        base = time_mod.monotonic()
        fake = {"t": base}
        monkeypatch.setattr(time_mod, "monotonic", lambda: fake["t"])

        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=Clock())
        s.load()

        # Never heard from an overlay -> not present.
        assert s.snapshot()["present"] is False

        # The overlay pings -> present.
        s.note_presence()
        assert s.snapshot()["present"] is True

        # Still present just inside the 90s TTL.
        fake["t"] = base + 89.0
        assert s.snapshot()["present"] is True

        # The companion is closed: no more pings. Past the TTL it reads as gone,
        # so the page flips to its "not on screen" state on the next poll.
        fake["t"] = base + 91.0
        assert s.snapshot()["present"] is False


# ── config ──────────────────────────────────────────────────────────────────


class TestConfig:
    def test_a_valid_interval_is_applied(self, store):
        store.patch_config({"breakReminderMins": 90})
        assert store.snapshot()["breakReminderMins"] == 90

    def test_an_unusable_interval_leaves_the_setting_alone(self, store):
        """A bad value must not silently reset the user's choice to a default."""
        store.patch_config({"breakReminderMins": 90})
        for bad in ("", "abc", -5, 0):
            store.patch_config({"breakReminderMins": bad})
            assert store.snapshot()["breakReminderMins"] == 90

    def test_out_of_range_is_clamped_not_rejected(self, store):
        store.patch_config({"breakReminderMins": 9999})
        assert store.snapshot()["breakReminderMins"] == 480
        store.patch_config({"breakReminderMins": 1})
        assert store.snapshot()["breakReminderMins"] == 5

    def test_toggles_round_trip(self, store):
        store.patch_config({"sessionNotificationsEnabled": False})
        assert store.snapshot()["sessionNotificationsEnabled"] is False


# ── persistence ─────────────────────────────────────────────────────────────


class TestPersistence:
    def test_reminders_survive_a_reload(self, tmp_path):
        clock = Clock()
        a = CompanionStore(tmp_path, now=clock)
        a.load()
        a.add("water", to_iso(clock.now + timedelta(hours=1)), every_minutes=120)

        b = CompanionStore(tmp_path, now=clock)
        b.load()
        rows = b.snapshot()["reminders"]
        assert [r["text"] for r in rows] == ["water"]
        assert rows[0]["recurrence"] == {"everyMinutes": 120}

    def test_config_and_stats_survive_a_reload(self, tmp_path):
        clock = Clock()
        a = CompanionStore(tmp_path, now=clock)
        a.load()
        a.patch_config({"breakReminderMins": 60})
        a.note_breathing_session()

        b = CompanionStore(tmp_path, now=clock)
        b.load()
        assert b.snapshot()["breakReminderMins"] == 60
        assert b.stats_payload()["stats"]["breathingSessions"] == 1

    def test_a_corrupt_store_loads_empty_rather_than_raising(self, tmp_path):
        (tmp_path / "crew-companion-reminders.json").write_text("{not json", "utf-8")
        s = CompanionStore(tmp_path, now=Clock())
        s.load()  # must not raise
        assert s.snapshot()["reminders"] == []

    def test_an_oversized_store_is_ignored(self, tmp_path):
        path = tmp_path / "crew-companion-reminders.json"
        path.write_text(json.dumps({"reminders": [], "pad": "x" * 2_100_000}), "utf-8")
        s = CompanionStore(tmp_path, now=Clock())
        s.load()
        assert s.snapshot()["reminders"] == []

    def test_the_written_file_is_owner_only(self, tmp_path):
        s = CompanionStore(tmp_path, now=Clock())
        s.load()
        s.add("x", to_iso(Clock().now))
        path = tmp_path / "crew-companion-reminders.json"
        # POSIX enforces this with chmod 0o600. Windows has no equivalent bit --
        # files report 0o666 there and access is governed by the DACL -- so the
        # POSIX-bit assertion is only meaningful off Windows. Same split as
        # test/test_token_auth.py makes for its secret file.
        if os.name != "nt":
            assert (path.stat().st_mode & 0o777) == 0o600
        else:
            assert path.exists()


# ── stats ───────────────────────────────────────────────────────────────────


class TestStats:
    def test_companion_seconds_counts_enabled_time(self, store):
        before = store.stats_payload()["stats"]["companionSeconds"]
        store.tick()
        store.tick()
        assert store.stats_payload()["stats"]["companionSeconds"] > before

    def test_reminders_created_counts_once_per_add(self, store):
        store.add("a", to_iso(store._now()))
        store.add("b", to_iso(store._now()))
        assert store.stats_payload()["stats"]["remindersCreated"] == 2

    def test_first_launch_is_set_once_and_kept(self, tmp_path):
        clock = Clock()
        a = CompanionStore(tmp_path, now=clock)
        a.load()
        first = a.stats_payload()["stats"]["firstLaunch"]
        assert first

        clock.advance(days=3)
        b = CompanionStore(tmp_path, now=clock)
        b.load()
        assert b.stats_payload()["stats"]["firstLaunch"] == first

    def test_streak_extends_on_consecutive_days_and_resets_on_a_gap(self, tmp_path):
        clock = Clock("2026-07-31T09:00:00")
        s = CompanionStore(tmp_path, now=clock)
        s.load()
        s.tick()
        assert s.stats_payload()["stats"]["streak"] == 1

        clock.advance(days=1)
        s.tick()
        assert s.stats_payload()["stats"]["streak"] == 2

        clock.advance(days=5)  # a gap
        s.tick()
        assert s.stats_payload()["stats"]["streak"] == 1


# ── lifecycle ───────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_start_and_stop_are_idempotent(self, tmp_path):
        s = CompanionStore(tmp_path, now=Clock())
        s.load()
        s.start()
        s.start()  # must not spawn a second thread or raise
        s.stop()
        s.stop()

    def test_a_throwing_tick_does_not_kill_the_loop(self, tmp_path, monkeypatch):
        """The one failure a reminder app cannot have is stopping silently."""
        s = CompanionStore(tmp_path, now=Clock())
        s.load()
        calls = {"n": 0}

        def boom() -> None:
            calls["n"] += 1
            raise RuntimeError("tick exploded")

        monkeypatch.setattr(s, "tick", boom)
        s.start()
        # The loop waits TICK_SECONDS before the first call; give it two chances.
        time_mod.sleep(2.5)
        s.stop()
        assert calls["n"] >= 2, f"loop stopped after {calls['n']} call(s)"


class TestPetPosition:
    """Where the user left the companion on screen.

    Worth pinning because the failure is subtle: a companion that reappears in a
    default corner every restart has silently discarded a deliberate choice, and the
    user usually moved it to keep it clear of something.
    """

    def test_a_saved_position_survives_a_reload(self, tmp_path):
        store = CompanionStore(tmp_path)
        store.load()
        store.patch_config({"petX": 412, "petY": 96})

        # A fresh store over the same directory is what a gateway restart looks like.
        reopened = CompanionStore(tmp_path)
        reopened.load()
        cfg = reopened.snapshot()
        assert cfg["petX"] == 412
        assert cfg["petY"] == 96

    def test_no_saved_position_reads_as_none_not_zero(self, tmp_path):
        # None means "never moved" and lets the renderer choose its own placement;
        # 0,0 would jam the companion into the top-left corner instead.
        store = CompanionStore(tmp_path)
        store.load()
        cfg = store.snapshot()
        assert cfg["petX"] is None
        assert cfg["petY"] is None

    def test_one_axis_alone_is_refused(self, tmp_path):
        # Half a position is worse than none: the companion would land on an axis
        # the user never chose.
        store = CompanionStore(tmp_path)
        store.load()
        store.patch_config({"petX": 300})
        assert store.snapshot()["petX"] is None

    def test_a_nonsense_coordinate_leaves_the_position_alone(self, tmp_path):
        store = CompanionStore(tmp_path)
        store.load()
        store.patch_config({"petX": 200, "petY": 150})
        store.patch_config({"petX": "over there", "petY": None})
        cfg = store.snapshot()
        assert cfg["petX"] == 200
        assert cfg["petY"] == 150

    def test_a_float_coordinate_is_rounded_not_rejected(self, tmp_path):
        # Browsers hand out fractional pixels; refusing them would make the position
        # silently fail to save on a scaled display.
        store = CompanionStore(tmp_path)
        store.load()
        store.patch_config({"petX": 120.6, "petY": 44.2})
        cfg = store.snapshot()
        assert cfg["petX"] == 121
        assert cfg["petY"] == 44


class TestPendingSurvivesRestart:
    """A queued fire must outlive the gateway.

    The window this pins: a due reminder is consumed from `reminders` the moment
    the tick queues it, so between that tick and the overlay's poll the fire
    exists ONLY in `_pending`. A restart in that window used to lose it — the
    reminder row was already gone, the queue was memory-only, and the client's
    refetch-from-zero restart recovery found nothing to refetch. The user's
    promise silently evaporated. `seq` persists with it so a restart cannot
    reissue numbers below a client's stored cursor.
    """

    def test_a_queued_reminder_survives_a_restart(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("drink water", to_iso(clock.now + timedelta(minutes=5)))
        clock.advance(minutes=6)
        s.tick()  # queues the fire AND removes the one-time reminder row
        assert [f["text"] for f in _fires(s)] == ["drink water"]

        # A fresh store over the same directory is what a gateway restart looks like.
        reopened = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        reopened.load()
        assert [f["text"] for f in _fires(reopened)] == ["drink water"]

    def test_seq_does_not_restart_below_delivered_fires(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("first", to_iso(clock.now + timedelta(minutes=1)))
        clock.advance(minutes=2)
        s.tick()
        first_seq = _fires(s)[0]["seq"]

        reopened = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        reopened.load()
        reopened.add("second", to_iso(clock.now + timedelta(minutes=1)))
        clock.advance(minutes=2)
        reopened.tick()
        seqs = [f["seq"] for f in _fires(reopened)]
        # The new fire numbers strictly above the restored one — a client whose
        # cursor sits at first_seq still sees it.
        assert max(seqs) > first_seq

    def test_a_corrupt_pending_entry_is_skipped_not_fatal(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.add("keep me", to_iso(clock.now + timedelta(minutes=1)))
        clock.advance(minutes=2)
        s.tick()
        # Corrupt one pending entry on disk; load() must shrug it off.
        raw = json.loads((tmp_path / "crew-companion-reminders.json").read_text("utf-8"))
        raw["pending"].append("not-a-fire")
        raw["pending"].append({"seq": "NaN"})
        (tmp_path / "crew-companion-reminders.json").write_text(json.dumps(raw), "utf-8")

        reopened = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        reopened.load()
        texts = [f["text"] for f in _fires(reopened)]
        assert "keep me" in texts


class TestStatsSurviveACrash:
    """Activity stats flush on a bounded interval, not only on unrelated saves.

    Stats mutate every tick but lived only in memory until some OTHER mutation
    happened to save the store — an ungraceful gateway exit lost every second of
    "kept you company" time since then. The bounded flush (STATS_FLUSH_SECONDS)
    caps the loss at one window without writing the disk at 1 Hz.
    """

    def test_ticked_seconds_survive_an_ungraceful_exit(self, tmp_path):
        clock = Clock()
        s = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        s.load()
        s.note_presence()
        # Several ticks accumulate seconds; the first tick's flush window is
        # already open (last flush starts at 0), so at least one flush happens.
        for _ in range(3):
            s.tick()
            clock.advance(seconds=1)

        # No stop(), no other mutation — this is the crash.
        reopened = CompanionStore(tmp_path, rand=lambda: 0.0, now=clock)
        reopened.load()
        assert reopened.stats_payload()["stats"]["companionSeconds"] >= 1
