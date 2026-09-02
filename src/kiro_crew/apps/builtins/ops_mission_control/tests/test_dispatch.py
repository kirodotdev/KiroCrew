"""Tests for the dispatch engine — the loop that makes the app actually work.

The most important assertions here are the ones about *silence* and about ledger
matching. A heartbeat that speaks every two minutes makes the ops channel
unreadable, and a claim that does not consult the ledger makes the compounding-
memory mechanism decorative. Both fail quietly in production, so both are pinned.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path


class _HomeIsolated(unittest.IsolatedAsyncioTestCase):
    """Redirects the data home and resets the provider registry per test."""

    def setUp(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        self.tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        self._clear_caches()
        registry.reset_registry()
        # Install ONLY the fakes each test registers — the public adapters would
        # otherwise try to reach real APIs.
        self.registry = registry.OpsProviderRegistry()
        registry._registry = self.registry

    def tearDown(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        self._clear_caches()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_caches():
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    @staticmethod
    def _signal(native_id="alarm/dlq", title="DLQ depth exceeded", **kw):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        return Signal.create(source="fake", native_id=native_id, title=title, **kw)

    def _add_source(self, signals):
        parent = self

        class _Fake:
            id = "fake"
            display_name = "Fake"

            def configured(self):
                return True

            async def poll(self):
                return list(signals)

        self.registry.register_signal_source(_Fake())
        return parent

    def _write_config(self, payload):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store as store_mod
        from kiro_crew.apps.manager import app_data_dir

        (app_data_dir(store_mod.APP_NAME) / "config.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


class TestCycleSilence(_HomeIsolated):
    async def test_no_signals_means_no_change(self):
        """The cron must be able to tell 'nothing happened' and stay quiet."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)
        self.assertEqual(result.claimed, [])

    async def test_already_claimed_signal_is_not_a_change(self):
        """A signal that keeps firing must not re-announce itself every cycle."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        first = await dispatch.run_cycle()
        self.assertTrue(first.changed)
        second = await dispatch.run_cycle()
        self.assertFalse(second.changed)

    async def test_a_resolved_alarm_refiring_is_claimed_through_run_cycle(self):
        """The recurrence fix, asserted through the FULL cycle rather than `store.claim`.

        `store.claim` learned that a terminal incident no longer owns its signal, and 408
        unit tests passed — but `run_cycle` has its own cheap pre-filter that computed
        `owned` from every non-stale incident, so it discarded the recurrence *before*
        `claim` ever saw it. The app still permanently stopped responding to any failure it
        had already handled once, and the compounding-memory fast path stayed unreachable.

        Caught only by driving a real gateway: inject → resolve → re-inject reported
        `polled=1, claimed=0`. Two places encoded the same ownership rule and fixing one
        looked complete. This test exercises the path the cron actually takes.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, models, store

        self._add_source([self._signal()])
        first = await dispatch.run_cycle()
        self.assertEqual(len(first.claimed), 1)
        incident_id = first.claimed[0].incident.incident_id

        store.transition(incident_id, models.STATUS_INVESTIGATING)
        store.transition(incident_id, models.STATUS_RESOLVED)

        # Same alarm fires again — a fresh incident, not a reopening.
        again = await dispatch.run_cycle()
        self.assertEqual(len(again.claimed), 1, "a resolved alarm that re-fires must be claimed")
        self.assertNotEqual(again.claimed[0].incident.incident_id, incident_id)

    async def test_an_open_incident_still_suppresses_its_signal_in_run_cycle(self):
        """The pre-filter's real job must survive the fix.

        `needs_human` is the trap: waiting on a person, not closed. A cycle that opened a
        second incident for the same still-firing alarm would double-investigate it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, models, store

        self._add_source([self._signal()])
        first = await dispatch.run_cycle()
        incident_id = first.claimed[0].incident.incident_id
        store.transition(incident_id, models.STATUS_INVESTIGATING)
        store.transition(incident_id, models.STATUS_NEEDS_HUMAN)

        again = await dispatch.run_cycle()
        self.assertEqual(again.claimed, [], "an open incident must still own its signal")

    async def test_non_firing_signal_is_ignored(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_OK

        self._add_source([self._signal(state=STATE_OK)])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)
        self.assertEqual(result.polled, 0)


class TestSuppressedSignalsAreNeverClaimed(_HomeIsolated):
    """Investigating something an operator explicitly parked destroys trust fastest.

    The claim rule is a single filter on ``state == firing``, so this holds by
    construction — which is precisely why it needs a test: the property is invisible in the
    code (there is nothing that says "suppressed"), so a later refactor that widened the
    filter to "not ok" would look harmless and would silently start investigating parked
    alarms.
    """

    async def test_a_parked_signal_is_not_claimed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source([self._signal(state=STATE_SUPPRESSED)])
        result = await dispatch.run_cycle()
        self.assertEqual(result.claimed, [])
        self.assertEqual(result.unclaimed_remaining, 0)

    async def test_a_parked_signal_is_counted_not_silently_dropped(self):
        """Otherwise the cycle reports a smaller world than it saw.

        `polled` counts firing signals only, so without this count a cycle facing three
        parked alarms reports "Polled 0 firing signal(s)" — indistinguishable from a
        genuinely quiet estate, which is the looks-deliberate-does-nothing failure the
        state itself exists to fix.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source(
            [self._signal(native_id=f"a{n}", state=STATE_SUPPRESSED) for n in range(3)]
        )
        result = await dispatch.run_cycle()
        self.assertEqual(result.suppressed, 3)
        self.assertEqual(result.polled, 0)
        self.assertEqual(result.to_dict()["suppressed"], 3)

    async def test_a_parked_signal_does_not_break_silence(self):
        """A suppression is not news, and the heartbeat must not speak on it.

        Someone silenced the alarm to stop hearing about it; announcing it would be the app
        re-notifying on exactly the signals an operator asked to mute.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source([self._signal(state=STATE_SUPPRESSED)])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)

    async def test_firing_work_beside_a_parked_signal_is_still_claimed(self):
        """The filter must exclude the parked one only — not shrink the cycle."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATE_FIRING,
            STATE_SUPPRESSED,
        )

        self._add_source(
            [
                self._signal(native_id="parked", title="parked one", state=STATE_SUPPRESSED),
                self._signal(native_id="live", title="live one", state=STATE_FIRING),
            ]
        )
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), 1)
        self.assertEqual(result.claimed[0].incident.signal.title, "live one")
        self.assertEqual(result.polled, 1)
        self.assertEqual(result.suppressed, 1)


