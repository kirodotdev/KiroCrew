"""Tests for the Ops Mission Control data model.

The fingerprint tests are the important ones: fingerprint stability is what makes
the knowledge ledger work at all. If a fingerprint drifts per occurrence, a repeat
failure never matches its ancestor and the whole compounding-memory premise fails
silently — the app keeps working, it just stops learning.
"""

import unittest

from kiro_crew.apps.builtins.ops_mission_control.backend import models


class TestFingerprint(unittest.TestCase):
    def test_stable_across_timestamps_and_numbers(self):
        """The same failure tomorrow, with different numbers, is the same pattern."""
        a = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/rds",
            title="RDS connections above 800 at 2026-07-30T12:00:00Z",
            resource="AWS/RDS/DatabaseConnections",
        )
        b = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/rds",
            title="RDS connections above 950 at 2026-07-31T04:22:11Z",
            resource="AWS/RDS/DatabaseConnections",
        )
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_stable_across_instance_ids_and_uuids(self):
        a = models.Signal.create(
            source="datadog",
            native_id="monitor/1",
            title="ingest failed on i-0abc123def456789",
            resource="ingest",
        )
        b = models.Signal.create(
            source="datadog",
            native_id="monitor/1",
            title="ingest failed on i-0999888777666555",
            resource="ingest",
        )
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_distinct_failures_differ(self):
        a = models.Signal.create(
            source="cloudwatch", native_id="alarm/rds", title="RDS connections high"
        )
        b = models.Signal.create(
            source="cloudwatch", native_id="alarm/dlq", title="DLQ depth exceeded"
        )
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_same_title_different_source_differs(self):
        """Provider is part of identity — the same words mean different things."""
        a = models.Signal.create(source="cloudwatch", native_id="x", title="latency high")
        b = models.Signal.create(source="datadog", native_id="x", title="latency high")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_signal_id_is_provider_scoped(self):
        s = models.Signal.create(source="pagerduty", native_id="incident/PABC", title="t")
        self.assertEqual(s.id, "pagerduty:incident/PABC")


