"""Tests for the git-native on-call schedule.

The owner's ask was a rotation without a rotation service: a YAML file in the same repo
the ledger syncs through, keyed on GitHub logins. The properties worth pinning, in order
of what would hurt most if broken:

1. **A real rotation must be hearable.** The always-on default is always configured and
   always on-shift, so before this it masked every real source — a schedule saying
   "someone else is on call" could not be heard at all. That defeats the entire feature.
2. **Every indeterminate answer DISARMS under strict gating.** Missing file, bad YAML,
   no login, expired schedule — all resolve to ``on_shift=False, unknown=True``. With a
   file every instance reads, "cannot tell" means the schedule is wrong, and arming would
   make the whole team pick up the same alarm. Only ``on_shift`` gates work; ``unknown``
   survives so the UI can say WHY.
3. **A date-only ``to`` includes that day.** ``to: 2026-08-08`` means "through the 8th".
   Reading it as midnight would silently drop the last day of every shift.
4. **The file is untrusted input.** It arrives by ``git pull`` from a shared repo, so it
   must be size-capped and parsed with ``safe_load``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import schedule_file
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus


class _Env(unittest.TestCase):
    """Isolated data home, and a stubbed login so no test shells out to ``gh``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        schedule_file.reset_login_cache()
        self._login = mock.patch.object(
            schedule_file, "_resolve_login_sync", return_value="octocat"
        )
        self._login.start()

    def tearDown(self) -> None:
        self._login.stop()
        schedule_file.reset_login_cache()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(body: str) -> Path:
        path = schedule_file.schedule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    @staticmethod
    def _at(iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestOnShiftResolution(_Env):
    def test_operator_named_in_the_current_window_is_on_shift(self) -> None:
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n")
        status = schedule_file.resolve_now(self._at("2026-08-03T12:00"))
        self.assertTrue(status.on_shift)
        self.assertFalse(status.unknown, "a matched window is a definitive answer")
        self.assertEqual(status.who, "octocat")

    def test_someone_else_on_shift_means_off_shift(self) -> None:
        """The whole point: the file must be able to say 'not you'."""
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: hubot\n")
        status = schedule_file.resolve_now(self._at("2026-08-03T12:00"))
        self.assertFalse(status.on_shift)
        self.assertFalse(status.unknown, "a covered window is definitive even when it is not you")
        self.assertEqual(status.who, "hubot")

    def test_co_primary_list_counts(self) -> None:
        self._write(
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: [hubot, octocat]\n"
        )
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)

    def test_login_match_is_case_insensitive(self) -> None:
        """GitHub logins are case-insensitive; 'Octocat' in the file must still match."""
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: Octocat\n")
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)

    def test_date_only_end_includes_the_final_day(self) -> None:
        """`to: 2026-08-08` means through the 8th, not midnight at its start.

        Read as 00:00 this drops the last day of every shift written date-only — the
        single most likely misreading of this file format.
        """
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n")
        self.assertTrue(
            schedule_file.resolve_now(self._at("2026-08-08T18:00")).on_shift,
            "the 8th is still on shift",
        )
        self.assertTrue(
            schedule_file.resolve_now(self._at("2026-08-09T00:30")).unknown,
            "the 9th falls outside every window",
        )

    def test_explicit_times_are_honored(self) -> None:
        self._write(
            "shifts:\n  - from: 2026-08-01T09:00\n    to: 2026-08-01T17:00\n    who: octocat\n"
        )
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-01T12:00")).on_shift)
        self.assertTrue(
            schedule_file.resolve_now(self._at("2026-08-01T18:00")).unknown,
            "after the window is a schedule gap — indeterminate, so it disarms",
        )

    def test_timezone_shifts_the_window(self) -> None:
        """A team writing local times must not be silently interpreted as UTC."""
        self._write(
            "timezone: America/Los_Angeles\n"
            "shifts:\n  - from: 2026-08-01T09:00\n    to: 2026-08-01T17:00\n    who: octocat\n"
        )
        # 09:00 PDT == 16:00 UTC, so 15:00 UTC is BEFORE the shift starts.
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-01T15:00")).unknown)
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-01T20:00")).on_shift)

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        self._write(
            "timezone: Mars/Olympus_Mons\n"
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n"
        )
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)