class TestClaimCap(_HomeIsolated):
    async def test_storm_is_capped_not_dropped(self):
        """A 50-alarm storm claims the cap now and the rest on later cycles."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        many = [self._signal(native_id=f"alarm/{n}", title=f"thing {n} broke") for n in range(50)]
        self._add_source(many)
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)
        # The remainder is reported, not silently discarded.
        self.assertEqual(result.unclaimed_remaining, 50 - dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)

    async def test_cap_is_configurable(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._write_config({"max_claims_per_cycle": 1})
        self._add_source([self._signal(native_id="a"), self._signal(native_id="b")])
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), 1)

    async def test_nonsense_cap_falls_back_to_default(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._write_config({"max_claims_per_cycle": "banana"})
        many = [self._signal(native_id=f"alarm/{n}") for n in range(10)]
        self._add_source(many)
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)


class TestLedgerWiring(_HomeIsolated):
    """The point of the whole app: a repeat failure arrives with its answer."""

    def _seed_verified_pattern(self, fingerprint, *, prior_uses=None):
        """Seed a verified/high entry that has ALREADY earned the fast path.

        ``prior_uses`` defaults to ``MIN_USES_FOR_FAST_PATH - 1`` because the claim under
        test contributes the last one itself: ``attach_ledger_matches`` calls
        ``record_use`` before ``is_fast_path``. Written as arithmetic over the constant
        rather than a literal so raising the floor does not silently turn every fast-path
        test in this class into a test of the floor instead.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            CONFIDENCE_HIGH,
            TRUST_VERIFIED,
            LedgerEntry,
        )

        entry = ledger.upsert(
            LedgerEntry.create(
                pattern="DLQ fills with duplicate-PK rows",
                fix="Clear the DLQ and redrive",
                fingerprints=[fingerprint],
                confidence=CONFIDENCE_HIGH,
                trust=TRUST_VERIFIED,
            )
        )
        uses = ledger.MIN_USES_FOR_FAST_PATH - 1 if prior_uses is None else prior_uses
        for _ in range(uses):
            ledger.record_use(entry.entry_id)
        return entry

    async def test_matching_pattern_is_attached_and_persisted(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store

        signal = self._signal()
        entry = self._seed_verified_pattern(signal.fingerprint)
        self._add_source([signal])

        result = await dispatch.run_cycle()
        claimed = result.claimed[0]
        self.assertEqual([m.entry_id for m in claimed.matches], [entry.entry_id])
        # Persisted, so re-opening the incident later still shows the match.
        stored = store.get_incident(claimed.incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.ledger_matches, [entry.entry_id])

    async def test_verified_high_confidence_match_is_the_fast_path(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        signal = self._signal()
        self._seed_verified_pattern(signal.fingerprint)
        self._add_source([signal])
        self.assertTrue((await dispatch.run_cycle()).claimed[0].fast_path)

    async def test_weak_match_is_not_the_fast_path(self):
        """An unverified guess must not be presented as a known answer."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            CONFIDENCE_LOW,
            TRUST_OBSERVED,
            LedgerEntry,
        )

        signal = self._signal()
        ledger.upsert(
            LedgerEntry.create(
                pattern="maybe this",
                fix="try that",
                fingerprints=[signal.fingerprint],
                confidence=CONFIDENCE_LOW,
                trust=TRUST_OBSERVED,
            )
        )
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertEqual(len(claimed.matches), 1)
        self.assertFalse(claimed.fast_path)

    async def test_use_count_is_incremented_and_reported_post_increment(self):
        """The brief must not claim 'used 0x' for a pattern it just used."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger

        signal = self._signal()
        self._seed_verified_pattern(signal.fingerprint, prior_uses=0)
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertEqual(claimed.matches[0].use_count, 1)
        self.assertEqual(ledger.read_entries()[0].use_count, 1)

    async def test_a_verified_entry_matching_for_the_first_time_is_not_the_fast_path(self):
        """A brand-new entry must not unlock "propose this fix directly" on sight.

        `POST /ledger` takes `confidence` and `trust` verbatim, so one hand-authored
        entry could arrive as verified/high and be proposed for a production failure
        having never been applied to anything. Worse after the exact-identity layer:
        `record_use` binds the provider key on that first match, so from occurrence two
        onward the same single piece of evidence presents as an EXACT match — a strictly
        stronger-looking claim with nothing new behind it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        signal = self._signal()
        self._seed_verified_pattern(signal.fingerprint, prior_uses=0)
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        # Matched, and the fix is still carried — this is a demotion to "hypothesis",
        # not a withholding.
        self.assertEqual(len(claimed.matches), 1)
        self.assertFalse(claimed.fast_path)
        self.assertIn("Clear the DLQ and redrive", dispatch.investigation_brief(claimed))

    async def test_an_entry_whose_fix_failed_loses_the_fast_path(self):
        """The mechanical downward path, end to end at the outermost caller."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger

        signal = self._signal()
        entry = self._seed_verified_pattern(signal.fingerprint)
        ledger.record_miss(entry.entry_id)
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertFalse(claimed.fast_path)
        # And the agent is TOLD, not merely denied the fast path — a ranked list reads
        # as an endorsement, so silence here would leave "used 2×" looking like
        # corroboration when part of that count is the record of the fix not holding.
        self.assertIn("STILL FIRING", dispatch.investigation_brief(claimed).upper())

    async def test_recurrence_of_the_same_failure_matches_its_ancestor(self):
        """Different numbers and timestamps, same pattern — the whole premise."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        original = self._signal(title="DLQ depth exceeded 500 at 2026-01-01T00:00:00Z")
        self._seed_verified_pattern(original.fingerprint)
        # A different day, a different count, a different native id.
        recurrence = self._signal(
            native_id="alarm/dlq-2", title="DLQ depth exceeded 912 at 2026-07-30T12:00:00Z"
        )
        self._add_source([recurrence])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertTrue(claimed.fast_path)

    async def test_unknown_failure_claims_cleanly_with_no_matches(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertEqual(claimed.matches, [])
        self.assertFalse(claimed.fast_path)


class TestRotationGate(_HomeIsolated):
    async def test_off_shift_skips_dispatch_entirely(self):
        """A misconfigured manual trigger must not dispatch off-shift."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        class _OffShift:
            id = "off"
            display_name = "off"

            def configured(self):
                return True

            async def on_shift(self):
                return ShiftStatus(on_shift=False)

        self.registry.register_rotation_source(_OffShift())
        self._add_source([self._signal()])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)
        self.assertIn("off shift", result.skipped_reason)

    async def test_unknown_rotation_still_dispatches(self):
        """Fail-open: an unreachable rotation API must not stop response."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        result = await dispatch.run_cycle()
        self.assertTrue(result.changed)


class TestBrief(_HomeIsolated):
    async def test_brief_states_authority_limits(self):
        """The investigating agent must be told what it may not do."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        brief = dispatch.investigation_brief(claimed)
        self.assertIn("act", brief)
        self.assertIn("Never run a remediation command", brief)

    async def test_fresh_install_says_nothing_is_watching(self):
        """The first thing a new user does, and the moment the app must admit setup.

        With no configured source, `polled == 0` is ambiguous — "nothing is wrong" and
        "nothing is watching" are opposite conclusions. The dashboard derived this
        itself, but an agent hitting POST /dispatch got a silent empty result.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        # No _add_source() call: this is a genuinely empty registry.
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed, "a fresh install must still be silent")
        self.assertIn("No signal source is configured", result.skipped_reason)
        self.assertIn("Settings", result.skipped_reason)

    async def test_an_unconfigured_source_does_not_count_as_watching(self):
        """Registered is not the same as set up."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        class _Unconfigured:
            id = "not-set-up"
            display_name = "Not set up"

            def configured(self) -> bool:
                return False

            async def poll(self):
                raise AssertionError("must not poll an unconfigured source")

        self.registry.register_signal_source(_Unconfigured())
        result = await dispatch.run_cycle()
        self.assertIn("No signal source is configured", result.skipped_reason)

    async def test_a_source_whose_configured_check_raises_is_not_trusted(self):
        """An adapter that cannot answer "am I ready" must not be polled.

        Treating it as ready turns "nothing is watching" into a source-level error
        every single cycle, which is noise the operator cannot act on.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        class _Broken:
            id = "broken-readiness"
            display_name = "Broken"

            def configured(self) -> bool:
                raise RuntimeError("config store unreadable")

            async def poll(self):
                raise AssertionError("must not poll")

        self.registry.register_signal_source(_Broken())
        result = await dispatch.run_cycle()
        self.assertIn("No signal source is configured", result.skipped_reason)

    async def test_brief_carries_brokered_evidence(self):
        """The gateway reads; the agent reasons over text.

        The investigating agent's sandbox has no AWS credentials, so before this the
        brief carried signal metadata and ledger hints and nothing else — an AWS
        investigation had no alarm history and no logs. The fix is brokering, NOT
        handing the agent credentials: the gateway already holds the profile and
        already redacts at a single chokepoint.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
        )

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = [
            Evidence(
                source="cloudwatch-evidence",
                kind="logs",
                title="Recent errors — /aws/lambda/x",
                body="[ERROR] ValueError: File processing failed.",
            )
        ]
        brief = dispatch.investigation_brief(claimed)
        self.assertIn("ValueError: File processing failed.", brief)
        self.assertIn("Recent errors", brief)
        # It must say the agent has no credentials, or the agent wastes a turn trying.
        # Asserted unconditionally in test_brief_always_states_it_has_no_credentials —
        # kept here too so the with-evidence path can never lose it silently.
        self.assertIn("you have NONE", brief)

    async def test_brief_evidence_is_bounded(self):
        """The per-item EvidenceBudget (64 KB) is a spool cap, not a prompt cap.

        Six calls at 64 KB is ~384 KB into a prompt, against a documented 50k total
        session context budget. A real beta brief measured 37k chars from two items.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
        )

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = [
            Evidence(source="s", kind="logs", title=f"item {n}", body="x" * 50_000)
            for n in range(6)
        ]
        brief = dispatch.investigation_brief(claimed)
        self.assertLess(len(brief), 20_000, "brief must stay well under the context budget")
        # And it must ADMIT the clipping — silent truncation invites confident
        # reasoning over a partial picture.
        self.assertIn("truncated", brief)

    async def test_brief_omits_the_evidence_block_when_there_is_none(self):
        """An empty 'evidence' heading reads as 'we looked and found nothing'."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = []
        self.assertNotIn("Provider evidence", dispatch.investigation_brief(claimed))

    async def test_brief_always_states_it_has_no_credentials(self):
        """The no-evidence brief is the case that MOST needs the warning.

        Regression test for an observed live failure. The statement used to live only
        inside the ``if claimed.evidence`` branch, so an incident with nothing gathered
        — an unconfigured evidence source, a provider outage, a source that returned
        empty — handed the agent an AWS alarm and no explanation. Two real sessions
        (INV-1, INV-2) then spent their entire turn re-running ``aws … --profile …``,
        collecting NoCredentials each time, and produced no diagnosis.

        Asserted with evidence EMPTY on purpose: with evidence present the old code
        passed too, which is exactly why the gap survived.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = []

        brief = dispatch.investigation_brief(claimed)
        self.assertIn("you have NONE", brief)
        # And it must name the dead end concretely — "you lack credentials" alone still
        # leaves `aws sts get-caller-identity` looking worth one try.
        self.assertIn("Do not run `aws`", brief)

    async def test_evidence_failure_does_not_lose_the_claim(self):
        """Evidence is context, never a gate on claiming work."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        with mock.patch.object(
            dispatch, "gather_evidence_safely", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                await dispatch.run_cycle()

        # The helper itself must swallow, so the cycle above is the only raising path.
        registry = mock.Mock()
        registry.gather_evidence = mock.AsyncMock(side_effect=RuntimeError("provider down"))
        out = await dispatch.gather_evidence_safely(registry, self._signal())
        self.assertEqual(out, [])

    async def test_claimed_incident_serializes_evidence(self):
        """The dispatch route returns this to the cron, which passes it on."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
        )

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = [Evidence(source="s", kind="k", title="t", body="b")]
        payload = claimed.to_dict()
        self.assertEqual(payload["evidence"][0]["body"], "b")
        self.assertEqual(payload["evidence"][0]["title"], "t")

    async def test_brief_flags_a_new_failure(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertIn("new to the", dispatch.investigation_brief(claimed))

    async def test_brief_distinguishes_known_from_hypothesis(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            CONFIDENCE_HIGH,
            TRUST_VERIFIED,
            LedgerEntry,
        )

        signal = self._signal()
        entry = ledger.upsert(
            LedgerEntry.create(
                pattern="p",
                fix="f",
                fingerprints=[signal.fingerprint],
                confidence=CONFIDENCE_HIGH,
                trust=TRUST_VERIFIED,
            )
        )
        # Verified and high is no longer sufficient on its own: the entry needs a track
        # record too. The claim under test supplies the last use itself.
        for _ in range(ledger.MIN_USES_FOR_FAST_PATH - 1):
            ledger.record_use(entry.entry_id)
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertIn("KNOWN PATTERN", dispatch.investigation_brief(claimed))


class TestCloudWatchAdapterShape(_HomeIsolated):
    """The CloudWatch adapter's alarm→Signal mapping, against a fixture payload.

    Verified end-to-end against a live AWS account during development (it polled a
    real firing alarm and claimed it correctly). This test pins the mapping without
    needing credentials, so CI covers it too.
    """

    _ALARM = {
        "AlarmName": "podcast-jobs-pending",
        "AlarmDescription": "Podcast jobs pending in SQS",
        "Namespace": "AWS/SQS",
        "MetricName": "ApproximateNumberOfMessagesVisible",
        "Dimensions": [],
    }

    async def test_alarm_maps_onto_a_normalized_signal(self):
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            SEVERITY_WARNING,
            STATE_FIRING,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
        )

        self._write_config({"providers": {"cloudwatch": {"enabled": True, "region": "us-east-1"}}})
        client = mock.MagicMock()
        client.describe_alarms.return_value = {"MetricAlarms": [self._ALARM]}
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=client):
            signals = await cloudwatch.CloudWatchSignalSource().poll()

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.source, "cloudwatch")
        self.assertEqual(signal.id, "cloudwatch:alarm/podcast-jobs-pending")
        self.assertEqual(signal.title, "Podcast jobs pending in SQS")
        self.assertEqual(signal.resource, "AWS/SQS/ApproximateNumberOfMessagesVisible")
        self.assertEqual(signal.severity, SEVERITY_WARNING)
        self.assertEqual(signal.state, STATE_FIRING)
        self.assertTrue(signal.fingerprint)
        self.assertEqual(signal.labels["alarm_name"], "podcast-jobs-pending")

    async def test_critical_named_alarm_is_escalated(self):
        """CloudWatch has no severity, so the name is the heuristic."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            SEVERITY_CRITICAL,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
        )

        self._write_config({"providers": {"cloudwatch": {"enabled": True, "region": "us-east-1"}}})
        alarm = {**self._ALARM, "AlarmName": "prod-critical-db-down"}
        client = mock.MagicMock()
        client.describe_alarms.return_value = {"MetricAlarms": [alarm]}
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=client):
            signals = await cloudwatch.CloudWatchSignalSource().poll()
        self.assertEqual(signals[0].severity, SEVERITY_CRITICAL)

    async def test_an_unavailable_client_is_reported_not_swallowed(self):
        """Rewritten: this asserted `poll()` returns `[]` when the client is unavailable.

        The GOAL was right — "boto3 missing must not crash the dispatch cycle" — but the
        mechanism was wrong, and it was the mechanism that mattered. `poll_all` records a
        source as unhealthy only when `poll()` RAISES, so returning `[]` was recorded as a
        successful poll that saw nothing: with expired credentials (the same `None` client)
        the board read as an all-clear over a live estate, and `all_sources_healthy` then
        promised absence-means-recovery. Review flagged it as blocking.

        The original goal never needed the swallow: `poll_all` catches per-source exceptions
        itself, which `TestProviderErrorsSurface::test_broken_source_is_reported_not_fatal`
        proves independently. So the fault propagates and the CYCLE still survives — this
        test now pins that pair, which is what it was really about.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
        )

        self._write_config({"providers": {"cloudwatch": {"enabled": True}}})
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=None):
            with self.assertRaises(Exception):
                await cloudwatch.CloudWatchSignalSource().poll()

            # ...and the cycle around it still completes, reporting the source as failed
            # rather than as quiet.
            registry.reset_registry()
            try:
                reg = registry.get_registry()
                signals, errors = await reg.poll_all()
                self.assertEqual(signals, [])
                self.assertIn("cloudwatch", errors)
                self.assertFalse(reg.poll_health().get("cloudwatch", {}).get("ok", True))
            finally:
                registry.reset_registry()


class TestProviderErrorsSurface(_HomeIsolated):
    async def test_broken_source_is_reported_not_fatal(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        class _Broken:
            id = "broken"
            display_name = "broken"

            def configured(self):
                return True

            async def poll(self):
                raise RuntimeError("provider down")

        self.registry.register_signal_source(_Broken())
        self._add_source([self._signal()])
        result = await dispatch.run_cycle()
        # The healthy source still produced a claim.
        self.assertEqual(len(result.claimed), 1)
        self.assertIn("broken", result.errors)


class TestPostActionVerification(_HomeIsolated):
    """An action's success is re-read, and a failed poll is never read as success.

    Before this, `_handle_action` awaited `sink.execute`, audited, and stopped — so
    `ActionResult.ok` meant only "the provider returned 2xx". Checkmk dispatches commands
    asynchronously and documents that a 2xx says nothing about execution; Nagios's command
    pipe returns nothing at all. The board could therefore report an applied fix with no
    code anywhere in a position to notice it had not landed.
    """

    def _acted_incident(self, signal, *, action="silence", due="2020-01-01T00:00:00Z"):
        """Claim `signal` and stamp it as though an action had just been executed."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            MODE_ACT,
            VERIFY_PENDING,
        )

        incident = store.claim(signal, operating_mode=MODE_ACT)
        assert incident is not None
        return store.update_fields(
            incident.incident_id,
            last_action=action,
            last_action_at="2020-01-01T00:00:00Z",
            verify_after=due,
            verification=VERIFY_PENDING,
        )

    async def test_a_signal_still_firing_after_an_action_is_reported_as_such(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            VERIFY_STILL_FIRING,
        )

        signal = self._signal()
        incident = self._acted_incident(signal)
        self._add_source([signal])
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications[incident.incident_id], VERIFY_STILL_FIRING)
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.verification, VERIFY_STILL_FIRING)
        self.assertIn("Still firing", stored.verification_detail)

    async def test_a_signal_gone_from_a_healthy_poll_confirms_the_action(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import VERIFY_CLEARED

        signal = self._signal()
        incident = self._acted_incident(signal)
        # The source answers, and this signal is not among what it returned.
        self._add_source([self._signal(native_id="alarm/other")])
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications[incident.incident_id], VERIFY_CLEARED)
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.verification, VERIFY_CLEARED)

    async def test_a_failed_poll_is_never_read_as_the_action_having_worked(self):
        """The bug class §5.10 explicitly says not to reintroduce.

        Absence from a source that returned 429, timed out, or is backing off is not
        evidence of anything — and here reading it as success would ALSO feed a false
        positive into the ledger's track record, making a fix that never worked look
        proven. `unknown` is recorded and left OPEN so a later cycle retries.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            OPEN_VERIFICATIONS,
            VERIFY_UNKNOWN,
        )

        signal = self._signal()
        incident = self._acted_incident(signal)

        class _Broken:
            id = "fake"  # the SAME source id the signal names
            display_name = "Fake"

            def configured(self):
                return True

            async def poll(self):
                raise RuntimeError("429 Too Many Requests")

        self.registry.register_signal_source(_Broken())
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications[incident.incident_id], VERIFY_UNKNOWN)
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertIn(stored.verification, OPEN_VERIFICATIONS)
        self.assertIn("429", stored.verification_detail)

    async def test_a_recheck_that_is_not_due_yet_reaches_no_verdict(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import VERIFY_PENDING

        signal = self._signal()
        incident = self._acted_incident(signal, due="2099-01-01T00:00:00Z")
        self._add_source([signal])
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications, {})
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.verification, VERIFY_PENDING)

    async def test_a_still_firing_verdict_charges_a_miss_to_every_matched_entry(self):
        """The §5.10 → §5.9 join: verification is what makes use_count mean "worked"."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        signal = self._signal()
        entry = ledger.upsert(
            LedgerEntry.create(pattern="p", fix="f", fingerprints=[signal.fingerprint])
        )
        incident = self._acted_incident(signal)
        store.update_fields(incident.incident_id, ledger_matches=[entry.entry_id])
        self._add_source([signal])
        await dispatch.run_cycle()
        self.assertEqual(ledger.read_entries()[0].miss_count, 1)

    async def test_a_cleared_verdict_charges_no_miss(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        signal = self._signal()
        entry = ledger.upsert(
            LedgerEntry.create(pattern="p", fix="f", fingerprints=[signal.fingerprint])
        )
        incident = self._acted_incident(signal)
        store.update_fields(incident.incident_id, ledger_matches=[entry.entry_id])
        self._add_source([self._signal(native_id="alarm/other")])
        await dispatch.run_cycle()
        self.assertEqual(ledger.read_entries()[0].miss_count, 0)

    async def test_an_unverified_poll_charges_no_miss(self):
        """`unknown` must not demote anything — it is a statement about us, not the fix."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        signal = self._signal()
        entry = ledger.upsert(
            LedgerEntry.create(pattern="p", fix="f", fingerprints=[signal.fingerprint])
        )
        incident = self._acted_incident(signal)
        store.update_fields(incident.incident_id, ledger_matches=[entry.entry_id])

        class _Broken:
            id = "fake"
            display_name = "Fake"

            def configured(self):
                return True

            async def poll(self):
                raise RuntimeError("provider down")

        self.registry.register_signal_source(_Broken())
        await dispatch.run_cycle()
        self.assertEqual(ledger.read_entries()[0].miss_count, 0)

    async def test_a_cleared_verification_is_not_news_but_a_still_firing_one_is(self):
        """`changed` gates the cron's silence, so what counts as news is load-bearing.

        A confirmed action is the expected outcome and announcing it would make the
        heartbeat congratulate itself. A still-firing one means the app previously
        reported something as applied that was not — the most newsworthy thing a cycle
        can find.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.dispatch import CycleResult
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            VERIFY_CLEARED,
            VERIFY_STILL_FIRING,
            VERIFY_UNKNOWN,
        )

        self.assertFalse(CycleResult(verifications={"INV-1": VERIFY_CLEARED}).changed)
        self.assertFalse(CycleResult(verifications={"INV-1": VERIFY_UNKNOWN}).changed)
        self.assertTrue(CycleResult(verifications={"INV-1": VERIFY_STILL_FIRING}).changed)

    async def test_an_incident_with_no_action_is_never_verified(self):
        """Every incident on disk before this feature reads as "" — not as verified fine."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import MODE_OBSERVE

        signal = self._signal()
        incident = store.claim(signal, operating_mode=MODE_OBSERVE)
        assert incident is not None
        self._add_source([signal])
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications, {})
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.verification, "")

    async def test_a_signal_parked_at_the_provider_does_not_confirm_the_action(self):
        """A suppression is the THIRD reason a signal is absent, and the worst to misread.

        After a `silence` this app itself issued, "the provider now reports it suppressed"
        is precisely what SUCCESS looks like — so reading it as `cleared` made the recheck
        congratulate the app for muting a live fault, and charged nothing to the ledger
        while `use_count` grew. `unknown` instead, and OPEN, because the condition is
        genuinely unobservable while the alarm is muted: the recheck can only be answered
        once the suppression lifts.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            OPEN_VERIFICATIONS,
            STATE_SUPPRESSED,
            VERIFY_UNKNOWN,
        )

        signal = self._signal()
        incident = self._acted_incident(signal, action="silence")
        # The SAME signal id, now reported parked rather than firing.
        parked = self._signal(state=STATE_SUPPRESSED, suppressed_by="silence-abc")
        self.assertEqual(parked.id, signal.id)
        self._add_source([parked])
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications[incident.incident_id], VERIFY_UNKNOWN)
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertIn(stored.verification, OPEN_VERIFICATIONS)
        self.assertIn("parked", stored.verification_detail)
        # Attribution named, so the operator does not have to hunt for who muted it.
        self.assertIn("silence-abc", stored.verification_detail)

    async def test_a_drained_push_spool_does_not_confirm_the_action(self):
        """Absence from a SUCCESSFUL poll is not always evidence — the `ok` guard's blind spot.

        The webhook source is a spool: `poll` drains it, so a delivered signal appears in
        exactly one cycle's result and is absent from every cycle after, whether or not
        anything changed at the sender. `poll_health` recorded that empty drain as
        `{"ok": True, "signals": 0}`, which the recheck read as "the source answered and
        the signal is gone" — so one cycle after any webhook delivery an action verified as
        `cleared` with the fault still live at the sender.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            OPEN_VERIFICATIONS,
            VERIFY_UNKNOWN,
        )

        signal = self._signal()
        incident = self._acted_incident(signal, action="resolve")

        class _Spool:
            id = "fake"  # the SAME source id the signal names
            display_name = "Spool"
            # The one property that separates this from every polled API.
            is_snapshot = False

            def configured(self):
                return True

            async def poll(self):
                return []  # drained

        self.registry.register_signal_source(_Spool())
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications[incident.incident_id], VERIFY_UNKNOWN)
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertIn(stored.verification, OPEN_VERIFICATIONS)
        self.assertIn("push", stored.verification_detail)

    async def test_a_snapshot_source_still_confirms_on_absence(self):
        """The fix above must not make every source unverifiable.

        `is_snapshot` defaults TRUE precisely so a companion adapter written before it
        existed keeps its previous, correct behaviour — only a source that genuinely
        drains a queue opts out.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import VERIFY_CLEARED

        signal = self._signal()
        incident = self._acted_incident(signal, action="resolve")
        # A plain fake declaring nothing — the default-true path.
        self._add_source([self._signal(native_id="alarm/other")])
        result = await dispatch.run_cycle()
        self.assertEqual(result.verifications[incident.incident_id], VERIFY_CLEARED)