class TestNormalization(unittest.TestCase):
    def test_severity_vocabularies(self):
        self.assertEqual(models.normalize_severity("P1"), models.SEVERITY_CRITICAL)
        self.assertEqual(models.normalize_severity("sev-2"), models.SEVERITY_WARNING)
        self.assertEqual(models.normalize_severity("low"), models.SEVERITY_INFO)
        self.assertEqual(models.normalize_severity("critical"), models.SEVERITY_CRITICAL)

    def test_unknown_severity_is_warning_not_critical(self):
        """An unparseable provider must not be able to manufacture top priority."""
        self.assertEqual(models.normalize_severity("banana"), models.SEVERITY_WARNING)
        self.assertEqual(models.normalize_severity(""), models.SEVERITY_WARNING)

    def test_every_separator_spelling_maps_to_the_same_level(self):
        """The separator must not decide the severity.

        The tables listed ``sev1`` and ``sev-1`` but not ``sev_1``, ``sev 1`` or
        ``sev.1``, so an underscore-separated vocabulary had EVERY level floored to
        ``warning`` — a genuine SEV_1 reached the board looking like a warning. The
        conservative unknown-value fallback is what hid it: nothing raised, nothing
        logged, and the wrong answer was a plausible one.

        Asserted across all five spellings per level rather than just the underscore
        that was reported, because the next vocabulary will pick a sixth separator.
        """
        for suffix, want in (
            ("1", models.SEVERITY_CRITICAL),
            ("2", models.SEVERITY_WARNING),
            ("3", models.SEVERITY_INFO),
        ):
            for sep in ("", "-", "_", " ", "."):
                for case in (str.lower, str.upper):
                    raw = case(f"sev{sep}{suffix}")
                    with self.subTest(raw=raw):
                        self.assertEqual(models.normalize_severity(raw), want)

    def test_an_unknown_level_number_still_falls_back(self):
        """Folding separators must not turn every ``sev<n>`` into a known level."""
        self.assertEqual(models.normalize_severity("SEV_9"), models.SEVERITY_WARNING)

    def test_folding_separators_does_not_manufacture_a_match(self):
        """The other direction of the same fix: dropping ``-`` can JOIN words.

        ``non-critical`` folds to ``noncritical`` and ``not ok`` to ``notok``, so a
        lookup that matched by substring — or a table entry that happened to be a
        suffix — would read a NEGATED phrase as the level it negates, which is the
        worst possible misread: "non-critical" reported as ``critical`` inverts the
        operator's meaning rather than merely losing it.

        Safe today because the tables use exact set membership, so ``notok`` is
        simply not an entry. Pinned because that safety is a property of HOW the
        lookup is written, not of the values — a future refactor to ``any(k in v
        for k in ...)`` would pass every other test in this class.
        """
        for raw in (
            "non-critical",
            "non critical",
            "not ok",
            "not-ok",
            "no_error",
            "ok-ish",
            "sev 10",
            "p 10",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(models.normalize_severity(raw), models.SEVERITY_WARNING)

    def test_unknown_state_is_unknown_not_firing(self):
        """An unparseable state must not create phantom work on the board."""
        self.assertEqual(models.normalize_state("banana"), models.STATE_UNKNOWN)
        self.assertEqual(models.normalize_state("triggered"), models.STATE_FIRING)
        self.assertEqual(models.normalize_state("resolved"), models.STATE_OK)


class TestProviderSideSuppressionIsReadable(unittest.TestCase):
    """ "A human parked this at the provider" must be distinguishable from every other state.

    Before this, the entire suppression vocabulary normalized to ``unknown`` — the same
    answer ``banana`` gets — so an adapter had two wrong options: report ``firing`` and
    investigate something an operator explicitly parked, or drop the signal so that "the
    app ignored my alarm" and "someone silenced it" look identical.
    """

    def test_every_providers_word_for_parked_reads_as_suppressed(self):
        """The vocabularies differ per provider; all of them used to land in `unknown`."""
        for raw in (
            "suppressed",  # Alertmanager status.state
            "silenced",
            "inhibited",  # Alertmanager, masked by a higher-ranked alert
            "muted",  # Datadog
            "snoozed",
            "downtime",  # Icinga
            "in downtime",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(models.normalize_state(raw), models.STATE_SUPPRESSED)

    def test_the_whole_v2_status_enum_parses_not_just_the_parked_value(self):
        """Found while implementing this: admitting only `suppressed` was worse than nothing.

        Alertmanager's v2 `alertStatus.state` enum is `{unprocessed, active, suppressed}`.
        Teaching the reader the object shape while leaving the other two values falling to
        `unknown` would mean the v2 payload parses a SILENCED alert correctly and drops a
        LIVE one — the app going quiet on real work in exchange for reading a mute.
        """
        self.assertEqual(models.normalize_state("active"), models.STATE_FIRING)
        self.assertEqual(models.normalize_state("unprocessed"), models.STATE_FIRING)
        self.assertEqual(models.normalize_state("suppressed"), models.STATE_SUPPRESSED)

    def test_suppressed_is_not_unknown(self):
        """The two mean opposite things: 'we read it' versus 'we could not read it'."""
        self.assertNotEqual(models.normalize_state("suppressed"), models.normalize_state("banana"))

    def test_suppressed_is_a_valid_state(self):
        """`from_dict` re-normalizes, so a state outside VALID_STATES would not round-trip."""
        self.assertIn(models.STATE_SUPPRESSED, models.VALID_STATES)

    def test_acknowledged_is_still_firing(self):
        """Deliberate: an acknowledged page is unresolved and the point is to work it.

        Pinned because "ack means someone handled it" is the intuitive-and-wrong reading,
        and mapping it here would silently stop the app responding to live pages.
        """
        self.assertEqual(models.normalize_state("acknowledged"), models.STATE_FIRING)

    def test_a_suppressed_signal_carries_its_attribution(self):
        """Who parked it is what separates 'ignored' from 'silenced' for an operator."""
        signal = models.Signal.create(
            source="webhook",
            native_id="fp1",
            title="disk full",
            state="suppressed",
            suppressed_by="silence-7f3",
            suppressed_reason="silenced",
        )
        self.assertEqual(signal.state, models.STATE_SUPPRESSED)
        self.assertEqual(signal.suppressed_by, "silence-7f3")
        self.assertEqual(signal.suppressed_reason, "silenced")

    def test_attribution_is_not_namespaced_by_source(self):
        """Unlike `provider_key` this is display text, not a match key.

        Prefixing it would put "webhook:silence-7f3" on the board where the operator wants
        the silence id they can paste into Alertmanager.
        """
        signal = models.Signal.create(
            source="webhook", native_id="x", title="t", suppressed_by="silence-7f3"
        )
        self.assertEqual(signal.suppressed_by, "silence-7f3")

    def test_an_incident_written_before_these_fields_still_loads(self):
        """New persisted fields must default empty or every on-disk incident breaks."""
        legacy = {
            "id": "webhook:old",
            "source": "webhook",
            "title": "an alarm from before",
            "state": "firing",
        }
        signal = models.Signal.from_dict(legacy)
        self.assertEqual(signal.suppressed_by, "")
        self.assertEqual(signal.suppressed_reason, "")

    def test_attribution_round_trips_through_disk(self):
        signal = models.Signal.create(
            source="webhook",
            native_id="x",
            title="t",
            state="inhibited",
            suppressed_by="ClusterDown",
            suppressed_reason="inhibited",
        )
        restored = models.Signal.from_dict(signal.to_dict())
        self.assertEqual(restored.state, models.STATE_SUPPRESSED)
        self.assertEqual(restored.suppressed_by, "ClusterDown")
        self.assertEqual(restored.suppressed_reason, "inhibited")

    def test_reading_a_suppression_is_not_the_same_word_as_issuing_one(self):
        """`ACTION_SILENCE` is a verb the app issues; this state is a fact it reads.

        One word for both would merge our own outbound intent with another party's
        decision, and only one of those is a fact about the world.
        """
        self.assertNotIn(models.STATE_SUPPRESSED, models.VALID_ACTIONS)
        self.assertNotIn(models.ACTION_SILENCE, models.VALID_STATES)


class TestEffectiveMode(unittest.TestCase):
    def test_rule_cannot_escalate_above_app_ceiling(self):
        """An operator pinned to observe cannot be overridden by a rule."""
        self.assertEqual(
            models.effective_mode(models.MODE_OBSERVE, models.MODE_ACT),
            models.MODE_OBSERVE,
        )

    def test_rule_narrows(self):
        self.assertEqual(
            models.effective_mode(models.MODE_ACT, models.MODE_PROPOSE),
            models.MODE_PROPOSE,
        )

    def test_no_rule_uses_app_default(self):
        self.assertEqual(models.effective_mode(models.MODE_ACT, None), models.MODE_ACT)

    def test_unknown_mode_falls_to_observe(self):
        self.assertEqual(models.effective_mode("nonsense", None), models.MODE_OBSERVE)


class TestTransitionGrammar(unittest.TestCase):
    def test_cannot_resolve_without_being_claimed(self):
        """Resolving requires a claim first, so every resolution has a timeline.

        Note this is narrower than "an investigation happened": a CLAIMED incident
        may resolve without one when its signal simply stopped firing (see
        ``test_dispatched_can_resolve_when_the_signal_clears``). What stays
        forbidden is resolving something that was never claimed at all, which would
        produce a resolution with no incident record behind it.
        """
        self.assertNotIn(
            models.STATUS_RESOLVED,
            models.LEGAL_TRANSITIONS[models.STATUS_UNCLAIMED],
        )

    def test_dispatched_can_resolve_when_the_signal_clears(self):
        """Reconcile's core case, and it had no legal move before.

        A signal can clear between the claim and the agent's first turn (a flapping
        alarm; a GitHub issue closed a minute later). Without this edge the
        incident sticks at ``dispatched`` until the stale sweep hours later, so the
        board asserts work is in progress on a problem that no longer exists.
        Found by exercising the reconcile SOP against a real cleared signal.
        """
        self.assertIn(
            models.STATUS_RESOLVED,
            models.LEGAL_TRANSITIONS[models.STATUS_DISPATCHED],
        )

    def test_stale_can_resolve_when_the_signal_clears(self):
        """Otherwise reconcile's only move is to re-dispatch a dead signal, spending
        a whole investigation to conclude nothing is wrong."""
        self.assertIn(
            models.STATUS_RESOLVED,
            models.LEGAL_TRANSITIONS[models.STATUS_STALE],
        )

    def test_terminal_states_have_no_exits(self):
        self.assertEqual(models.LEGAL_TRANSITIONS[models.STATUS_RESOLVED], frozenset())
        self.assertEqual(models.LEGAL_TRANSITIONS[models.STATUS_ESCALATED], frozenset())

    def test_stale_can_be_reclaimed(self):
        self.assertIn(models.STATUS_DISPATCHED, models.LEGAL_TRANSITIONS[models.STATUS_STALE])

    def test_every_status_has_a_rule(self):
        """A status with no entry would raise a KeyError in the store at runtime."""
        for status in (
            models.STATUS_UNCLAIMED,
            models.STATUS_DISPATCHED,
            models.STATUS_INVESTIGATING,
            models.STATUS_NEEDS_HUMAN,
            models.STATUS_RESOLVED,
            models.STATUS_ESCALATED,
            models.STATUS_STALE,
        ):
            self.assertIn(status, models.LEGAL_TRANSITIONS)


class TestRoundTrip(unittest.TestCase):
    def test_signal_round_trip(self):
        s = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/x",
            title="t",
            resource="r",
            labels={"k": "v"},
        )
        again = models.Signal.from_dict(s.to_dict())
        self.assertEqual(s, again)

    def test_incident_round_trip(self):
        s = models.Signal.create(source="cloudwatch", native_id="alarm/x", title="t")
        inc = models.Incident(incident_id="INV-1", signal=s, ledger_matches=["abc"])
        again = models.Incident.from_dict(inc.to_dict())
        self.assertEqual(again.incident_id, "INV-1")
        self.assertEqual(again.signal, s)
        self.assertEqual(again.ledger_matches, ["abc"])

    def test_malformed_incident_dict_does_not_raise(self):
        """A corrupt index entry must degrade, not crash the board."""
        inc = models.Incident.from_dict({"incident_id": "INV-9", "ledger_matches": "oops"})
        self.assertEqual(inc.incident_id, "INV-9")
        self.assertEqual(inc.ledger_matches, [])


class TestLedgerEntryIdentity(unittest.TestCase):
    def test_content_addressed_id_is_deterministic(self):
        """Two people learning the same lesson must produce the same id — that is
        what makes a git-merged ledger a dedupe rather than a conflict."""
        a = models.LedgerEntry.create(pattern="DLQ fills up", fix="clear and redrive")
        b = models.LedgerEntry.create(pattern="dlq fills up", fix="Clear and redrive")
        self.assertEqual(a.entry_id, b.entry_id)

    def test_different_fix_is_a_different_entry(self):
        a = models.LedgerEntry.create(pattern="DLQ fills up", fix="clear and redrive")
        b = models.LedgerEntry.create(pattern="DLQ fills up", fix="raise concurrency")
        self.assertNotEqual(a.entry_id, b.entry_id)


class TestTheLedgerRecordCarriesItsFormatVersion(unittest.TestCase):
    """``ledger.jsonl`` is the one artifact that leaves the machine.

    ``ledger_sync`` git-pushes it and teammates on DIFFERENT Kiro Crew builds pull it, so an
    older instance can be handed a row a newer one wrote. With no version there was no way
    to notice: the reader coerces what it recognises and defaults what it does not, so a row
    it only partly understands reads as fully understood. Review called it the nearest thing
    in this app to a one-way door, and the retrofit is only free while there is one version.
    """

    def test_a_new_entry_is_stamped_with_the_current_version(self):
        entry = models.LedgerEntry.create(pattern="p", fix="f")
        self.assertEqual(entry.v, models.LEDGER_RECORD_V1)
        self.assertEqual(entry.to_dict()["v"], models.LEDGER_RECORD_V1)

    def test_the_field_default_matches_the_constant(self):
        """The default is spelled as a literal, so nothing keeps the two equal but this.

        It cannot BE the constant: a dataclass field default is evaluated when the class is
        built, and ``test_ledger_sync_git`` evicts this module from ``sys.modules`` mid-test
        to simulate two instances — a name resolved at class-creation time then hits a
        half-initialised module and raises ``NameError``. Observed exactly that.
        """
        import dataclasses

        default = {f.name: f.default for f in dataclasses.fields(models.LedgerEntry)}["v"]
        self.assertEqual(default, models.LEDGER_RECORD_V1)

    def test_a_line_written_before_the_field_existed_reads_as_v1(self):
        """The whole reason this was cheap to add now: absent means 1."""
        entry = models.LedgerEntry.from_dict({"entry_id": "abc", "pattern": "p", "fix": "f"})
        self.assertEqual(entry.v, models.LEDGER_RECORD_V1)

    def test_an_unparseable_version_reads_as_v1_rather_than_raising(self):
        """This reader salvages a git-merged team ledger; it does not get to reject it."""
        junk_values: tuple[object, ...] = ("", "two", None, [], {})
        for junk in junk_values:
            entry = models.LedgerEntry.from_dict(
                {"entry_id": "abc", "pattern": "p", "fix": "f", "v": junk}
            )
            self.assertEqual(entry.v, models.LEDGER_RECORD_V1, f"v={junk!r}")

    def test_a_future_version_survives_the_round_trip(self):
        """A newer row must keep its stamp, so a reader can act on it later."""
        entry = models.LedgerEntry.from_dict(
            {"entry_id": "abc", "pattern": "p", "fix": "f", "v": 99}
        )
        self.assertEqual(entry.v, 99)
        self.assertEqual(models.LedgerEntry.from_dict(entry.to_dict()).v, 99)


if __name__ == "__main__":
    unittest.main()
