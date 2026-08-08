"""A config write must not postpone the break nudge.

This is the bug that made break reminders look broken, and it was invented by the
port rather than inherited: `patch_config` re-armed the break countdown on EVERY
config write, with a well-meant comment about a shortened interval taking effect
promptly. The overlay saves the companion's position through that same config
endpoint, and the companion moves ITSELF -- the idle fidget hops it a few pixels and
the new position is persisted. So every little hop restarted the break clock, and a
companion left alone postponed its own breaks forever.

Measured on a live instance before the fix: 22 seconds before a nudge was due, a
position write pushed the deadline back out to 269 seconds.

The desktop app this came from re-arms only on start, on return from away, and after
firing, and reads the interval lazily when it arms -- so it never had the bug. These
tests pin the middle ground: prompt re-arm when the interval genuinely changed,
untouched countdown for every other write.
"""

from __future__ import annotations

import time as time_mod

from kiro_crew.apps.builtins.crew_companion.store import CompanionStore


def _armed_store(tmp_path, monkeypatch):
    """A store with presence established and its break countdown armed."""
    store = CompanionStore(tmp_path)
    base = time_mod.monotonic()
    fake = {"t": base}
    monkeypatch.setattr(time_mod, "monotonic", lambda: fake["t"])
    store.note_presence()
    store.tick()  # arms the first break
    return store, fake


class TestPositionWritesDoNotPostponeBreaks:
    def test_saving_the_position_leaves_the_countdown_alone(self, tmp_path, monkeypatch):
        store, fake = _armed_store(tmp_path, monkeypatch)
        armed_at = store._next_break_at  # noqa: SLF001 — the value under test

        # Exactly what the overlay posts after the companion fidgets.
        store.patch_config({"petX": 1299, "petY": 343})

        assert store._next_break_at == armed_at, (  # noqa: SLF001
            "a position save re-armed the break countdown"
        )

    def test_a_fidgeting_companion_still_gets_its_break(self, tmp_path, monkeypatch):
        # The end-to-end shape of the bug: keep saving a position while time passes,
        # and the nudge must still arrive.
        store, fake = _armed_store(tmp_path, monkeypatch)
        # The default interval is 45 minutes, so shorten it to the 5-minute minimum
        # first and let the next tick arm against it. Jitter is +/-15%, so 8 minutes
        # of advancement clears the 5.75-minute worst case.
        store.patch_config({"breakReminderMins": 5})
        store.tick()

        for step in range(1, 9):  # eight minutes, a position write every minute
            fake["t"] += 60
            store.note_presence()
            store.patch_config({"petX": 100 + step, "petY": 200 + step})
            store.tick()

        kinds = [f["kind"] for f in store.drain(0)["fires"]]
        assert any(k.startswith("break") for k in kinds), (
            "the companion postponed its own break by moving"
        )

    def test_an_unrelated_setting_does_not_postpone_it_either(self, tmp_path, monkeypatch):
        store, _ = _armed_store(tmp_path, monkeypatch)
        armed_at = store._next_break_at  # noqa: SLF001

        store.patch_config({"sessionNotificationsEnabled": False})

        assert store._next_break_at == armed_at  # noqa: SLF001

    def test_changing_the_interval_DOES_re_arm_promptly(self, tmp_path, monkeypatch):
        # The behaviour the original comment wanted, kept: shortening the interval
        # must not make the user wait out the old, longer one.
        store, _ = _armed_store(tmp_path, monkeypatch)
        assert store._next_break_at != 0.0  # noqa: SLF001

        store.patch_config({"breakReminderMins": 5})

        assert store._next_break_at == 0.0, (  # noqa: SLF001
            "a real interval change should re-arm on the next tick"
        )

    def test_writing_the_same_interval_is_not_a_change(self, tmp_path, monkeypatch):
        # A panel that re-sends the current value on every render must not become a
        # second way to postpone the nudge forever.
        store, _ = _armed_store(tmp_path, monkeypatch)
        current = store.snapshot()["breakReminderMins"]
        armed_at = store._next_break_at  # noqa: SLF001

        store.patch_config({"breakReminderMins": current})

        assert store._next_break_at == armed_at  # noqa: SLF001