if __name__ == "__main__":
    unittest.main()


class TestEveryInstancePullsTheSchedule(_HomeIsolated):
    """`rotation.yaml` travels in the ledger repo, and only the primary used to fetch it.

    `sync_safely`'s only other caller is the daily hygiene pass, which is gated to the
    primary instance. So a NON-primary instance had no code path that ever fetched the
    schedule: it kept arming (or not) off whatever it last saw. That is the double-claim
    the single-owner model exists to prevent, reintroduced by the transport rather than
    by the model.
    """

    async def test_the_cycle_pulls_before_it_reads_the_shift(self):
        """Ordering IS the fix, not an implementation detail.

        Pulling after `resolve_shift` would still gate THIS cycle on the stale file and
        only help the next one — so a shift swap would take effect one heartbeat late on
        every instance, and the window where two teammates both believe they are on call
        is precisely the window that matters.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, registry

        order: list[str] = []

        async def _pull():
            order.append("pull")
            return "pulled"

        real_resolve = registry.OpsProviderRegistry.resolve_shift

        async def _resolve(self):
            order.append("resolve_shift")
            return await real_resolve(self)

        with mock.patch.object(dispatch, "_pull_shared_repo_safely", _pull):
            with mock.patch.object(registry.OpsProviderRegistry, "resolve_shift", _resolve):
                await dispatch.run_cycle()

        self.assertEqual(
            order[:2], ["pull", "resolve_shift"], f"pull must precede the shift read: {order}"
        )

    async def test_an_unreachable_remote_does_not_stop_the_cycle(self):
        """Shared memory is worth having; it is never worth losing a claim over.

        Asserted through the REAL helper with a broken `sync_safely` underneath, rather
        than by stubbing the helper to raise. Stubbing the helper would only prove that
        `run_cycle` propagates whatever its callee raises — which is the opposite of the
        property, and a version of this test asserting `assertRaises(OSError)` passed
        while claiming the cycle survived.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            dispatch,
            ledger_sync,
        )

        with mock.patch.object(ledger_sync, "sync_safely", side_effect=OSError("no remote")):
            result = await dispatch.run_cycle()

        # The cycle ran to a real verdict instead of blowing up.
        self.assertIsNotNone(result)

    async def test_the_pull_helper_swallows_every_fault(self):
        """The helper is where the tolerance lives, so assert it there."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger_sync

        with mock.patch.object(ledger_sync, "sync_safely", side_effect=OSError("git gone")):
            self.assertEqual(await dispatch._pull_shared_repo_safely(), "")

    async def test_an_unconfigured_install_pays_nothing(self):
        """The overwhelmingly common case: no remote, so this must cost nothing at all."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self.assertEqual(await dispatch._pull_shared_repo_safely(), "")


