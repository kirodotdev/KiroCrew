"""Tests for the autonomy gate's checks beyond the rule match.

The off-shift check must consult every configured rotation, not just the schedule file, and a
malformed ``actions`` scope must reject the rule rather than widen it to every action.
"""

from kiro_crew.apps.builtins.ops_mission_control.tests.store_and_gate_helpers import _HomeIsolated


class TestTheWriteGateConsultsEveryRotation(_HomeIsolated):
    """`_definitely_off_shift` must honour EVERY configured rotation, not just the file.

    It read `rotation.yaml` and returned False at the first line when absent — so a PagerDuty
    rotation reporting "someone else is on call" was invisible to the write gate, and
    `/incident/action` executed a production write against a provider this operator was not on
    call for. The rotation was consulted for TIER arming (via `registry.resolve_shift`) and
    ignored for AUTHORIZATION, which is the path where it matters most. Found in review.
    """

    def _install(self, *sources):
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        reg = registry.OpsProviderRegistry()
        registry._registry = reg
        self.addCleanup(registry.reset_registry)
        for src in sources:
            reg.register_rotation_source(src)
        return reg

    @staticmethod
    def _source(source_id, *, on_shift, unknown=False, fallback=False, configured=True):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        class _Src:
            id = source_id
            is_fallback = fallback

            def configured(self):
                return configured

            def _on_shift_sync(self):
                return ShiftStatus(on_shift=on_shift, unknown=unknown)

        return _Src()

    def test_an_api_rotation_reporting_off_shift_blocks_the_write(self):
        """The reported bug: no schedule file on disk, PagerDuty says off shift."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=False))
        self.assertTrue(rotation._definitely_off_shift())

    def test_on_shift_anywhere_allows_the_write(self):
        """A person on two rotations is on call — matches `resolve_shift`'s algebra."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(
            self._source("pagerduty", on_shift=False),
            self._source("schedule-file", on_shift=True),
        )
        self.assertFalse(rotation._definitely_off_shift())

    def test_the_always_on_fallback_cannot_veto_a_real_rotation(self):
        """`AlwaysOnRotationSource` is always configured and always on-shift, so counting it
        would make every real rotation unhearable — the exact bug already fixed once in
        `resolve_shift`."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(
            self._source("always-on", on_shift=True, fallback=True),
            self._source("pagerduty", on_shift=False),
        )
        self.assertTrue(rotation._definitely_off_shift())

    def test_unknown_is_not_an_off_shift_vote(self):
        """An unreachable API must not block an operator driving an action by hand."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=False, unknown=True))
        self.assertFalse(rotation._definitely_off_shift())

    def test_no_configured_rotation_allows_the_write(self):
        """A solo install has no rotation to be off.

        Expressed as `unknown`, NOT as `configured=False`. A source reporting
        `on_shift=False, configured=False` with the write allowed would assert that the gate
        SKIPS an unconfigured source, which is the hole below rather than the property. A
        source that cannot answer says so with `unknown=True`; that is the contract every
        shipped source implements, and the one the gate reads.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=False, unknown=True))
        self.assertFalse(rotation._definitely_off_shift())

    def test_an_agent_cannot_manufacture_an_abstention_by_disabling_the_provider(self):
        """The refusal must not consult a predicate the constrained party can write.

        `configured()` reads `providers.<id>.enabled` from `data/config.json` for every adapter
        except the schedule file — and that file is agent-writable and served unauthenticated.
        A gate that skips any source where `configured()` is false lets ONE flag flip make a
        rotation source abstain, so no real source answers and the off-shift refusal stops
        firing without anything changing about who is actually on call.

        Reproduced end to end: PagerDuty reporting `on_shift=False` refuses the write; with
        `enabled: false` the same signal returns "granted by rule on cloudwatch".

        One instance of a general class — a security refusal must not read an input the
        constrained party can write. The gate asks EVERY non-fallback source and lets each
        report its own inability to answer as `unknown`, which is both safer and simpler.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        # Off shift, and claiming to be unconfigured — the shape the flag flip produces.
        self._install(self._source("pagerduty", on_shift=False, configured=False))
        self.assertTrue(
            rotation._definitely_off_shift(),
            "a source that CAN answer must be counted even when it reports unconfigured",
        )

    @staticmethod
    def _async_only_source(source_id, *, on_shift, unknown=False, raises=False, hangs=False):
        """A source implementing ONLY the public `RotationSource` contract.

        No `_on_shift_sync`, no `resolve_now` — those are private details of the two adapters
        this repo happens to ship. This is what a correctly-written companion source looks
        like against the documented Protocol.
        """
        import asyncio as _asyncio

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        class _AsyncSrc:
            id = source_id
            is_fallback = False

            def configured(self):
                return True

            async def on_shift(self):
                if raises:
                    raise RuntimeError("provider down")
                if hangs:
                    # flake-ok: pre-existing hang stub, cut by the patched timeout; moved verbatim
                    await _asyncio.sleep(3600)
                return ShiftStatus(on_shift=on_shift, unknown=unknown)

        return _AsyncSrc()

    def test_an_async_only_source_reporting_off_shift_blocks_the_write(self):
        """`async on_shift()` IS the contract; the sync cores are our own private shortcut.

        The vote probed `_on_shift_sync`/`resolve_now` and abstained when neither existed — so
        the off-shift refusal was silently inapplicable to any source that implemented the
        documented interface and nothing else. A companion rotation reported "someone else is
        on call", the vote counted it as not answering, and a matching act-rule executed a real
        provider write from an off-shift instance.

        Same shape as the four earlier holes in this refusal (forge the identity, disable
        strict gating, hide behind `configured()`, no source consulted at all) — reached this
        time by writing a CORRECT adapter. Verified before fixing: this exact source returned
        `(True, 'granted by rule on cloudwatch')`; it now returns the off-shift refusal.
        Found in review (GPT 5.6).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._async_only_source("companion-rota", on_shift=False))
        self.assertTrue(
            rotation._definitely_off_shift(),
            "a source offering only the public async contract was not consulted at all",
        )

    def test_an_async_only_source_reporting_on_shift_still_allows_the_write(self):
        """The same path must not become a blanket denial."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._async_only_source("companion-rota", on_shift=True))
        self.assertFalse(rotation._definitely_off_shift())

    def test_an_async_only_source_reporting_unknown_is_still_a_non_vote(self):
        """Indeterminacy keeps permitting, whichever contract the source implements."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._async_only_source("companion-rota", on_shift=False, unknown=True))
        self.assertFalse(rotation._definitely_off_shift())

    def test_an_async_only_source_that_raises_is_a_fault_not_an_absence(self):
        """The fault/absence split has to hold on the async path too.

        Swallowing the exception inside the coroutine driver would downgrade a fault to an
        abstention and permit the write during exactly the window the provider is down — the
        regression `test_a_faulting_rotation_source_blocks_the_write` exists to prevent, one
        code path over.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._async_only_source("companion-rota", on_shift=False, raises=True))
        self.assertTrue(rotation._definitely_off_shift())

    def test_a_hanging_async_source_times_out_as_a_fault(self):
        """A companion source is a network client we do not control, and this gate fronts an
        operator action — so it must not hold the request open, and a hang is a fault (the
        source could not clear this instance) rather than a shrug."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._async_only_source("companion-rota", on_shift=False, hangs=True))
        with mock.patch.object(rotation, "_ASYNC_SHIFT_TIMEOUT_SECS", 0.05):
            self.assertTrue(rotation._definitely_off_shift())

    def test_the_sync_core_is_still_preferred_when_present(self):
        """The fast path must not regress: a source with a sync core is not driven async."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        called: list[str] = []
        src = self._source("pagerduty", on_shift=False)
        original = src._on_shift_sync

        def _tracked():
            called.append("sync")
            return original()

        src._on_shift_sync = _tracked

        # `on_shift` would raise if it were reached, so preferring the sync core is asserted
        # by the absence of that failure as well as by the call record.
        async def _boom():
            raise AssertionError("async path used despite a sync core being available")

        src.on_shift = _boom
        self._install(src)
        self.assertTrue(rotation._definitely_off_shift())
        self.assertEqual(called, ["sync"])

    def test_a_faulting_rotation_source_blocks_the_write(self):
        """A FAULT is not an ABSENCE, and conflating them opened a window.

        A configured source that raises — PagerDuty timing out, a revoked token, DNS failing —
        is a source that WOULD have answered. Counting that as "no information" let an
        off-shift instance write during exactly the window a rotation API is down, which is
        also when a real incident is most likely. Found in review.

        The review's proposed fix was to deny whenever ANY source reports unknown. That would
        break the documented property that a broken rotation must not lock the operator out: an
        unconfigured source, an absent schedule file, and a PagerDuty with no `schedule_ids` all
        legitimately report `unknown`, and on a solo install that is the NORMAL state. So the
        two cases are separated instead of merged, and both halves are asserted here.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._raising_source("pagerduty"))
        self.assertTrue(
            rotation._definitely_off_shift(),
            "a source that exists and is failing must not read as 'no information'",
        )

    def test_an_unconfigured_source_still_permits_a_manual_action(self):
        """The other half: absence must stay a non-vote, or a missing config disables the app.

        Both call sites of `authorize_action` are authenticated dashboard handlers — an operator
        clicking — so permitting here means "a broken rotation does not block the human", not
        "the agent gets a free write". A solo install with no rotation configured is the
        common case and must keep working.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=True, unknown=True))
        self.assertFalse(rotation._definitely_off_shift())

        # And with no rotation sources registered at all.
        self._install()
        self.assertFalse(rotation._definitely_off_shift())

    def test_a_fault_cannot_be_masked_by_another_sources_unknown(self):
        """`faulted` is tracked separately from `answered` for this case.

        A single flag would let an `unknown` from a second source overwrite the fault and
        re-open the window — the shape of bug where two independently-correct branches combine
        into an incorrect result.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(
            self._raising_source("pagerduty"),
            self._source("schedule-file", on_shift=True, unknown=True),
        )
        self.assertTrue(rotation._definitely_off_shift())

    def test_a_real_on_shift_answer_still_beats_a_fault(self):
        """The refusal must not become unconditional: if a working source says this instance
        IS on call, that is a positive answer and the write proceeds even though a sibling
        source is down."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(
            self._raising_source("pagerduty"),
            self._source("schedule-file", on_shift=True),
        )
        self.assertFalse(rotation._definitely_off_shift())

    @staticmethod
    def _raising_source(source_id):
        class _Src:
            id = source_id
            is_fallback = False

            def configured(self):
                return True

            def _on_shift_sync(self):
                raise RuntimeError("rotation API unavailable")

        return _Src()

    def test_every_shipped_rotation_source_exposes_a_sync_core(self):
        """The gate finds each source's sync core by attribute lookup, so a real source that
        keeps that logic in a module function ABSTAINS — silently disabling off-shift detection.

        That is exactly what happened: `ScheduleFileRotationSource` had only the async
        `on_shift`, with the work in the module-level `resolve_now`, so the committed schedule
        stopped blocking off-shift writes. My own tests for this gate used fakes that DID have
        `_on_shift_sync`, so they all passed against the regression — the fake was more
        cooperative than the real thing. Assert against the shipped classes instead.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        for src in registry.get_registry().rotation_sources():
            if getattr(src, "is_fallback", False):
                continue  # a fallback is skipped by the vote and needs no sync core
            self.assertTrue(
                callable(getattr(src, "_on_shift_sync", None))
                or callable(getattr(src, "resolve_now", None)),
                f"rotation source {src.id!r} has no sync core, so the write gate cannot "
                f"consult it and it silently abstains",
            )

    def test_one_broken_source_does_not_decide_the_vote(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        class _Exploding:
            id = "boom"
            is_fallback = False

            def configured(self):
                return True

            def _on_shift_sync(self):
                raise RuntimeError("API down")

        self._install(_Exploding(), self._source("schedule-file", on_shift=True))
        self.assertFalse(rotation._definitely_off_shift())


class TestAMalformedActionScopeIsRefusedNotWidened(_HomeIsolated):
    """A typo in `actions` must reject the rule, never widen it.

    `authorize_action` reads `not rule.actions or action in rule.actions`, so an EMPTY set means
    "every action". Silently dropping unrecognised verbs therefore inverted the operator's
    intent: `actions: ["resovle"]` filtered down to nothing and the rule then authorized ack,
    resolve, comment AND silence — one typo turning a narrow grant into the blanket one this
    design refuses to let anyone express. Found in review.
    """

    def test_a_typo_in_a_verb_rejects_the_rule(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rule = rotation.AutonomyRule.from_dict(
            {
                "source": "cloudwatch",
                "mode": "act",
                "resource_glob": "prod-*",
                "actions": ["resovle"],  # typo
            }
        )
        self.assertIsNone(rule, "a typo'd verb produced a rule instead of a refusal")

    def test_a_typo_alongside_a_valid_verb_still_rejects(self):
        """Keeping the good half would still grant something the operator did not write."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self.assertIsNone(
            rotation.AutonomyRule.from_dict(
                {
                    "source": "cloudwatch",
                    "mode": "act",
                    "resource_glob": "prod-*",
                    "actions": ["ack", "resovle"],
                }
            )
        )

    def test_an_empty_or_non_list_actions_key_rejects(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        bad_values: list[object] = [[], "ack", {}, None]
        for bad in bad_values:
            self.assertIsNone(
                rotation.AutonomyRule.from_dict(
                    {
                        "source": "cloudwatch",
                        "mode": "act",
                        "resource_glob": "prod-*",
                        "actions": bad,
                    }
                ),
                f"actions={bad!r} produced a rule",
            )

    def test_omitting_the_key_still_means_every_action(self):
        """Deliberate by omission, and what the manual documents — distinct from a malformed
        narrowing, which is a mistake."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rule = rotation.AutonomyRule.from_dict(
            {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
        )
        assert rule is not None
        self.assertEqual(rule.actions, frozenset())

    def test_a_valid_narrowing_is_preserved_exactly(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rule = rotation.AutonomyRule.from_dict(
            {
                "source": "cloudwatch",
                "mode": "act",
                "resource_glob": "prod-*",
                "actions": ["ack", "comment"],
            }
        )
        assert rule is not None
        self.assertEqual(rule.actions, frozenset({"ack", "comment"}))

    def test_the_route_surfaces_the_refusal_as_a_400(self):
        """`save_rules` must not store the rule and must name the offending index, so the
        operator fixes the typo instead of unknowingly running with more authority."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        ok, code, _ = rotation.save_rules(
            [
                {
                    "source": "cloudwatch",
                    "mode": "act",
                    "resource_glob": "prod-*",
                    "actions": ["resovle"],
                }
            ]
        )
        self.assertFalse(ok)
        self.assertEqual(code, "rule_0_invalid")
        self.assertEqual(rotation.load_rules(), [])