class TestIndeterminateScheduleDisarms(_Env):
    """Every degradation must DISARM under strict gating, and say why.

    This inverts what this class originally asserted, and the inversion is the point.

    A rotation *API* should fail OPEN: "cannot tell" means the network is down, and
    wrongly disarming one instance loses incident response for something the team does
    not control.

    A committed schedule every instance reads must fail CLOSED: "cannot tell" means the
    SCHEDULE is wrong (expired, unparseable, or this operator's login is missing), and
    arming then makes *every* instance in the team pick up the same alarm — the exact
    double-claim a shared schedule exists to prevent. Verified against a real private
    GitHub remote with two instances: only the on-call one arms.

    `unknown` stays TRUE in every case, so the UI can still distinguish "a teammate has
    it" from "the file is broken". Only `on_shift` drives arming.
    """

    def _assert_disarmed_unknown(self, status: ShiftStatus) -> None:
        self.assertFalse(status.on_shift, "strict gating must NOT pick up work")
        self.assertTrue(status.unknown, "must not claim to be a real answer")

    def test_missing_file(self) -> None:
        self._assert_disarmed_unknown(schedule_file.resolve_now())

    def test_malformed_yaml(self) -> None:
        self._write("shifts: [unclosed\n")
        self._assert_disarmed_unknown(schedule_file.resolve_now())

    def test_yaml_that_is_not_a_mapping(self) -> None:
        self._write("- just\n- a\n- list\n")
        self._assert_disarmed_unknown(schedule_file.resolve_now())

    def test_no_shifts_key(self) -> None:
        self._write("timezone: UTC\n")
        self._assert_disarmed_unknown(schedule_file.resolve_now())

    def test_expired_schedule(self) -> None:
        """A rotation nobody refilled must not disable everyone's response."""
        self._write("shifts:\n  - from: 2020-01-01\n    to: 2020-01-08\n    who: octocat\n")
        self._assert_disarmed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))

    def test_unresolvable_login(self) -> None:
        """No gh, no configured login: we cannot know, so we arm."""
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n")
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value=""):
            self._assert_disarmed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))

    def test_reversed_window_is_skipped_not_honored(self) -> None:
        self._write("shifts:\n  - from: 2026-08-08\n    to: 2026-08-01\n    who: octocat\n")
        self._assert_disarmed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))

    def test_entry_with_no_who_is_skipped(self) -> None:
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n")
        self._assert_disarmed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))


class TestUntrustedInput(_Env):
    """The file arrives by `git pull` from a shared repo — treat it as hostile."""

    def test_oversized_file_is_refused(self) -> None:
        self._write("shifts:\n" + ("  # padding padding padding\n" * 20_000))
        self.assertGreater(
            schedule_file.schedule_path().stat().st_size, schedule_file.MAX_SCHEDULE_BYTES
        )
        _, _, error = schedule_file.read_schedule()
        self.assertIn("exceeds", error)

    def test_yaml_uses_safe_load(self) -> None:
        """A pushed schedule must not be able to construct arbitrary Python."""
        source = Path(schedule_file.__file__).read_text(encoding="utf-8")
        self.assertIn("yaml.safe_load", source)
        self.assertNotIn("yaml.load(", source)

    def test_shift_count_is_bounded(self) -> None:
        body = "shifts:\n" + "".join(
            f"  - from: 2026-01-01\n    to: 2026-01-02\n    who: u{n}\n" for n in range(20)
        )
        self._write(body)
        with mock.patch.object(schedule_file, "MAX_SHIFTS", 5):
            shifts, _, error = schedule_file.read_schedule()
        self.assertEqual(error, "")
        self.assertEqual(len(shifts), 5, "scan is capped")

    def test_schedule_filename_is_not_operator_configurable(self) -> None:
        """Every teammate must read the SAME file, or they disagree about the rotation."""
        self.assertEqual(schedule_file.SCHEDULE_FILENAME, "rotation.yaml")
        self.assertTrue(str(schedule_file.schedule_path()).endswith("rotation.yaml"))