class TestWebhookSpoolSurvivesReads(_HomeIsolated):
    """A push-delivered signal must survive every READ of it, and be consumed once.

    The bug this pins: `WebhookSignalSource.poll()` returned `drain()`, which cleared the
    deque with no re-enqueue path anywhere. `poll_all` has THREE callers and only one of
    them claims — the heartbeat, `GET /signals` (the Signals-tab "Poll now" button) and the
    claim-authorization re-poll. So an operator refreshing the board while alerts sat in the
    spool permanently destroyed them: signature-verified, accepted with a 200, then silently
    no incident and no trace. Reported in review as the dashboard path; the claim path was
    the same bug and worse (claiming ONE signal discarded every other queued delivery).
    """

    def setUp(self):
        super().setUp()
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        webhook.reset_spool()
        self._install_webhook_source()

    def tearDown(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        webhook.reset_spool()
        super().tearDown()

    def _install_webhook_source(self):
        """Register the REAL `WebhookSignalSource`, forced configured.

        Deliberately the real adapter rather than a fake returning a list: the whole defect
        lived in `poll()`'s spool interaction, so a fake that returns signals would pass
        against the broken code and prove nothing.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        class _AlwaysConfigured(webhook.WebhookSignalSource):
            def configured(self):
                return True

        self.registry.register_signal_source(_AlwaysConfigured())

    @staticmethod
    def _spool(*ids):
        """Put signals in the spool directly, bypassing HMAC verification."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        for native_id in ids:
            webhook._queue.append(
                Signal.create(source="webhook", native_id=native_id, title=f"{native_id} broke")
            )

    async def test_a_signals_read_does_not_destroy_the_spool(self):
        """The reported failure, end to end through the registry."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self._spool("a", "b", "c", "d", "e")

        # Two independent reads — the Signals tab, then the claim-authorization re-poll.
        first, _ = await self.registry.poll_all()
        second, _ = await self.registry.poll_all()

        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5, "a second read saw fewer signals: the poll consumed")
        self.assertEqual(webhook.queue_depth(), 5)

    async def test_claiming_consumes_only_what_it_claimed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self._spool("a", "b", "c")
        result = await dispatch.run_cycle(max_claims=1)

        self.assertEqual(len(result.claimed), 1)
        claimed_id = result.claimed[0].incident.signal.id
        # The claimed one is gone (its incident owns it now)...
        remaining = {s.id for s in webhook.peek()}
        self.assertNotIn(claimed_id, remaining)
        # ...and the two the cap deferred are STILL THERE for the next cycle. Before the
        # fix these were destroyed by the same poll that delivered them.
        self.assertEqual(len(remaining), 2)

    async def test_a_burst_larger_than_the_cap_is_fully_claimed_over_cycles(self):
        """The property an operator actually cares about: nothing delivered is lost."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self._spool(*[f"alarm-{n}" for n in range(5)])

        seen: set[str] = set()
        for _ in range(5):
            # A dashboard read between every cycle — the exact interleaving that lost data.
            await self.registry.poll_all()
            result = await dispatch.run_cycle(max_claims=1)
            seen.update(c.incident.signal.id for c in result.claimed)

        self.assertEqual(len(seen), 5, "a delivered signal was never claimed")
        self.assertEqual(webhook.queue_depth(), 0)


class TestTheBriefRedactsProviderMetadata(_HomeIsolated):
    """Provider-controlled signal fields must be redacted before the MODEL sees them.

    `registry.gather_evidence` already redacts every evidence BODY centrally, but the brief
    also prints the signal's own metadata — title, resource, url — and those were rendered raw.
    A signed webhook is accepted from anything able to POST JSON and a console link can carry a
    token in its query string, so that metadata is exactly as untrusted as a fetched log line.
    The brief goes into the agent's context and from there into the transcript. Found in review.
    """

    def _brief_for(self, **signal_kwargs):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, models, store

        signal = models.Signal.create(source="webhook", native_id="alert/1", **signal_kwargs)
        incident = store.claim(signal, operating_mode=models.MODE_OBSERVE)
        assert incident is not None
        return dispatch.investigation_brief(dispatch.ClaimedIncident(incident=incident))

    def test_a_credential_in_the_title_is_redacted(self):
        brief = self._brief_for(title="worker used AKIAIOSFODNN7EXAMPLE and 403'd")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", brief)
        self.assertIn("403", brief)  # redaction, not truncation

    def test_a_credential_in_the_resource_is_redacted(self):
        brief = self._brief_for(title="t", resource="arn:aws:x/AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", brief)

    def test_a_token_in_the_provider_url_query_is_redacted(self):
        brief = self._brief_for(title="t", url="https://x.example/a?key=AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", brief)

    def test_fields_this_app_owns_are_not_mangled(self):
        """`source`/`severity`/`fingerprint` are ours; redacting them could only corrupt a
        value the agent needs to reason about."""
        brief = self._brief_for(title="t", severity="critical")
        self.assertIn("webhook", brief)
        self.assertIn("critical", brief)


class TestOwnedRedeliveriesLeaveTheSpool(_HomeIsolated):
    """A redelivery for an already-open incident must be acked, not accumulated.

    `owned` ids are filtered out of `candidates`, so they are never claimed — and with only the
    `claimed` set acked they never left the spool either. A sender that redelivers while an
    investigation is in flight (Alertmanager repeats every `group_interval`; a webhook script
    retries) accumulated copies of a signal already being worked, and on a full 200-entry spool
    those evicted a NEW unclaimed alert. Same shape as the manual-claim gap one round earlier:
    every place a signal becomes or already IS durable has to acknowledge it. Found in review.
    """

    def setUp(self):
        super().setUp()
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        webhook.reset_spool()
        self._install_webhook_source()

    def tearDown(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        webhook.reset_spool()
        super().tearDown()

    def _install_webhook_source(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        class _AlwaysConfigured(webhook.WebhookSignalSource):
            def configured(self):
                return True

        self.registry.register_signal_source(_AlwaysConfigured())

    @staticmethod
    def _spool(*ids):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        for native_id in ids:
            webhook._queue.append(
                Signal.create(source="webhook", native_id=native_id, title=f"{native_id} broke")
            )

    async def test_a_redelivery_of_an_open_incidents_signal_is_acked(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        # Cycle 1: claim it, which acks the first copy.
        self._spool("alert/1")
        first = await dispatch.run_cycle()
        self.assertEqual(len(first.claimed), 1)
        self.assertEqual(webhook.queue_depth(), 0)

        # The sender redelivers while the investigation is still open.
        self._spool("alert/1")
        self.assertEqual(webhook.queue_depth(), 1)

        # Cycle 2 claims nothing (the incident already owns it) and must still drain it.
        second = await dispatch.run_cycle()
        self.assertEqual(len(second.claimed), 0)
        self.assertEqual(
            webhook.queue_depth(),
            0,
            "an owned redelivery stayed spooled and can evict a new alert",
        )

    async def test_an_unclaimed_alert_is_not_evicted_by_redeliveries(self):
        """The operator-visible property: the new alert still gets claimed."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self._spool("alert/1")
        await dispatch.run_cycle(max_claims=1)

        # A pile of redeliveries for the open incident, then a genuinely new alert.
        for _ in range(5):
            self._spool("alert/1")
        self._spool("alert/2")

        result = await dispatch.run_cycle(max_claims=1)
        claimed_ids = {c.incident.signal.id for c in result.claimed}
        self.assertIn("webhook:alert/2", claimed_ids)
        self.assertEqual(webhook.queue_depth(), 0)
