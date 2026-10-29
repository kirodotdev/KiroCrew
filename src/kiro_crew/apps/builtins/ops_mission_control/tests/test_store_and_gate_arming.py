"""Tests for arming the shift tiers.

Which crons each tier pauses (``rotation-check`` stays on the always tier, so an off-shift
instance can re-arm itself), ``rotation.apply_tiers`` arming without the agent choosing what
to pause, and ``POST /rotation/arm`` reporting a cron store that refuses the write or was
unreadable before the arm began.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import rotation, routes
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus
from kiro_crew.apps.builtins.ops_mission_control.tests.store_and_gate_helpers import _HomeIsolated
from kiro_crew.cron import CronService, CronStoreUnreadable


class TestTierArming(_HomeIsolated):
    def test_a_failing_rotation_api_still_arms_the_tier(self):
        """Fail-open is preserved — but the SOURCE decides it, not the gate.

        Asserting that ``on_shift=False, unknown=True`` arms the tier would encode
        ``on_shift or unknown`` into the gate, which silently defeats strict gating: a
        committed schedule that cannot say whether this operator is on call returns exactly
        that shape, and the ``or`` re-arms every teammate.

        The intent is "an unreachable API must not disable response", and it holds:
        ``pagerduty.on_shift`` returns ``on_shift=True, unknown=True`` on any
        failure, which is the shape asserted here. Two sources, two policies for "cannot
        tell", one gate that just reads ``on_shift``.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        # Exactly what pagerduty.py returns when its API cannot answer.
        states = rotation.tier_states(ShiftStatus(on_shift=True, unknown=True))
        self.assertTrue(states[rotation.TIER_ON_SHIFT])

    def test_an_indeterminate_schedule_disarms_the_tier(self):
        """The other half: a strict source's "cannot tell" must reach the crons.

        `resolve_now` returning ``on_shift=False`` is only meaningful if the tier map
        honours it. This is the assertion whose absence let the ``or`` survive.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        states = rotation.tier_states(ShiftStatus(on_shift=False, unknown=True))
        self.assertFalse(states[rotation.TIER_ON_SHIFT])
        self.assertTrue(states[rotation.TIER_ALWAYS], "rotation-check must survive to re-arm")

    def test_reconcile_is_on_shift_gated_not_always(self):
        """It mutates SHARED state, so exactly one instance may run it.

        `reconcile` POSTs `incident/transition` and rewrites the incident's Slack message.
        On the `always` tier every teammate raced to resolve the same incidents and edit
        the same thread — the shared-state mutation the single-owner model prevents, on the
        reconciliation path instead of the claim path. The equivalent reconciler is gated
        equivalent (`dispatch-snapshot`) the same way.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        on_shift = rotation.crons_for_tier(rotation.TIER_ON_SHIFT)
        self.assertIn("ops-mission-control/reconcile", on_shift)
        self.assertNotIn(
            "ops-mission-control/reconcile",
            rotation.crons_for_tier(rotation.TIER_ALWAYS),
        )
        # rotation-check must stay always-on, or nothing can re-arm the gated tier.
        self.assertEqual(
            rotation.crons_for_tier(rotation.TIER_ALWAYS), ("ops-mission-control/rotation-check",)
        )

    def test_off_shift_disarms(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        states = rotation.tier_states(ShiftStatus(on_shift=False))
        self.assertFalse(states[rotation.TIER_ON_SHIFT])
        # The always tier must stay armed or the instance cannot re-arm itself.
        self.assertTrue(states[rotation.TIER_ALWAYS])

    def test_rotation_check_is_on_the_always_tier(self):
        """If rotation-check were on-shift-gated, an off-shift instance could
        never turn itself back on."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self.assertIn(
            "ops-mission-control/rotation-check",
            rotation.crons_for_tier(rotation.TIER_ALWAYS),
        )

    def test_arming_is_server_side_not_agent_held(self):
        """The app must not hold `cron_pause`/`cron_resume`.

        With tier arming as the agent's job, the only thing stopping it pausing
        `rotation-check` — the sole always-tier job, so the only one that can re-arm a
        gated instance — is a sentence of SOP prose. Prose is not enforcement. `mcpTools`
        now IS enforced for an app's own agent sessions (the framework excludes every
        undeclared managed tool via managedToolPolicy), but this app's crons run on the
        host default agent (no `agent:` set), which that enforcement does not scope, so
        the manifest entry alone is not a gate here. Arming lives in
        `rotation.apply_tiers`; the capability must be absent from the manifest, or
        a future SOP edit can quietly hand the loaded gun back.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        manifest = json.loads(
            (Path(rotation.__file__).resolve().parents[1] / "app.json").read_text(encoding="utf-8")
        )
        tools = manifest["permissions"]["mcpTools"]
        self.assertNotIn("cron_pause", tools)
        self.assertNotIn("cron_resume", tools)

    def test_rotation_check_prompt_points_at_the_arm_route(self):
        """The cron message must POST /rotation/arm, not hand-pick crons to pause.

        Pins the manifest prompt to the enforced path. A revert to "pause the crons in
        tier_crons.on_shift" would re-open the hole even with the route in place.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        manifest = json.loads(
            (Path(rotation.__file__).resolve().parents[1] / "app.json").read_text(encoding="utf-8")
        )
        message = next(c["message"] for c in manifest["crons"] if c["name"] == "rotation-check")
        self.assertIn("/rotation/arm", message)
        self.assertNotIn("cron_pause", message)
        self.assertNotIn("cron_resume", message)

    def test_protected_cron_names_is_derived_from_the_tier_table(self):
        """The protected set must BE the always tier, not a second hand-kept list.

        Restating the names would let a job move onto `always` while the guard kept
        protecting the old one — protected in one place, forgotten in the other.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self.assertEqual(
            rotation.protected_cron_names(),
            frozenset(rotation.crons_for_tier(rotation.TIER_ALWAYS)),
        )
        self.assertIn("ops-mission-control/rotation-check", rotation.protected_cron_names())

    def test_the_spec_table_matches_the_shipped_tiers_and_enabled_flags(self):
        """The SPEC is the thing that drifted, so the spec is what this asserts against.

        `docs/system-specs/modules/ops-mission-control.md` claimed `reconcile` was on the
        `always` tier and that "all four ship `enabled: false`". Both were false, and the
        `always` claim was false in the dangerous direction: an edit trued up to the prose
        would have re-armed `reconcile` on an ungated tier, which is the multi-instance write
        race the tier move exists to prevent (every instance resolving the same incidents and
        rewriting the same Slack thread). Review caught it; prose alone will re-rot, so the
        table is pinned here.

        Parses the markdown table rather than restating it, so the check cannot pass by being
        updated in lockstep with a wrong table.
        """
        import json
        import re
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        pkg = Path(rotation.__file__).resolve().parents[1]
        manifest = json.loads((pkg / "app.json").read_text(encoding="utf-8"))
        # repo_root/src/kiro_crew/apps/builtins/<app> -> repo_root
        spec = pkg.parents[4] / "docs/system-specs/modules/ops-mission-control.md"
        if not spec.exists():  # pragma: no cover — docs are absent in a wheel install
            self.skipTest("spec not present in this install")

        rows = re.findall(
            r"^\|\s*`([a-z-]+)`\s*\|[^|]*\|\s*\**([a-z_]+)\**\s*\|\s*([^|]+)\|",
            spec.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertEqual(len(rows), 4, f"expected the four-cron spec table, parsed {rows}")

        tier_of = {
            name.split("/", 1)[1]: tier
            for tier, names in rotation.TIER_CRONS.items()
            for name in names
        }
        enabled_of = {c["name"]: bool(c.get("enabled")) for c in manifest.get("crons", [])}

        for cron, spec_tier, ships in rows:
            with self.subTest(cron=cron):
                self.assertEqual(spec_tier, tier_of[cron], f"spec tier for {cron} is stale")
                # The "Ships" cell says "paused" or "live"; both must match the manifest.
                spec_live = "live" in ships.lower()
                self.assertEqual(
                    spec_live,
                    enabled_of[cron],
                    f"spec says {cron} ships {'live' if spec_live else 'paused'}, "
                    f"manifest says enabled={enabled_of[cron]}",
                )

    def test_tier_cron_names_match_the_manifest(self):
        """Every tier cron must be a job the scheduler actually has.

        Derived from `app.json` rather than hardcoded, so adding or renaming a
        manifest cron fails here instead of silently producing a tier that
        pauses/resumes nothing. This shipped broken: `TIER_CRONS` carried bare
        `omc-*` names while registration namespaces them as
        `ops-mission-control/<name>`, so every pause/resume the rotation tier emitted
        targeted a job that did not exist and the whole tier mechanism was inert.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        manifest = json.loads(
            (Path(rotation.__file__).resolve().parents[1] / "app.json").read_text(encoding="utf-8")
        )
        app_name = manifest["name"]
        registered = {f"{app_name}/{c['name']}" for c in manifest.get("crons", [])}
        self.assertTrue(registered, "manifest must declare crons")

        tiered = {name for names in rotation.TIER_CRONS.values() for name in names}
        self.assertEqual(
            tiered,
            registered,
            "TIER_CRONS must name exactly the crons app registration creates",
        )

    def test_sop_frontmatter_cron_names_match_the_manifest(self):
        """Each SOP's `cron:` must name the job that actually runs it.

        The crons bind to SOPs by FILE PATH, so a stale name here breaks nothing at
        runtime — which is precisely why it is dangerous: it was the stale `omc-*`
        frontmatter that misled `TIER_CRONS` into naming crons that were never
        registered, silently disabling tier arming. Documentation that lies in the
        same direction as a real bug is worth a test.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        app_dir = Path(rotation.__file__).resolve().parents[1]
        manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
        registered = {f"{manifest['name']}/{c['name']}" for c in manifest.get("crons", [])}

        sops = app_dir.parents[2] / "builtin_skills" / "ops-mission-control" / "sops"
        if not sops.is_dir():  # pragma: no cover - python-only checkout
            self.skipTest("builtin_skills not present in this checkout")

        declared = {}
        for sop in sorted(sops.glob("*.md")):
            for line in sop.read_text(encoding="utf-8").splitlines()[:8]:
                if line.startswith("cron:"):
                    value = line.split(":", 1)[1].strip()
                    if value and value != "null":
                        declared[sop.name] = value
                    break

        self.assertTrue(declared, "at least one SOP must declare a cron")
        for filename, cron_name in declared.items():
            self.assertIn(
                cron_name,
                registered,
                f"{filename} declares cron {cron_name!r}, which no manifest cron creates",
            )

    def test_describe_exposes_per_tier_crons(self):
        """The rotation-check SOP pauses `tier_crons.on_shift` and nothing else.

        Without this field the only cron list on the response is `armed_crons` — the
        flat union across armed tiers — and an agent told to pause "the armed crons"
        would pause `omc-rotation-check` itself (see the next test).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        described = rotation.describe(ShiftStatus(on_shift=True))
        self.assertIn("tier_crons", described)
        self.assertEqual(
            described["tier_crons"][rotation.TIER_ON_SHIFT],
            list(rotation.crons_for_tier(rotation.TIER_ON_SHIFT)),
        )
        # The on-shift list must NOT contain this job, or arming becomes one-way.
        self.assertNotIn(
            "ops-mission-control/rotation-check",
            described["tier_crons"][rotation.TIER_ON_SHIFT],
        )

    def test_armed_crons_off_shift_still_contains_rotation_check(self):
        """Pins WHY `tier_crons` exists, so the trap cannot silently return.

        `armed_crons` is the union of every armed tier, so off shift it legitimately
        still lists `omc-rotation-check` (an `always` job). That is correct for "what
        is running now" and catastrophic as a pause list: pausing it strands the
        instance with no way to re-arm, silently ending incident response. If this
        assertion ever fails, re-read the SOP before changing it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        described = rotation.describe(ShiftStatus(on_shift=False))
        self.assertIn("ops-mission-control/rotation-check", described["armed_crons"])
        self.assertNotIn("ops-mission-control/dispatch", described["armed_crons"])


class _FakeJob:
    """Minimal duck-type of a CronJob for the arming tests."""

    def __init__(self, name, enabled):
        self.id = f"id-{name}"
        self.name = name
        self.enabled = enabled


class _FakeCronService:
    """Records every enable_job_async call so a test can assert what was moved."""

    def __init__(self, jobs):
        self._jobs = jobs
        self.calls = []

    async def list_jobs_async(self, include_disabled=False):
        # Mirrors the real freshness-guaranteed reader `apply_tiers` uses.
        return list(self._jobs)

    def raise_if_store_unreadable(self):
        # Third member of the duck-type `apply_tiers` requires, alongside
        # `list_jobs_async` and `enable_job_async`. A readable store is a no-op.
        return None

    async def enable_job_async(self, job_id, enabled=True):
        self.calls.append((job_id, enabled))
        for job in self._jobs:
            if job.id == job_id:
                job.enabled = enabled
        return True


class _RefusingCronService(_FakeCronService):
    """A store READABLE at list time that has gone unreadable before the write.

    It yields jobs and then refuses the write, and that combination is reachable
    rather than contrived -- but only for this ordering, which is why the
    docstring now names it. `_sync`'s ``except OSError`` arm latches
    ``_load_failed`` for exactly "a store that went unreadable AFTER a good load
    (EIO, EACCES, a botched restore)" (its own words), so ``_sync_for_write``
    refuses the mutation that ``list_jobs_async`` had already handed us jobs for.

    What this fake must NOT be read as is a store that was ALREADY unreadable
    when the arm began -- see `_UnreadableCronService`, where the real service
    yields NOTHING and so never reaches a write at all.
    """

    async def enable_job_async(self, job_id, enabled=True):
        raise CronStoreUnreadable("move the file aside")


class _UnreadableCronService(_FakeCronService):
    """A store ALREADY unreadable when the arm starts: no jobs, and writes refused.

    The empty list is the point. ``_load`` degrades an unreadable ``crons.json``
    to ``_jobs = []`` WITHOUT raising -- its ``except (OSError, ValueError,
    TypeError, RecursionError)`` arm warns, empties, latches ``_load_failed`` and
    returns -- and ``_synced_snapshot`` only translates ``CronStoreBusy``. So
    ``list_jobs_async`` hands back nothing, no job can diverge from the tier map,
    and ``enable_job_async`` -- the only member of this duck-type that raises --
    is never called. A guard keyed on a refused WRITE therefore cannot observe
    this state, which is the whole reason the probe exists.
    """

    def __init__(self):
        super().__init__([])

    def raise_if_store_unreadable(self):
        raise CronStoreUnreadable("move the file aside")


class TestArmingOnARefusingStore(unittest.IsolatedAsyncioTestCase):
    """`POST /rotation/arm` must report a refused store, never raise into aiohttp.

    `enable_job_async` refuses BEFORE mutating once the store cannot be read.
    Untranslated that escapes `_handle_rotation_arm` and becomes a 500, which is
    the one outcome this app forbids everywhere else -- a failure shown as a
    crash rather than as a failure. It reports `ok: False` rather than skipping
    the job quietly for the same reason: a partial arm reported as success is the
    quiet-versus-broken conflation the tier logic exists to prevent.
    """

    async def test_arm_reports_a_refused_store_instead_of_raising(self):
        names = [name for names in rotation.TIER_CRONS.values() for name in names]
        svc = _RefusingCronService([_FakeJob(name, False) for name in names])

        class _App(dict):
            pass

        app = _App(state=SimpleNamespace(crons=svc))

        class _Req:
            def __init__(self, application):
                self.app = application

        registry = mock.MagicMock()
        registry.resolve_shift = mock.AsyncMock(return_value=ShiftStatus(on_shift=True))
        with mock.patch.object(routes, "get_registry", return_value=registry):
            resp = await routes._handle_rotation_arm(cast(web.Request, _Req(app)))

        # Two narrowings, both asserting what this test genuinely relies on. The
        # handler is annotated `-> StreamResponse` (as its siblings are) and only
        # the `Response` subclass `json_response` returns carries `body`; that
        # `body` is in turn `bytes | bytearray | Payload | None`. Asserting keeps
        # the test honest -- a handler refactored to stream, or to send an empty
        # body, fails here rather than reading through a private attribute.
        assert isinstance(resp, web.Response)
        assert isinstance(resp.body, bytes)
        body = json.loads(resp.body)
        self.assertFalse(body["ok"], "a refused store must not be reported as a successful arm")
        self.assertEqual(body["code"], "cron_store_unreadable")


class TestArmingOnAnAlreadyUnreadableStore(unittest.IsolatedAsyncioTestCase):
    """`POST /rotation/arm` on a store that was unreadable BEFORE the arm began.

    The sibling `TestArmingOnARefusingStore` covers the other ordering (readable
    at list time, unreadable by the write) and cannot cover this one, because the
    two states differ in what `list_jobs_async` returns. Here `_load` has already
    degraded the store to an EMPTY job list without raising, so:

      * no job can diverge from the tier map,
      * `enable_job_async` -- the only raiser on this path -- is never called,
      * and the arm would otherwise return 200 `{"ok": True, "changed": []}`.

    That 200 is the quiet-versus-broken conflation this app forbids: a gated
    instance that believes it armed is indistinguishable from one that did, and
    the operator learns nothing about the corrupt `crons.json` underneath. So the
    unreadable state has to be detected independently of finding a write to make.
    """

    async def test_arm_refuses_when_the_store_was_already_unreadable(self):
        svc = _UnreadableCronService()
        self.assertEqual(
            await svc.list_jobs_async(True),
            [],
            "precondition: an already-unreadable store yields NO jobs, which is "
            "why a write-keyed guard cannot see it",
        )

        class _App(dict):
            pass

        app = _App(state=SimpleNamespace(crons=svc))

        class _Req:
            def __init__(self, application):
                self.app = application

        registry = mock.MagicMock()
        registry.resolve_shift = mock.AsyncMock(return_value=ShiftStatus(on_shift=True))
        with mock.patch.object(routes, "get_registry", return_value=registry):
            resp = await routes._handle_rotation_arm(cast(web.Request, _Req(app)))

        assert isinstance(resp, web.Response)
        assert isinstance(resp.body, bytes)
        body = json.loads(resp.body)
        self.assertEqual(
            resp.status,
            503,
            "an unreadable store must fail the arm, not return a successful no-op",
        )
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "cron_store_unreadable")
        self.assertEqual(body["changed"], [])
        self.assertEqual(svc.calls, [], "nothing may be written while the store cannot be read")

    async def test_the_real_cron_service_reaches_the_state_the_fake_models(self):
        """The fake above is only worth anything if the real service gets here.

        Asserted against the real `CronService` rather than a duck-type, because the
        defect this class covers was ORIGINALLY masked by a fake whose state the real
        service could not reach (it yielded jobs while refusing writes, which only
        happens when the store goes bad BETWEEN the read and the write). This pins the
        three facts the fix depends on: a corrupt store yields NO jobs, it does so
        WITHOUT raising, and the probe still refuses.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "crons.json"
            store.write_bytes(b"{not json at all")
            svc = CronService(base_dir=Path(tmp))

            jobs = await svc.list_jobs_async(True)
            self.assertEqual(jobs, [], "a corrupt store must load as an EMPTY list")
            with self.assertRaises(CronStoreUnreadable):
                svc.raise_if_store_unreadable()

            # NEGATIVE CONTROL: the probe must not fire on a store that is merely
            # empty. Without this the guard could be satisfied by refusing always,
            # which would break every honestly-empty install.
            store.write_text(json.dumps({"jobs": []}))
            healthy = CronService(base_dir=Path(tmp))
            await healthy.list_jobs_async(True)
            healthy.raise_if_store_unreadable()


class TestServerSideArming(unittest.IsolatedAsyncioTestCase):
    """`rotation.apply_tiers` — arming without the agent choosing what to pause.

    The property under test is the one the design review flagged: nothing may pause an
    always-tier cron, because that is the only job that can re-arm a gated instance and
    pausing it silently ends incident response until a human notices.
    """

    def _all_jobs(self, enabled=True):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        names = [name for names in rotation.TIER_CRONS.values() for name in names]
        return [_FakeJob(name, enabled) for name in names]

    async def test_off_shift_disarms_on_shift_but_never_the_always_tier(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        result = await rotation.apply_tiers(ShiftStatus(on_shift=False), svc)

        self.assertTrue(result["ok"])
        by_name = {job.name: job.enabled for job in jobs}
        # The gated tier is off...
        self.assertFalse(by_name["ops-mission-control/dispatch"])
        self.assertFalse(by_name["ops-mission-control/reconcile"])
        # ...and the job that re-arms the instance is STILL RUNNING. This is the
        # invariant: an off-shift instance that cannot re-arm is permanently dead.
        self.assertTrue(by_name["ops-mission-control/rotation-check"])
        paused = {name for name, enabled in by_name.items() if not enabled}
        self.assertNotIn("ops-mission-control/rotation-check", paused)

    async def test_on_shift_arms_the_gated_tier(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=False)
        svc = _FakeCronService(jobs)
        await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)

        by_name = {job.name: job.enabled for job in jobs}
        self.assertTrue(by_name["ops-mission-control/dispatch"])
        self.assertTrue(by_name["ops-mission-control/reconcile"])

    async def test_a_protected_cron_is_refused_even_if_the_tier_map_says_pause(self):
        """The guard is the invariant, not a consequence of how `tier_states` happens
        to be written today.

        `tier_states` hardcodes `always: True`, so this case is unreachable through the
        normal path — which is exactly why it is asserted directly. If a later edit makes
        the always tier conditional (the same class of bug as the `on_shift or unknown`
        regression already fixed in `tier_states`), this test is what fails.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        hostile = {
            rotation.TIER_ALWAYS: False,
            rotation.TIER_ON_SHIFT: False,
            rotation.TIER_PRIMARY: False,
        }
        with mock.patch.object(rotation, "tier_states", return_value=hostile):
            await rotation.apply_tiers(ShiftStatus(on_shift=False), svc)

        by_name = {job.name: job.enabled for job in jobs}
        self.assertTrue(
            by_name["ops-mission-control/rotation-check"],
            "apply_tiers paused an always-tier cron; the instance can no longer re-arm",
        )
        self.assertNotIn(("id-ops-mission-control/rotation-check", False), svc.calls)

    async def test_no_change_reports_an_empty_changed_list(self):
        """The cron is `silent: true` and runs every 5 minutes; a no-op must be
        distinguishable from work so the SOP can exit without output."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        # Settle first, then re-apply the identical shift.
        await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)
        svc.calls.clear()
        again = await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)

        self.assertEqual(again["changed"], [])
        self.assertEqual(svc.calls, [])

    async def test_jobs_this_app_does_not_own_are_left_alone(self):
        """Arming is scoped to the tier table. A user's unrelated cron that happens to
        be paused must not be resumed as a side effect of a shift starting."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        stranger = _FakeJob("my-personal-backup", False)
        svc = _FakeCronService(self._all_jobs(enabled=False) + [stranger])
        await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)

        self.assertFalse(stranger.enabled)
        self.assertNotIn(("id-my-personal-backup", True), svc.calls)

    async def test_missing_cron_service_is_reported_not_crashed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        result = await rotation.apply_tiers(ShiftStatus(on_shift=True), None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "cron_service_unavailable")

    async def test_the_primary_tier_cron_is_not_armed_from_here(self):
        """`ledger-hygiene` ships ENABLED and is gated in its ROUTE (409 `not_primary`),
        not by pausing.

        Widening arming to the primary tier while fixing the self-disarm hole would stop
        the nightly job on every non-primary instance — a behavior change, not a security
        fix. Worse, `is_primary()` shells out to `gh api user`, so a network blip answering
        False would silently skip a night's ledger maintenance. Scope is pinned here so a
        later "arm every tier for symmetry" edit has to argue with a test.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        # Not the primary owner, and off shift: the widest possible disarm.
        with mock.patch.object(rotation, "is_primary", return_value=False):
            await rotation.apply_tiers(ShiftStatus(on_shift=False), svc)

        by_name = {job.name: job.enabled for job in jobs}
        self.assertTrue(
            by_name["ops-mission-control/ledger-hygiene"],
            "arming paused the primary-tier cron; its gate is the route, not the scheduler",
        )

    async def test_arming_reads_a_fresh_job_list_not_the_cached_snapshot(self):
        """`apply_tiers` must use `list_jobs_async`, not `list_jobs`.

        Both are safe on the loop — `CronService.list_jobs` is explicitly cache-only — so
        this is about FRESHNESS, not blocking. The cached snapshot refreshes only once per
        timer poll, so a pause the operator just made from the CLI or the dashboard can
        still read as active here, and arming would helpfully "resume" a job they had
        deliberately stopped seconds earlier. Pinned because `list_jobs` is the obvious
        simplification and reads as equivalent.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        source = inspect.getsource(rotation.apply_tiers)
        self.assertIn("list_jobs_async", source)
        # No bare `.list_jobs(` call — `list_jobs_async(` contains `list_jobs` as a
        # substring, so match on the call punctuation.
        self.assertNotIn(".list_jobs(", source)