class TestLivesInTheSyncedRepo(_Env):
    def test_schedule_sits_beside_the_ledger(self) -> None:
        """It must be inside the repo ledger_sync pushes, or it never reaches teammates."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        self.assertEqual(schedule_file.schedule_path().parent, ledger.ledger_path().parent)

    def test_sync_tracks_the_schedule_file(self) -> None:
        """ledger_sync's .gitignore tracks the ledger and nothing else by default —
        the schedule must be tracked too or it is written locally and never shared."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        source = Path(ledger_sync.__file__).read_text(encoding="utf-8")
        self.assertIn(
            schedule_file.SCHEDULE_FILENAME,
            source,
            "ledger_sync must un-ignore rotation.yaml, or the schedule never syncs",
        )


class TestAdapterContract(_Env):
    def test_unconfigured_when_no_schedule_exists(self) -> None:
        self.assertFalse(schedule_file.ScheduleFileRotationSource().configured())

    def test_configured_once_a_schedule_is_committed(self) -> None:
        self._write("shifts: []\n")
        self.assertTrue(schedule_file.ScheduleFileRotationSource().configured())

    def test_configured_does_not_require_a_login(self) -> None:
        """The common case is a committed file plus an already-authenticated gh."""
        self._write("shifts: []\n")
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value=""):
            self.assertTrue(schedule_file.ScheduleFileRotationSource().configured())

    def test_status_reports_without_raising_on_a_missing_file(self) -> None:
        status = schedule_file.status()
        self.assertFalse(status["present"])
        self.assertTrue(status["unknown"])
        self.assertIn("rotation.yaml", status["detail"])

    def test_status_counts_shifts(self) -> None:
        self._write(
            "timezone: UTC\nshifts:\n"
            "  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n"
            "  - from: 2026-08-08\n    to: 2026-08-15\n    who: hubot\n"
        )
        status = schedule_file.status()
        self.assertEqual(status["shifts"], 2)
        self.assertEqual(status["timezone"], "UTC")

    def test_status_exposes_no_secret_fields(self) -> None:
        """A GitHub login is not a credential; this adapter must add no secret."""
        self.assertEqual(schedule_file.ScheduleFileRotationSource().secret_fields, ())


class TestLoginResolution(_Env):
    def test_configured_login_wins_over_shelling_out(self) -> None:
        """An operator who set a login must not pay a `gh` spawn per rotation tick."""
        self._login.stop()
        try:
            gh_calls = []

            def _counting_run(argv, *a, **kw):
                # Match the whole argv, not argv[0]: sandboxed_spawn_argv PREPENDS a
                # wrapper, so `gh` is no longer element 0 and an argv[0] check silently
                # lets the real `gh` run (observed: it returned the developer's login).
                if "api" in argv and "user" in argv:
                    gh_calls.append(argv)
                raise AssertionError("no spawn should happen at all on this path")

            # The login is read from the KEYSTONE now, not provider config -- see
            # `policy_store.OPERATOR_ONLY_KEYS` for why it moved.
            with mock.patch.object(
                schedule_file.policy_store, "get", return_value="configured-user"
            ) as cfg:
                with mock.patch.object(schedule_file.subprocess, "run", side_effect=_counting_run):
                    self.assertEqual(schedule_file._resolve_login_sync(), "configured-user")
            self.assertEqual(gh_calls, [], "a configured login must not shell out")
            self.assertTrue(cfg.called)
        finally:
            self._login.start()

    def test_gh_failure_is_cached_so_it_does_not_reshell_every_tick(self) -> None:
        """The rotation-check cron runs on a schedule; an unbounded re-spawn per tick
        on a machine with no `gh` is pure waste.

        Counts only OUR ``gh`` invocations. A bare ``subprocess.run`` call count is the
        wrong assertion here: ``sandboxed_spawn_argv`` makes its own probe (``ssh -V``)
        on the way in, so a broad patch counts the sandbox layer's spawns as if they
        were ours and reports 2 for a single ``gh`` attempt.
        """
        self._login.stop()
        try:
            schedule_file.reset_login_cache()

            gh_calls = []
            real_run = schedule_file.subprocess.run

            def _counting_run(argv, *a, **kw):
                # Match the whole argv, not argv[0]: sandboxed_spawn_argv PREPENDS a
                # wrapper, so `gh` is no longer element 0 and an argv[0] check silently
                # lets the real `gh` run (observed: it returned the developer's login).
                if "api" in argv and "user" in argv:
                    gh_calls.append(argv)
                    raise OSError("no gh")
                return real_run(argv, *a, **kw)

            with mock.patch.object(schedule_file.policy_store, "get", return_value=""):
                with mock.patch.object(schedule_file.subprocess, "run", side_effect=_counting_run):
                    self.assertEqual(schedule_file._resolve_login_sync(), "")
                    self.assertEqual(schedule_file._resolve_login_sync(), "")
            self.assertEqual(len(gh_calls), 1, "the miss is cached too")
        finally:
            self._login.start()

    def test_login_lookup_is_routed_through_the_spawn_chokepoint(self) -> None:
        """test/test_spawn_audit.py requires it, and a rotation check is agent-reachable."""
        source = Path(schedule_file.__file__).read_text(encoding="utf-8")
        self.assertIn("sandboxed_spawn_argv", source)
        self.assertIn("resource_limit_preexec", source)


