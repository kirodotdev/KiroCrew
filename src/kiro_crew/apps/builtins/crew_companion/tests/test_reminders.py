"""Scheduling rules, ported case-for-case from the desktop app's own suite.

Source: ``crew-companion/src/test/reminders.test.ts`` (29 cases). Twenty of them
are ported here verbatim in intent, including the exact-string assertions. The
other nine cover ``labelFor`` / ``upNext`` — presentation that deliberately
stayed in TypeScript (see the module docstring), so porting them here would test
code that does not exist on this side.

The TypeScript is the specification. Where a case looks like it is pinning a
quirk, it is: the comments explaining WHY are carried across with it, because a
future reader who deletes the loop in ``advance`` or the do-while in
``skip_once`` needs to find out from a failing test, not from a user.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from kiro_crew.apps.builtins.crew_companion.reminders import (
    BREAK_NUDGES,
    MAX_BREATHE_PROMPTS_PER_DAY,
    BreatheBudget,
    Recurrence,
    Reminder,
    ReminderFile,
    advance,
    can_prompt_breathe,
    clamp_break_mins,
    day_key,
    due_reminders,
    file_from_dict,
    file_to_dict,
    jittered_interval_seconds,
    note_breathe_prompt,
    parse_iso,
    pick_break_nudge,
    pick_variant,
    reminder_from_dict,
    skip_once,
    to_iso,
)

#: Fixed clock so rollover is deterministic. Naive, i.e. LOCAL — matching the
#: original fixture `new Date('2026-07-31T14:00:00')`, which JS also reads as local.
NOW = parse_iso("2026-07-31T14:00:00")


def rem(**over: object) -> Reminder:
    # Annotated rather than inferred: without it the literal narrows to
    # dict[str, str | None] and `over` (object) cannot be merged in, which is what the
    # discarded `# type: ignore` was hiding.
    base: dict[str, object] = {
        "id": "r1",
        "text": "Call the dentist",
        "fire_at": "2026-07-31T15:00:00",
        "recurrence": None,
        "created_at": "2026-07-31T10:00:00",
    }
    base.update(over)
    return Reminder(**base)  # type: ignore[arg-type]


def rec(fire_at: str, every_minutes: int | None) -> Reminder:
    return Reminder(
        id="r",
        text="x",
        fire_at=fire_at,
        recurrence=Recurrence(every_minutes) if every_minutes else None,
        created_at="2026-07-31T00:00:00.000Z",
    )


# ── dueReminders ────────────────────────────────────────────────────────────


class TestDueReminders:
    def test_returns_only_reminders_whose_time_has_passed(self):
        past = rem(id="past", fire_at="2026-07-31T13:59:00")
        future = rem(id="future", fire_at="2026-07-31T14:01:00")
        assert [r.id for r in due_reminders([past, future], NOW)] == ["past"]

    def test_ignores_reminders_already_finished(self):
        done = rem(id="done", fire_at="2026-07-31T13:00:00", done=True)
        assert due_reminders([done], NOW) == []


# ── advance ─────────────────────────────────────────────────────────────────


class TestAdvance:
    def test_marks_a_one_time_reminder_done(self):
        assert advance(rem(fire_at="2026-07-31T13:00:00"), NOW).done is True

    def test_rolls_a_recurring_reminder_to_the_next_slot_after_now(self):
        hourly = rem(fire_at="2026-07-31T13:30:00", recurrence=Recurrence(60))
        out = advance(hourly, NOW)
        assert out.done is False
        assert parse_iso(out.fire_at) == parse_iso("2026-07-31T14:30:00")

    def test_skips_every_missed_slot_rather_than_catching_up(self):
        """The catch-up trap: a reminder due while the machine slept must not fire
        once per missed slot — it lands on the next FUTURE slot in one step."""
        hourly = rem(fire_at="2026-07-30T08:00:00", recurrence=Recurrence(60))
        out = advance(hourly, NOW)
        assert parse_iso(out.fire_at) > NOW
        # And only just past it — the next slot, not some distant one.
        assert parse_iso(out.fire_at) - NOW <= timedelta(hours=1)

    def test_always_lands_strictly_in_the_future(self):
        exact = rem(fire_at="2026-07-31T14:00:00", recurrence=Recurrence(30))
        assert parse_iso(advance(exact, NOW).fire_at) > NOW


# ── break nudges ────────────────────────────────────────────────────────────


class TestBreakNudges:
    def test_can_pick_every_nudge_in_the_rotation(self):
        # Lowest and highest rand values must map to first and last entries.
        assert pick_break_nudge(lambda: 0)[0] == BREAK_NUDGES[0][0]
        assert pick_break_nudge(lambda: 0.999)[0] == BREAK_NUDGES[-1][0]

    def test_never_picks_out_of_range(self):
        # rand() is documented as [0,1) but the boundary is guarded anyway.
        assert pick_break_nudge(lambda: 1) is not None

    def test_every_family_has_five_phrasings(self):
        # Five per family, listed explicitly so a missing catalogue key is a
        # findable absence rather than a raw key rendered in a notification.
        assert [len(keys) for _, keys in BREAK_NUDGES] == [5, 5, 5, 5]

    def test_pick_variant_avoids_the_last_phrasing_used(self):
        keys = ("a", "b")
        assert pick_variant(keys, lambda: 0, avoid="a") == "b"

    def test_pick_variant_drops_the_constraint_when_nothing_is_left(self):
        """With one variant there is nothing to avoid, so the constraint is
        dropped rather than yielding an empty pool."""
        assert pick_variant(("only",), lambda: 0, avoid="only") == "only"


# ── jitter ──────────────────────────────────────────────────────────────────


class TestJitter:
    def test_varies_around_the_base_interval(self):
        low = jittered_interval_seconds(45, 0.15, lambda: 0)  # -15%
        high = jittered_interval_seconds(45, 0.15, lambda: 0.999)  # +15%
        assert low < 45 * 60
        assert high > 45 * 60

    def test_never_returns_less_than_a_minute(self):
        assert jittered_interval_seconds(0.2, 0.9, lambda: 0) >= 60


# ── daily breathe-prompt budget ─────────────────────────────────────────────


class TestBreatheBudget:
    def test_allows_prompting_on_a_fresh_day(self):
        assert can_prompt_breathe(ReminderFile(), NOW) is True

    def test_stops_after_the_daily_maximum(self):
        f = ReminderFile()
        for _ in range(MAX_BREATHE_PROMPTS_PER_DAY):
            assert can_prompt_breathe(f, NOW) is True
            f = note_breathe_prompt(f, NOW)
        assert can_prompt_breathe(f, NOW) is False

    def test_resets_on_the_next_day(self):
        """The budget is per calendar day, so it must reset rather than latch shut."""
        f = ReminderFile()
        for _ in range(MAX_BREATHE_PROMPTS_PER_DAY):
            f = note_breathe_prompt(f, NOW)
        assert can_prompt_breathe(f, NOW) is False
        tomorrow = parse_iso("2026-08-01T09:00:00")
        assert can_prompt_breathe(f, tomorrow) is True
        assert day_key(tomorrow) != day_key(NOW)

    def test_still_returns_a_nudge_when_breathing_is_excluded(self):
        """When the budget is spent the break still happens — only the breathing
        suggestion drops out of the pool."""
        for r in (0, 0.34, 0.67, 0.99):
            nudge_id, _ = pick_break_nudge(lambda r=r: r, False)  # type: ignore[misc]
            assert nudge_id != "breathe"

    def test_can_still_pick_breathing_when_allowed(self):
        ids = [
            pick_break_nudge(lambda r=r: r, True)[0]  # type: ignore[misc]
            for r in (0, 0.26, 0.51, 0.76, 0.99)
        ]
        assert "breathe" in ids


# ── skipOnce ────────────────────────────────────────────────────────────────


class TestSkipOnce:
    def test_moves_a_recurring_reminder_forward_by_one_interval(self):
        r = rec("2026-07-31T16:00:00.000Z", 120)
        out = skip_once(r, parse_iso("2026-07-31T15:00:00.000Z"))
        assert out.fire_at == "2026-07-31T18:00:00.000Z"

    def test_keeps_the_recurrence_intact(self):
        """'Not this time' must not become 'never again'."""
        r = rec("2026-07-31T16:00:00.000Z", 1440)
        out = skip_once(r, parse_iso("2026-07-31T15:00:00.000Z"))
        assert out.recurrence == Recurrence(1440)

    def test_leaves_a_one_time_reminder_untouched(self):
        """A one-time reminder has no next slot, so skipping would be
        indistinguishable from deleting it — the caller relies on the object
        coming back unchanged."""
        r = rec("2026-07-31T16:00:00.000Z", None)
        assert skip_once(r, parse_iso("2026-07-31T15:00:00.000Z")) is r

    def test_lands_strictly_in_the_future_even_when_several_slots_passed(self):
        """Why the loop rather than a single addition: a reminder whose slot is
        already past would otherwise be 'skipped' to another past time and fire
        immediately, which reads as the skip having done nothing."""
        r = rec("2026-07-31T09:00:00.000Z", 60)
        now = parse_iso("2026-07-31T15:30:00.000Z")
        out = skip_once(r, now)
        assert parse_iso(out.fire_at) > now
        assert out.fire_at == "2026-07-31T16:00:00.000Z"

    def test_advances_past_an_exactly_now_slot_rather_than_firing_again(self):
        now = parse_iso("2026-07-31T16:00:00.000Z")
        out = skip_once(rec("2026-07-31T16:00:00.000Z", 30), now)
        assert parse_iso(out.fire_at) > now


# ── interval clamping ───────────────────────────────────────────────────────


class TestClampBreakMins:
    def test_clamps_into_range(self):
        assert clamp_break_mins(1) == 5
        assert clamp_break_mins(9999) == 480
        assert clamp_break_mins("45") == 45

    def test_returns_none_for_unusable_input(self):
        """None rather than a fallback: a bad value must leave the current setting
        alone instead of silently resetting it to something the user did not pick."""
        for bad in ("", "abc", "-5", 0, -1):
            assert clamp_break_mins(bad) is None


# ── on-disk format ──────────────────────────────────────────────────────────


class TestSerialisation:
    def test_iso_matches_javascript_to_iso_string(self):
        """Three decimal places and a Z — the format every stored row already
        uses, because the desktop app wrote them with Date.toISOString()."""
        assert to_iso(parse_iso("2026-07-31T18:00:00.000Z")) == "2026-07-31T18:00:00.000Z"

    def test_round_trips_a_reminder_through_camel_case_json(self):
        r = rem(recurrence=Recurrence(90))
        back = reminder_from_dict(
            file_to_dict(ReminderFile(reminders=(r,)))["reminders"][0]
        )
        assert back == r

    def test_a_corrupt_row_does_not_lose_the_others(self):
        """A store with one bad row must load every other row: a reminder app that
        throws on load is worse than one that starts short."""
        loaded = file_from_dict(
            {
                "version": 1,
                "reminders": [
                    {"id": "ok", "text": "keep me", "fireAt": "2026-07-31T15:00:00.000Z"},
                    {"id": "bad", "text": "no fireAt"},
                    {"nope": True},
                    {"id": "bad2", "text": "unparsable", "fireAt": "not-a-date"},
                ],
            }
        )
        assert [r.id for r in loaded.reminders] == ["ok"]

    def test_absent_or_wrong_shaped_file_reads_as_empty(self):
        assert file_from_dict(None).reminders == ()
        assert file_from_dict({"reminders": "nope"}).reminders == ()

    def test_breathe_budget_survives_a_round_trip(self):
        f = ReminderFile(breathe_budget=BreatheBudget("2026-07-31", 2))
        assert file_from_dict(file_to_dict(f)).breathe_budget == BreatheBudget(
            "2026-07-31", 2
        )

    def test_parses_the_z_suffix_on_python_3_10(self):
        """`datetime.fromisoformat` only accepted 'Z' from 3.11, and CI runs 3.10.
        Every stored fireAt ends in 'Z', so this is load-bearing, not defensive."""
        assert parse_iso("2026-07-31T18:00:00.000Z") == datetime.fromisoformat(
            "2026-07-31T18:00:00.000+00:00"
        )


# ── reminder_from_dict tolerance ────────────────────────────────────────────


class TestStoredRecurrenceBounds:
    def test_an_infinite_stored_recurrence_degrades_to_one_time(self):
        """A stored `everyMinutes: 1e309` is float infinity — it passed `> 0`
        and `int(inf)` raised OverflowError, killing the row restore. The
        value must be dropped (reminder loads as one-time) rather than raise."""
        row = reminder_from_dict(
            {
                "id": "r1",
                "text": "hydrate",
                "fireAt": "2026-07-31T16:00:00.000Z",
                "recurrence": {"everyMinutes": float("inf")},
                "createdAt": "2026-07-31T15:00:00.000Z",
            }
        )
        assert row is not None
        assert row.recurrence is None

    def test_an_absurdly_large_stored_recurrence_is_dropped(self):
        """A finite-but-absurd interval would overflow `timedelta` later in
        the fire scan, which stops ALL reminders — bound it at load time,
        matching the route's add-time cap."""
        row = reminder_from_dict(
            {
                "id": "r2",
                "text": "stretch",
                "fireAt": "2026-07-31T16:00:00.000Z",
                "recurrence": {"everyMinutes": 10**50},
            }
        )
        assert row is not None
        assert row.recurrence is None

    def test_a_normal_recurrence_still_round_trips(self):
        row = reminder_from_dict(
            {
                "id": "r3",
                "text": "water",
                "fireAt": "2026-07-31T16:00:00.000Z",
                "recurrence": {"everyMinutes": 1440},
            }
        )
        assert row is not None
        assert row.recurrence == Recurrence(1440)