if __name__ == "__main__":
    unittest.main()


class TestStrictGating(_Env):
    """The team model: only the on-call instance picks up work.

    This is the property the owner asked for — "only who is oncall at this moment will
    pick up tasks and other instances will not". It is enforced by `on_shift`, which
    `rotation.tier_states` reads to arm the dispatch tier.
    """

    def test_only_the_named_operator_arms(self) -> None:
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n")
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="alice"):
            self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="bob"):
            self.assertFalse(
                schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift,
                "a teammate's instance must not pick up the on-call operator's work",
            )

    def test_the_dispatch_tier_follows_on_shift(self) -> None:
        """The gate that actually stops work, asserted through `rotation`, not just here.

        `resolve_now` returning `on_shift=False` is only meaningful if the tier map
        honors it — that is the seam between "the schedule says no" and "no dispatch
        cron runs".
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n")
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="bob"):
            states = rotation.tier_states(schedule_file.resolve_now(self._at("2026-08-03T12:00")))
        self.assertFalse(states[rotation.TIER_ON_SHIFT], "dispatch tier must be disarmed")
        self.assertTrue(states[rotation.TIER_ALWAYS], "always-tier must stay armed to re-arm later")

    def test_strict_is_the_default(self) -> None:
        """Default matters: a team that never configures this still gets single-owner."""
        self.assertTrue(schedule_file.strict_gating())

    def test_strict_can_be_turned_off_for_a_solo_install(self) -> None:
        """A single-instance operator may prefer over-firing to missing an alarm."""
        self._write("shifts:\n  - from: 2020-01-01\n    to: 2020-01-08\n    who: alice\n")
        # Through the real keystone writer, not a patch: strict-gating moved onto the fenced
        # floor (setting it false restores fail-open gating, which is the second way to defeat
        # the off-shift refusal), so this also pins that the operator path still works.
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        policy_store.put(policy_store.SCHEDULE_STRICT_KEY, False)
        self.assertFalse(schedule_file.strict_gating())
        status = schedule_file.resolve_now(self._at("2026-08-03T12:00"))
        self.assertTrue(status.on_shift, "fail-open restores the old arming behavior")
        self.assertTrue(status.unknown)

    def test_unknown_survives_so_the_ui_can_explain(self) -> None:
        """Disarming silently would be indistinguishable from a normal off-shift turn."""
        self._write("shifts: [unclosed\n")
        status = schedule_file.resolve_now(self._at("2026-08-03T12:00"))
        self.assertFalse(status.on_shift)
        self.assertTrue(status.unknown, "the UI must be able to say WHY nothing is running")


class TestRoster(_Env):
    """Team composition — the display half of the owner's ask.

    `who` alone cannot distinguish "a teammate holds the pager" from "the schedule is
    broken", and under strict gating a broken schedule means this instance has silently
    stopped working. The roster is what makes that legible.
    """

    SCHEDULE = (
        "timezone: UTC\n"
        "shifts:\n"
        "  - from: 2026-07-27\n    to: 2026-08-03\n    who: alice\n"
        "  - from: 2026-08-03\n    to: 2026-08-10\n    who: [bob, carol]\n"
        "  - from: 2026-08-10\n    to: 2026-08-17\n    who: alice\n"
    )

    def test_lists_the_whole_team_not_just_the_current_holder(self) -> None:
        self._write(self.SCHEDULE)
        roster = schedule_file.roster(self._at("2026-08-05T12:00"))
        self.assertEqual([m["login"] for m in roster["members"]], ["alice", "bob", "carol"])

    def test_marks_who_is_on_call_now(self) -> None:
        self._write(self.SCHEDULE)
        roster = schedule_file.roster(self._at("2026-08-05T12:00"))
        on_call = {m["login"] for m in roster["members"] if m["on_call_now"]}
        self.assertEqual(on_call, {"bob", "carol"}, "co-primary shifts name both")

    def test_counts_shifts_so_an_unbalanced_rotation_is_visible(self) -> None:
        self._write(self.SCHEDULE)
        by_login = {m["login"]: m["shifts"] for m in schedule_file.roster()["members"]}
        self.assertEqual(by_login["alice"], 2)
        self.assertEqual(by_login["bob"], 1)

    def test_member_order_is_stable_across_calls(self) -> None:
        """An order derived from dict iteration would reshuffle the panel every poll."""
        self._write(self.SCHEDULE)
        first = [m["login"] for m in schedule_file.roster()["members"]]
        second = [m["login"] for m in schedule_file.roster()["members"]]
        self.assertEqual(first, second)

    def test_says_when_this_instance_is_not_on_the_rotation(self) -> None:
        """The setup mistake that looks exactly like a normal off-shift turn."""
        self._write(self.SCHEDULE)
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="dave"):
            roster = schedule_file.roster()
        self.assertFalse(roster["me_on_roster"], "dave will never pick up work; say so")
        self.assertEqual(roster["me"], "dave")

    def test_identifies_this_instance_when_it_is_on_the_rotation(self) -> None:
        self._write(self.SCHEDULE)
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="carol"):
            roster = schedule_file.roster()
        self.assertTrue(roster["me_on_roster"])

    def test_malformed_rows_are_not_counted_as_coverage(self) -> None:
        """Counting a reversed or nameless window would imply a shift nobody holds."""
        self._write(
            "shifts:\n"
            "  - from: 2026-08-08\n    to: 2026-08-01\n    who: alice\n"  # reversed
            "  - from: 2026-08-01\n    to: 2026-08-08\n"  # no who
            "  - from: 2026-08-01\n    to: 2026-08-08\n    who: bob\n"
        )
        roster = schedule_file.roster(self._at("2026-08-03T12:00"))
        self.assertEqual([m["login"] for m in roster["members"]], ["bob"])

    def test_a_broken_schedule_reports_the_error_rather_than_an_empty_team(self) -> None:
        self._write("shifts: [unclosed\n")
        roster = schedule_file.roster()
        self.assertEqual(roster["members"], [])
        self.assertTrue(roster["error"], "an empty team with no reason is indistinguishable")

    def test_roster_reaches_the_dashboard_through_describe(self) -> None:
        """The wiring, not just the function — `describe` backs the board's poll."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.SCHEDULE)
        described = rotation.describe(schedule_file.resolve_now(self._at("2026-08-05T12:00")))
        self.assertIn("roster", described)
        self.assertTrue(described["roster"]["members"])

    def test_no_schedule_means_no_roster_rather_than_an_empty_one(self) -> None:
        """A solo install has no team; the UI should render nothing, not 'team: none'."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        described = rotation.describe(schedule_file.resolve_now())
        self.assertEqual(described["roster"], {})


class TestLeader(_Env):
    """Exactly ONE instance may run nightly ledger hygiene.

    `primary_instance` defaults to True and lives in each instance's own config, so on a
    team where nobody opted out, EVERY instance claimed the primary tier — verified with
    three default installs, all reporting is_primary=True. That is N agents concurrently
    running dedupe/decay/PRUNE against one shared ledger: the same shape as the
    double-claim the shared schedule prevents, but on the maintenance path, and worse,
    because a duplicate claim wastes a turn while a duplicate prune deletes knowledge.

    A shared team file wants a `leader:` field for exactly this
    reason. One fact in the file everyone reads beats N local settings agreeing by
    convention.
    """

    SCHEDULE = (
        "leader: alice\n"
        "timezone: UTC\n"
        "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n"
    )

    def test_only_the_named_leader_is_primary(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.SCHEDULE)
        for who, expected in (("alice", True), ("bob", False), ("carol", False)):
            with self.subTest(login=who):
                with mock.patch.object(schedule_file, "_resolve_login_sync", return_value=who):
                    self.assertEqual(rotation.is_primary(), expected)

    def test_the_schedule_overrides_local_config(self) -> None:
        """A teammate who left primary_instance at its default must not become leader."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.SCHEDULE)
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="bob"):
            # bob's local config says primary (the default) — the schedule still wins.
            self.assertFalse(rotation.is_primary())

    def test_no_leader_key_falls_back_to_local_config(self) -> None:
        """A solo install, and a team that configures primaries by hand, keep working."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n")
        self.assertEqual(schedule_file.leader(), "")
        self.assertTrue(rotation.is_primary(), "default primary_instance=True still applies")

    def test_no_schedule_at_all_falls_back_to_local_config(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self.assertEqual(schedule_file.leader(), "")
        self.assertTrue(rotation.is_primary())

    def test_an_unresolvable_login_is_not_the_leader(self) -> None:
        """Cannot prove leadership => do not claim it.

        A missed nightly pass is recoverable on the next run; every instance pruning the
        shared ledger is not.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.SCHEDULE)
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value=""):
            self.assertFalse(rotation.is_primary())

    def test_leader_match_is_case_insensitive(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.SCHEDULE.replace("leader: alice", "leader: Alice"))
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="alice"):
            self.assertTrue(rotation.is_primary())

    def test_a_broken_schedule_does_not_grant_leadership(self) -> None:
        """Markers or bad YAML must not read as "no leader, so I am primary" for everyone."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write("leader: alice\nshifts: [unclosed\n")
        self.assertEqual(schedule_file.leader(), "", "an unparseable file names nobody")
        # Falls back to local config, which is the documented behavior — but the ledger is
        # protected because a conflicted schedule also refuses to push (ledger_sync).
        self.assertTrue(rotation.is_primary())

    def test_the_roster_reports_the_leader(self) -> None:
        """So the panel can mark who owns hygiene without a second call."""
        self._write(self.SCHEDULE)
        self.assertEqual(schedule_file.roster()["leader"], "alice")


class TestOffShiftCannotWrite(_Env):
    """An off-shift instance must not execute a provider write.

    The tier gate only pauses SCHEDULED work. `/incident/action` is reachable
    independently, and an investigation already in flight when a shift ends keeps running —
    so before this an off-shift teammate with `act` mode and a matching rule could
    acknowledge or resolve a real page in the operator's production tooling. Verified:
    dispatch tier disarmed, and `authorize_action` still returned
    "granted by rule on cloudwatch".

    Third instance of one pattern: a gate one layer up does not protect a path that does
    not pass through it.
    """

    #: A window covering any plausible test clock. An earlier version of this fixture used
    #: 2026-08-01..08 and silently tested the INDETERMINATE path instead, because the real
    #: clock fell outside it — the guard correctly did not fire and it read as a failure.
    WIDE = "timezone: UTC\nshifts:\n  - from: 2026-01-01\n    to: 2027-12-31\n    who: alice\n"

    def _grant_act(self, login: str) -> None:
        # EVERY input to the authorization decision goes on the KEYSTONE floor, not into
        # `config.json`: that file is agent-writable and the read path deliberately ignores a
        # ceiling found there (see `policy_store` — honouring it let the constrained party set
        # its own ceiling). That now includes the rotation LOGIN: it answers "who am I on the
        # rotation", so leaving it in config let an agent name the on-call member and forge its
        # way past the off-shift refusal.
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        policy_store.set_mode("act")
        # `act` rules must name a resource_glob — no wildcard grants (AutonomyRule).
        policy_store.set_rules(
            [
                {
                    "source": "cloudwatch",
                    "mode": "act",
                    "resource_glob": "r",
                    "actions": ["ack"],
                }
            ]
        )
        policy_store.put(policy_store.SCHEDULE_LOGIN_KEY, login)
        schedule_file.reset_login_cache()
        # The base class stubs `_resolve_login_sync` to "octocat", which would shadow the
        # config written above — that shadowing made an on-call instance read as off-shift
        # and looked like a bug in the guard. Repoint the stub at the login under test.
        self._login.stop()
        self._login = mock.patch.object(schedule_file, "_resolve_login_sync", return_value=login)
        self._login.start()

    @staticmethod
    def _signal():
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        return Signal.create(source="cloudwatch", native_id="alarm/X", title="t", resource="r")

    def test_an_off_shift_instance_is_refused(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.WIDE)
        self._grant_act("bob")
        allowed, reason = rotation.authorize_action(self._signal(), "ack")
        self.assertFalse(allowed, "off shift must not write to a provider")
        self.assertIn("off shift", reason)

    def test_the_rotation_identity_cannot_be_forged_through_config_json(self) -> None:
        """The login that decides the off-shift refusal must be unforgeable from config.

        This was the third input to the same authorization decision as `mode`/`autonomy_rules`,
        and the only one left in `data/config.json` — which any auto-approved agent shell can
        write and which is served unauthenticated. So the off-shift refusal could be defeated
        without touching the fenced floor at all: write the CURRENT on-call member's login into
        provider config and this instance answers "on shift", performing a real ack/resolve
        against tooling the on-call instance owns.

        `bob` is off shift (the schedule says `alice`), and stays off shift no matter what
        config claims. Fails against the old `config_value(PROVIDER_ID, "github_login")` read.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            read_config,
            write_config,
        )

        self._write(self.WIDE)
        self._grant_act("bob")

        # The forgery: claim to be the person the schedule names.
        cfg = read_config()
        cfg.setdefault("providers", {}).setdefault("schedule-file", {})["github_login"] = "alice"
        write_config(cfg)
        schedule_file.reset_login_cache()
        # The base-class stub would shadow BOTH sources, so drop it and let the real resolution
        # run — that is the code path under test.
        self._login.stop()
        try:
            allowed, reason = rotation.authorize_action(self._signal(), "ack")
        finally:
            self._login.start()

        self.assertFalse(allowed, "a config-written login must not grant an off-shift write")
        self.assertIn("off shift", reason)

    def test_strict_gating_cannot_be_disabled_through_config_json(self) -> None:
        """The other half of the same hole: disable strict gating and the refusal stops.

        An agent that can write `config.json` can also make the schedule indeterminate (it
        lives in a repo the agent can edit), and under fail-open an indeterminate schedule
        reports `on_shift=True`. So `strict_gating` is an authorization input too, and had to
        move onto the same fenced floor. Fails against the old
        `config_value(PROVIDER_ID, "strict_gating")` read.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            read_config,
            write_config,
        )

        cfg = read_config()
        cfg.setdefault("providers", {}).setdefault("schedule-file", {})["strict_gating"] = False
        write_config(cfg)

        self.assertTrue(
            schedule_file.strict_gating(),
            "strict gating must not be disableable from the agent-writable config file",
        )

    def test_the_login_is_not_advertised_as_a_provider_config_field(self) -> None:
        """Fencing the READ is not enough while the generic write route still accepts it.

        `PUT /provider/<id>/config` accepts exactly the keys an adapter declares in
        `config_fields` and writes them to `config.json`. Leaving the login declared there
        would keep a second, non-keystone writer for an authorization input — so the field list
        has to be empty as well.
        """
        source = schedule_file.ScheduleFileRotationSource()
        self.assertEqual(source.config_fields, ())
        self.assertNotIn("github_login", source.config_fields)
        self.assertNotIn("strict_gating", source.config_fields)

    def test_no_rotation_source_advertises_an_identity_field_as_provider_config(self) -> None:
        """The CLASS, not the three keys — five instances in two review rounds earned this.

        Every input the off-shift refusal depends on must be unwritable by the party it
        constrains. Per-key tests kept passing while the next key went unnoticed:

          1. `schedule-file.github_login`     — the rotation identity (Opus 5)
          2. `schedule-file.strict_gating`    — disable it and indeterminacy permits
          3. `config_fields` still listing the login — a second, non-keystone writer
          4. `configured()` in the vote       — flip `enabled` and the source abstains
          5. `pagerduty.user_id`              — the SAME identity, one adapter over (GPT 5.6)

        So this asserts over the REAL registry: no rotation source may declare a
        config-writable field that names an identity or a gating switch. `config_fields` is
        exactly the allow-list `PUT /provider/<id>/config` writes into the agent-writable,
        unauthenticated `config.json`, so a name appearing there is a name the agent can set.

        Deliberately a NAME check rather than a taint trace: a heuristic that fails loudly on a
        new `owner_id`/`identity`/`strict_*` field is worth more than a precise analysis of the
        five fields that exist today, because the failure mode being guarded is somebody adding
        the sixth. A false positive costs one line here plus a decision about where the field
        belongs — which is the conversation that should happen anyway.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        self.addCleanup(registry.reset_registry)

        # Substrings that mark a value as identity ("who am I on this rotation?") or as a
        # switch over the refusal itself.
        forbidden = ("login", "user", "identity", "owner", "me", "strict", "gating")
        offenders = []
        for src in registry.get_registry().rotation_sources():
            for field in getattr(src, "config_fields", ()):
                lowered = field.lower()
                if any(mark in lowered.split("_") or mark in lowered for mark in forbidden):
                    offenders.append(f"{src.id}.{field}")

        self.assertEqual(
            offenders,
            [],
            "a rotation source declares an identity/gating field as provider config, which "
            "`PUT /provider/<id>/config` writes into the agent-writable config.json — move it "
            "to policy_store.OPERATOR_ONLY_KEYS and read it from there: " + ", ".join(offenders),
        )

    def test_the_off_shift_vote_does_not_consult_provider_enabled(self) -> None:
        """Instance 4 of the same class, pinned structurally.

        `_definitely_off_shift` used to skip any source where `configured()` was false, and
        `configured()` reads `providers.<id>.enabled` from `config.json` for every adapter but
        this one. One flag flip therefore made a source abstain, nothing answered, and the
        refusal stopped firing. The vote now asks every non-fallback source and lets each
        report its own inability to answer as `unknown`.
        """
        import ast
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        source = inspect.getsource(rotation._definitely_off_shift)
        tree = ast.parse(textwrap.dedent(source))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("configured", "provider_enabled")
        ]
        self.assertEqual(
            [ast.dump(c) for c in calls],
            [],
            "the off-shift vote must not consult an agent-writable predicate; each source "
            "reports its own indeterminacy as ShiftStatus(unknown=True)",
        )

    def test_the_on_call_instance_is_still_allowed(self) -> None:
        """The refusal must not break the legitimate configured case."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write(self.WIDE)
        self._grant_act("alice")
        allowed, reason = rotation.authorize_action(self._signal(), "ack")
        self.assertTrue(allowed, reason)

    def test_an_indeterminate_schedule_does_not_block_the_operator(self) -> None:
        """Deliberately narrow: refuse only a DEFINITE "someone else owns this".

        An unparseable or expired schedule must not stop an operator deliberately driving
        an action by hand — that would make a broken file disable the app rather than just
        its automation.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._write("shifts: [unclosed\n")
        self._grant_act("bob")
        self.assertFalse(rotation._definitely_off_shift())
        allowed, _ = rotation.authorize_action(self._signal(), "ack")
        self.assertTrue(allowed, "indeterminate must not block a manual action")

    def test_no_schedule_at_all_does_not_block(self) -> None:
        """Solo install: no rotation configured, so nobody is "off shift"."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._grant_act("bob")
        self.assertFalse(rotation._definitely_off_shift())
        allowed, _ = rotation.authorize_action(self._signal(), "ack")
        self.assertTrue(allowed)
