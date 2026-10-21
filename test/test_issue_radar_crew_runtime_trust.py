"""A crew's auto-approve grant: minted, renewed, revoked and audited.

The wake's liveness guard is total, stopping a crew reaches the trust and loop state its
record cannot express, the watcher's gates and an app disable revoke the grant in the
request, renewal slides rather than re-activates, the backend never stamps a human's slot
trust, a slot with no scope key takes the shared approval path unchanged, the SEL reason
names which grant approved a tool, and each test's SEL root is private. The shared fakes and
autouse fixtures live in ``issue_radar_crew_runtime_helpers.py``.
"""

import ast
import contextlib
import inspect
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app,
    _app_is_on,
    _crew,
    _effectively_trusted,
    _FakeLoop,
    _FakeNudge,
    _FakeSlot,
    _FakeState,
    _isolated_crew_log,
    _private_sel_root_per_test,
    _runtime_modules,
)

import kiro_crew.sel as sel_mod
from kiro_crew import platform_compat
from kiro_crew.apps import teardown
from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import watch as watch_mod
from kiro_crew.dashboard import chat_runner
from kiro_crew.safety_override import reset_singleton, safety_override
from kiro_crew.testing.wait import async_wait_until


class TestTheWakesLivenessGuardIsTotal(unittest.TestCase):
    """The two shape rules that make the wake's liveness checks total.

    1. Every exit from the wake passes the same check, which is why it sits in a
       ``finally``: the grant is minted from a pre-``await`` record, so a path that
       returns without re-checking leaves a grant the stop path already revoked
       standing. A guard per ``return`` cannot cover the exception path, and each
       instance found in review was a ``return`` nobody had listed yet.
    2. Not one of those reads runs on the event loop. The record carries uncapped
       operator free text, so a synchronous read of it is an unbounded blocking call
       on the gateway's only thread — the ``no-blocking-call-on-event-loop`` hazard.
       Correctness does not need it: :func:`crew_runtime.wake_crew` documents why the
       read's own thread hop cannot hide a stop, since every stop path writes the
       record before it revokes.

    Pinned on the code's SHAPE because that is what regresses: an added ``return``,
    or a read quietly taken on the loop to make a check "tighter", cannot be observed
    behaviourally on an event loop nothing else is driving. Same source-inspection
    idiom as :class:`TestCrewStoreScoping`.
    """

    @staticmethod
    def _is_awaited_read(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Await)
            and isinstance(stmt.value.value, ast.Call)
            and getattr(stmt.value.value.func, "id", "") == "_current_crew"
        )

    def test_no_liveness_read_runs_on_the_event_loop(self):
        """Every ``_current_crew`` call site is awaited. The helper hops to a thread
        internally, so an un-awaited call would not even be a read — but the shape is
        what a future edit would break, and the AUTOSDE rule is blocking."""
        tree = ast.parse("\n".join(map(inspect.getsource, _runtime_modules())))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_current_crew"
        ]
        awaited = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "_current_crew"
        ]
        self.assertTrue(calls, "no _current_crew call sites at all")
        self.assertEqual(len(calls), len(awaited), "a _current_crew call site is not awaited")

    def test_the_exit_guard_reconciles_through_sync_trust(self):
        """Not through a hand-written condition. ``sync_trust`` mints on
        ``unattended AND is_live``; a guard that re-implements half of that is blind
        to an ``unattended`` downgrade, and a copy of the whole thing is a second
        definition free to drift. So the guard hands the record back to the minter."""
        final = self._wake_try().finalbody
        dumped = "".join(ast.dump(stmt) for stmt in final)
        self.assertIn("_reconcile_trust", dumped, "the exit guard does not reconcile the grant")
        self.assertIn("revoke_crew_execution", dumped, "the exit guard does not revoke")

    def test_only_sync_trust_ever_writes_the_trust_scope(self):
        """The scope attribute is the grant's carrier, so a second writer is a second
        policy. Pinned across the module: ``revoke_crew_execution`` clears it on the
        stop path, and nothing else may assign it."""
        tree = ast.parse("\n".join(map(inspect.getsource, _runtime_modules())))
        writers = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                targets = list(getattr(inner, "targets", []))
                if isinstance(inner, ast.AugAssign):
                    targets = [inner.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "_trust_scope":
                        writers.add(node.name)
        self.assertEqual(writers, {"sync_trust", "revoke_crew_execution", "revoke_crew_grants"})

    def test_every_async_grant_goes_through_the_generation_check(self):
        """A bare ``to_thread(sync_trust, ...)`` on an async path is the resurrection
        window this class exists to close. The one permitted bare call is inside
        ``_reconcile_trust`` itself; ``ensure_crew_session``'s is owned by another
        change and is pinned here so that ownership is explicit rather than silent."""
        tree = ast.parse("\n".join(map(inspect.getsource, _runtime_modules())))
        owners = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and "to_thread" in ast.dump(node.func)
                    and node.args
                    and getattr(node.args[0], "id", "") == "sync_trust"
                ):
                    owners.append(fn.name)
        # ``_reconcile_trust`` reaches ``sync_trust`` through ``_trust_inputs`` so
        # that the app gate is read in the same hop; it is not a direct owner.
        self.assertEqual(sorted(owners), ["ensure_crew_session"])

    def test_the_app_gate_is_read_in_the_hop_and_never_on_the_loop(self):
        """``installed.json`` is parsed off the loop with the mint (so a disabled app
        never mints); a disable landing inside the hop is the generation's job, not a
        second read's -- a loop-side read of that file is the
        ``no-blocking-call-on-event-loop`` hazard."""
        self.assertIn("is_app_enabled", inspect.getsource(cr._trust_inputs))
        body = ast.parse(inspect.getsource(cr._reconcile_trust)).body[0]
        on_loop = [
            node
            for node in ast.walk(body)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "is_app_enabled"
        ]
        self.assertEqual(on_loop, [], "_reconcile_trust reads the app gate on the loop")

    def test_the_disable_revoker_latches_and_only_the_watchdog_releases(self):
        """A second writer of ``_disabling = False`` is a second place a disable can
        be forgotten; pin the set."""
        tree = ast.parse("\n".join(map(inspect.getsource, _runtime_modules())))
        setters: dict[str, set[bool]] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_disabling" for t in node.targets
                ):
                    assert isinstance(node.value, ast.Constant)
                    setters.setdefault(fn.name, set()).add(bool(node.value.value))
        self.assertEqual(setters, {"revoke_crew_grants": {True}, "watchdog_cycle": {False}})

    def test_the_watchdog_keeps_the_slot_it_creates(self):
        """Both session-creating awaits assign back to ``slot``, so the trailing
        reconciliation reaches a session this pass created."""
        src = inspect.getsource(cr.watchdog_cycle)
        self.assertIn("slot = await launch_crew(", src)
        self.assertIn("slot = await ensure_crew_session(", src)

    def test_the_generation_is_keyed_by_crew(self):
        self.assertIsInstance(cr._revoke_generation, dict)

    def test_both_revokers_bump_the_generation(self):
        for fn in (cr.revoke_crew_execution, cr.revoke_crew_grants):
            self.assertIn(
                "_note_revocation",
                ast.dump(ast.parse(inspect.getsource(fn))),
                f"{fn.__name__} revokes without bumping the generation",
            )

    def test_the_read_helper_hops_off_the_loop(self):
        tree = ast.parse(inspect.getsource(cr._current_crew))
        self.assertTrue(inspect.iscoroutinefunction(cr._current_crew))
        self.assertIn("to_thread", ast.dump(tree), "the record read is not hoisted")

    def _wake_try(self) -> ast.Try:
        fn = ast.parse(inspect.getsource(cr.wake_crew)).body[0]
        assert isinstance(fn, ast.AsyncFunctionDef)
        tries = [st for st in fn.body if isinstance(st, ast.Try)]
        self.assertEqual(len(tries), 1, "wake_crew does not wrap its body in one try")
        return tries[0]

    def test_every_exit_from_the_wake_passes_the_liveness_check(self):
        """In a ``finally``, so a ``return`` added anywhere in the body — or an
        exception raised out of it — cannot bypass it."""
        final = self._wake_try().finalbody
        self.assertTrue(final, "wake_crew's try has no finally")
        self.assertTrue(
            any(self._is_awaited_read(st) for st in final),
            "wake_crew's finally does not re-read the record",
        )
        self.assertIn(
            "revoke_crew_execution",
            "".join(ast.dump(st) for st in final),
            "wake_crew's finally does not revoke a stopped crew's grants",
        )

    def test_the_wake_wrapper_holds_nothing_the_guard_could_miss(self):
        """The wrapper is the guard and nothing else: any work outside the ``try``
        would run un-guarded, which is the shape this class exists to forbid."""
        fn = ast.parse(inspect.getsource(cr.wake_crew)).body[0]
        assert isinstance(fn, ast.AsyncFunctionDef)
        outside = [
            st
            for st in fn.body
            if not isinstance(st, ast.Try)
            and not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))
        ]
        self.assertEqual(outside, [], "wake_crew does work outside its guarded try")
        self.assertTrue(
            any(isinstance(node, ast.Return) for node in ast.walk(self._wake_try())),
            "wake_crew's try never returns the body's result",
        )


# ── revoking execution (the two grants the record does not express) ─────────


class TestRevocation(unittest.IsolatedAsyncioTestCase):
    """Stopping a crew has to reach state the crew RECORD cannot express.

    ``enabled``, ``paused_reason`` and ``retired_at`` are all on disk; the two
    things that actually give a crew a turn are not. Its autonudge loop is a live
    timer owned by another service, and its auto-approve grant is an in-memory
    ``SafetyOverride`` scope that makes its tool calls auto-approve. Anything that
    writes only the record leaves both armed until the watchdog notices — and that
    runs on the app's poll interval, which is long enough for an idle timer to fire
    one more unattended turn on a crew a human just stopped.

    One helper serves the routes and the watchdog, so these tests pin the helper and
    the watchdog's use of it; the routes' own timing is pinned in the route tests.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Use platform_compat.rmtree_force instead of TemporaryDirectory.cleanup
        # so that read-only files written by the crew store (which the Windows
        # CRT cannot unlink without first clearing the read-only attribute) do
        # not leave the directory behind and trip the tmp-residue guard.  Also
        # unregister the process-global slot-close hook that watchdog_cycle
        # installs with root=self.root, so no background callback can open
        # files in the directory while it is being removed.
        self.addCleanup(platform_compat.rmtree_force, self._tmp.name)
        self.addCleanup(teardown.unregister_slot_close_hook, cr.APP_NAME)
        self.root = Path(self._tmp.name)
        reset_singleton()
        self.addCleanup(reset_singleton)

    async def _armed(self, **spec) -> tuple[dict[str, Any], _FakeState, _FakeSlot, _FakeNudge]:
        """A crew that is genuinely running: trusted slot, active loop."""
        crew = _crew(self.root, unattended=True, **spec)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        # the grant this revocation has to remove
        self.assertTrue(_effectively_trusted(slot))
        return crew, state, slot, _FakeNudge([_FakeLoop("nl_0", slot.key, active=True)])

    async def test_it_clears_trust_and_deactivates_the_loop(self):
        crew, state, slot, svc = await self._armed()
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            self.assertTrue(await cr.revoke_crew_execution(state, crew, "paused"))
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))
        self.assertFalse(svc.get_by_slot(slot.key).active)

    async def test_it_also_clears_an_interactive_grant_a_human_left_behind(self):
        """Stopping a crew means stopped. ``sync_trust`` runs unprompted every cycle
        and so leaves a human's session trust alone, but this runs only because
        someone decided the crew must not run — including its slot."""
        crew, state, slot, svc = await self._armed()
        slot._trust = True  # as the approval card's "Trust all tools" leaves it
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.revoke_crew_execution(state, crew, "retired")
        self.assertFalse(slot._trust)
        self.assertFalse(_effectively_trusted(slot))

    async def test_revoking_twice_is_a_no_op_rather_than_an_error(self):
        """Retiring an already-paused crew, or a route racing the watchdog."""
        crew, state, slot, svc = await self._armed()
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.revoke_crew_execution(state, crew, "paused")
            self.assertFalse(await cr.revoke_crew_execution(state, crew, "retired"))
        self.assertEqual(svc.updates, [("nl_0", {"active": False})])

    async def test_a_failing_loop_service_still_clears_trust(self):
        """Best-effort, and the halves are independent: the grant that lets a turn
        run auto-approved must go even when the timer service cannot be reached."""
        crew, state, slot, svc = await self._armed()
        svc.update = mock.AsyncMock(side_effect=RuntimeError("registry busy"))  # type: ignore[method-assign]
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.revoke_crew_execution(state, crew, "retired")
        self.assertFalse(_effectively_trusted(slot))

    async def test_a_crew_with_no_resident_slot_is_still_un_armed(self):
        """After a restart the loop is persisted and the slot is not. Revocation
        must still reach the timer, or the crew gets a turn with no session."""
        crew = _crew(self.root, unattended=True)
        svc = _FakeNudge([_FakeLoop("nl_0", f"crew-{crew['id']}", active=True)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            self.assertTrue(await cr.revoke_crew_execution(_FakeState(), crew, "paused"))
        self.assertFalse(svc.loops[0].active)

    async def test_the_watchdog_remains_the_backstop(self):
        """A crew stopped by editing the record directly never went through a
        route, so the sweep has to keep revoking on its own."""
        crew, state, slot, svc = await self._armed()
        paused = cs.update_crew(
            OWNER, REPO, crew["id"], {"paused_reason": "operator paused"}, self.root
        )
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [paused], self.root)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(svc.get_by_slot(slot.key).active)


class TestWatchGating(unittest.IsolatedAsyncioTestCase):
    """The two gates in ``watch.py`` and the difference between them."""

    def _watch(self):
        from kiro_crew.apps.builtins.issue_radar.backend import watch

        watch._crews_suspended = False
        return watch

    async def test_sweep_does_not_inherit_the_notify_preference(self):
        watch = self._watch()
        entries = [{"owner": OWNER, "repo": REPO, "provider": "github", "host": "github.com"}]
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=True))
            use(mock.patch.object(watch.store, "list_connected_repos", return_value=entries))
            use(
                mock.patch.object(
                    watch.store,
                    "read_repo_settings",
                    return_value={"notify_on_new_issue": False},
                )
            )
            poll = use(mock.patch.object(watch, "_poll_repo", new=mock.AsyncMock()))
            sweep = use(
                mock.patch.object(
                    watch.crew_runtime, "sweep_repo", new=mock.AsyncMock(return_value={})
                )
            )
            await watch._poll_once(_app(_FakeState()))
        # Muting the bell must not stop a crew reconciling its own pull requests.
        poll.assert_not_awaited()
        sweep.assert_awaited_once()

    async def test_a_failing_new_issue_poll_still_runs_the_sweep(self):
        watch = self._watch()
        entries = [{"owner": OWNER, "repo": REPO}]
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=True))
            use(mock.patch.object(watch.store, "list_connected_repos", return_value=entries))
            use(
                mock.patch.object(
                    watch.store,
                    "read_repo_settings",
                    return_value={"notify_on_new_issue": True},
                )
            )
            use(
                mock.patch.object(
                    watch, "_poll_repo", new=mock.AsyncMock(side_effect=RuntimeError("gh down"))
                )
            )
            sweep = use(
                mock.patch.object(
                    watch.crew_runtime, "sweep_repo", new=mock.AsyncMock(return_value={})
                )
            )
            await watch._poll_once(_app(_FakeState()))
        sweep.assert_awaited_once()

    async def test_disabling_the_app_suspends_the_crews(self):
        watch = self._watch()
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=False))
            suspend = use(
                mock.patch.object(
                    watch.crew_runtime, "suspend_crews", new=mock.AsyncMock(return_value=0)
                )
            )
            repos = use(mock.patch.object(watch.store, "list_connected_repos"))
            await watch._poll_once(_app(_FakeState()))
            await watch._poll_once(_app(_FakeState()))
        # A disabled app stays silent — no config walk — and suspends only ONCE,
        # because nothing re-establishes trust while the app is off.
        repos.assert_not_called()
        suspend.assert_awaited_once()

    async def test_suspension_clears_trust_on_resident_crew_slots(self):
        reset_singleton()
        self.addCleanup(reset_singleton)
        state = _FakeState()
        slot = _FakeSlot("crew-c_abc")
        slot._app = "issue-radar"
        cr.sync_trust(slot, {"id": "c_abc", "unattended": True, "enabled": True})
        self.assertTrue(_effectively_trusted(slot))
        other = _FakeSlot("chat-1")
        other._app = ""
        other._trust = True
        state._slots = {"crew-c_abc": slot, "chat-1": other}
        cleared = await cr.suspend_crews(state)
        self.assertEqual(cleared, 1)
        self.assertFalse(_effectively_trusted(slot))
        # Revoked at the source too, so re-attaching a slot cannot inherit it.
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope("c_abc")))
        self.assertTrue(other._trust)  # a user's own session is not ours to touch


class TestDisablingTheAppRevokesInline(unittest.IsolatedAsyncioTestCase):
    """Disabling the app must stop the crews IN THE REQUEST, not on the next sweep.

    The window this pins: the sweep runs every ``POLL_INTERVAL_SEC``, so a
    suspension that only happened there left up to a full minute in which an
    already-armed nudge fired a whole auto-approved turn for an app the operator had
    just switched off. "Stop" has to mean stopped, the way pause and retire already
    revoke inline before they answer.

    Nothing here sleeps or polls: every assertion is made on the state left behind
    the moment the disable hook's coroutine returns.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(platform_compat.rmtree_force, self._tmp.name)
        self.root = Path(self._tmp.name)
        reset_singleton()
        self.addCleanup(reset_singleton)
        # ``watchdog_cycle`` re-registers the dismissal hook, which is process-wide
        # state; drop it again so these tests cannot change another's outcome.
        self.addCleanup(teardown.unregister_slot_close_hook, cr.APP_NAME)
        self.state = _FakeState()
        # A PERSISTED crew, because the cycle re-reads the record before it grants:
        # a crew the store never wrote is a state its only caller cannot produce, so
        # a hand-built dict would test a shape the product never reaches.
        self.crew = _crew(self.root, unattended=True)
        self.slot = _FakeSlot(f"crew-{self.crew['id']}")
        self.slot._app = cr.APP_NAME
        cr.sync_trust(self.slot, self.crew)
        self.assertTrue(_effectively_trusted(self.slot), "fixture never got its grant")
        # Both registries: ``get_slot`` reads the public one, the suspension walks
        # the private one it can enumerate.
        self.state.slots[self.slot.key] = self.slot
        self.state._slots = {self.slot.key: self.slot}
        self.nudge = _FakeNudge([_FakeLoop("nl_dis", self.slot.key)])

    def _register(self) -> Any:
        """Install the hook against a stand-in for core's registry, and return it.

        ``create=True``: the registry is core's to add (see the report accompanying
        this change), and the app must keep working — on the sweep alone — against a
        build that has none. Patching it in is what lets this test assert the app
        registers the right callable under its own name, so the seam is live the
        moment core calls it.
        """
        from kiro_crew.apps import teardown

        registry: dict[str, Any] = {}
        patch = mock.patch.object(
            teardown,
            "register_app_disable_hook",
            create=True,
            side_effect=lambda app, hook: registry.__setitem__(app, hook),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(cr.install_app_disable_hook(self.state))
        self.assertIn(cr.APP_NAME, registry)
        return registry[cr.APP_NAME]

    async def test_the_registered_hook_revokes_with_no_poll_at_all(self):
        hook = self._register()
        with (
            mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge),
            mock.patch.object(watch_mod, "_poll_once", new=mock.AsyncMock()) as poll,
        ):
            # Core passes the disabled app's name; the registration is already
            # per-app, so the argument is accepted and ignored.
            cleared = await hook(cr.APP_NAME)
        self.assertEqual(cleared, 1)
        self.assertFalse(_effectively_trusted(self.slot))
        # Revoked at the source too, so re-attaching a slot cannot inherit it.
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope("c_dis")))
        # And the crew's clock is stopped, so no later turn is even scheduled.
        self.assertEqual(self.nudge.updates, [("nl_dis", {"active": False})])
        poll.assert_not_awaited()

    async def test_the_grant_is_gone_before_the_first_await(self):
        """GRANTS BEFORE LOOPS is the ordering that closes the window.

        Deactivating a loop awaits, so it hands the event loop back. Doing it first
        would leave the grants live across that suspension point, and an armed timer
        firing in the gap would take its auto-approved turn from the very call that
        was stopping it. Asserted from inside the await itself, because that is the
        only moment at which a reversed order is observable.
        """
        observed: list[bool] = []

        async def _update(loop_id: str, **kw: Any) -> None:
            observed.append(_effectively_trusted(self.slot))
            self.nudge.updates.append((loop_id, dict(kw)))

        with (
            mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge),
            mock.patch.object(self.nudge, "update", new=_update),
        ):
            await cr.on_app_disabled(self.state)
        self.assertEqual(observed, [False], "the loop was stopped before the grant")

    async def test_an_armed_nudge_that_fires_after_disable_is_not_trusted(self):
        """The turn a nudge fires is run by core, which asks the slot at fire time.

        So the assertion that matters is not "did we call something" but what the
        shared approval path answers for this slot AFTER the disable — and it must
        answer the same for the human ``_trust`` flag, which a crew session can also
        be carrying when someone clicked it.
        """
        self.slot._trust = True
        hook = self._register()
        with mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge):
            await hook(cr.APP_NAME)
        # Simulate the fire landing anyway (a timer already inside its sleep, or a
        # gateway with no hook registry at all): core resolves trust here.
        self.assertFalse(_effectively_trusted(self.slot))
        self.assertFalse(self.slot._trust)
        self.assertEqual(self.slot._trust_scope, "")

    async def test_the_sweep_still_suspends_a_flag_flipped_behind_our_back(self):
        """The BACKSTOP. ``kirocrew app disable`` runs in another process and an
        ``installed.json`` can be edited on disk, so neither can reach an in-process
        hook. The sweep is the only thing that catches those, and it must keep
        working with no hook registered at all."""
        watch_mod._crews_suspended = False
        self.addCleanup(setattr, watch_mod, "_crews_suspended", False)
        with (
            mock.patch.object(watch_mod, "is_app_enabled", return_value=False),
            mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge),
            mock.patch.object(watch_mod.store, "list_connected_repos") as repos,
        ):
            await watch_mod._poll_once(_app(self.state))
        self.assertFalse(_effectively_trusted(self.slot))
        self.assertEqual(self.nudge.updates, [("nl_dis", {"active": False})])
        repos.assert_not_called()  # a disabled app reads no config

    async def test_enabling_again_restores_trust_and_the_loop(self):
        """Reversible: the revocation is in-memory and the record is untouched, so
        the next watchdog cycle for a still-live crew brings both back."""
        hook = self._register()
        with mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge):
            await hook(cr.APP_NAME)
        self.assertFalse(_effectively_trusted(self.slot))

        # Re-enabled: the gate reads on again (the autouse pin), so the cycle regrants.
        with mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge):
            await cr.watchdog_cycle(self.state, OWNER, REPO, [self.crew], self.root)
        self.assertTrue(_effectively_trusted(self.slot))
        self.assertIn(("nl_dis", {"active": True}), self.nudge.updates)

    async def test_a_look_alike_chat_tab_keeps_its_own_loop(self):
        """Disabling this app must not touch a loop it does not own.

        The slot-key prefix is not a namespace this app owns: a person can name an
        ordinary chat tab ``crew-notes`` and arm their own monitoring loop on it.
        Matching on the prefix alone deactivated that loop and PERSISTED it
        inactive, so someone else's monitoring silently stopped because an
        unrelated app was switched off — and nothing in the tab explains why.

        The crew's own loop must still be deactivated in the same pass, or this
        test would also pass on a build that had simply stopped suspending crews.
        """
        mine = _FakeLoop("nl_mine", self.slot.key)
        # Valid prefix, suffix that the store's id grammar rejects — which is
        # exactly what a hand-named tab looks like.
        theirs = _FakeLoop("nl_theirs", "crew-notes")
        nudge = _FakeNudge([mine, theirs])

        hook = self._register()
        with mock.patch.object(cr, "_autonudge_instance", return_value=nudge):
            await hook(cr.APP_NAME)

        self.assertIn(("nl_mine", {"active": False}), nudge.updates, "the crew's loop survived")
        self.assertNotIn(
            "nl_theirs",
            [lid for lid, _ in nudge.updates],
            "an unrelated chat tab's monitoring loop was deactivated",
        )

    def test_a_gateway_without_the_registry_says_so_rather_than_pretending(self):
        """A security control that silently did not install is worse than one that
        is absent: the sweep is still the backstop, but the operator must be able to
        learn that "stop" means "within a minute" on this build.

        The absence is SIMULATED rather than read off the real module, so this keeps
        testing the degradation path after core lands the registry.
        """
        from kiro_crew.apps import teardown

        with mock.patch.object(teardown, "register_app_disable_hook", None, create=True):
            self.assertFalse(cr.install_app_disable_hook(self.state))


class TestGrantRenewal(unittest.TestCase):
    """Renewal is a SLIDE, not a re-activation, and that is a security property.

    The watchdog calls ``sync_trust`` every 60s. Re-activating on each of those
    would write a critical SEL entry per cycle — 1,440 a day per crew, burying the
    activation an auditor came for — and would reset ``activated_at``, so
    ``SafetyOverride``'s 24h ceiling could never be reached: the grant would be
    perpetual with an audit trail that merely looked busy.
    """

    def setUp(self):
        reset_singleton()
        self.addCleanup(reset_singleton)
        self.crew = {"id": "c_r", "unattended": True, "enabled": True}
        self.slot = _FakeSlot("crew-c_r")

    def test_repeated_cycles_activate_once_and_slide_thereafter(self):
        so = safety_override()
        with (
            mock.patch.object(type(so), "activate_scoped", wraps=so.activate_scoped) as activate,
            mock.patch.object(type(so), "renew_scoped", wraps=so.renew_scoped) as renew,
        ):
            for _ in range(5):
                self.assertTrue(cr.sync_trust(self.slot, self.crew))
        self.assertEqual(activate.call_count, 1, "re-minted the grant on a live scope")
        self.assertEqual(renew.call_count, 4)

    def test_a_slide_carries_the_crew_ttl_and_not_the_six_hour_default(self):
        """``renew_scoped``'s default TTL is the 6h ad-hoc one. Letting it default
        would silently widen the grant to 6h on the very first watchdog cycle."""
        cr.sync_trust(self.slot, self.crew)
        so = safety_override()
        with mock.patch.object(type(so), "renew_scoped", wraps=so.renew_scoped) as renew:
            cr.sync_trust(self.slot, self.crew)
        self.assertEqual(renew.call_args.kwargs.get("ttl"), cr.TRUST_TTL_SECS)
        self.assertLessEqual(
            so.scope_remaining_secs(cr.autoapprove_scope("c_r")), cr.TRUST_TTL_SECS
        )

    def test_a_grant_at_its_ceiling_is_reminted_rather_than_left_to_lapse(self):
        """At the 24h ceiling the slide is refused. Minting a fresh grant re-audits
        the decision AND keeps a mid-turn crew from losing trust in the gap."""
        cr.sync_trust(self.slot, self.crew)
        so = safety_override()
        refused = type(so).renew_scoped(so, "nope", source="x")  # renewed=False shape
        self.assertFalse(refused.renewed)
        with (
            mock.patch.object(type(so), "renew_scoped", return_value=refused),
            mock.patch.object(type(so), "activate_scoped", wraps=so.activate_scoped) as activate,
        ):
            self.assertTrue(cr.sync_trust(self.slot, self.crew))
        activate.assert_called_once()


class TestNoBackendTrustGrant(unittest.TestCase):
    """The load-bearing invariant, mirroring ``spec_builder``'s own source check.

    ``slot._trust`` is the grant a HUMAN makes. It never expires and nothing audits
    its activation, because the click is the record. A backend that stamps it
    manufactures an unbounded auto-approval out of nothing, which is why
    ``spec_builder`` was made to stop — and asserting on module SOURCE is what stops
    it coming back, since a behavioural test only covers the paths it happens to
    drive.

    Revoking is not granting: ``= False`` writes are fine and are what
    ``revoke_crew_execution`` and ``suspend_crews`` are for.
    """

    def test_crew_runtime_never_grants_slot_trust(self):
        src = "\n".join(map(inspect.getsource, _runtime_modules()))
        assert "slot._trust = True" not in src
        # And no revive-by-another-name: not via a variable, not via setattr, and
        # not via the ``slot._trust = want`` form this replaced — which is the one
        # that actually shipped, and which no ``= True`` search would have caught.
        for revived in (
            "_trust = True",
            "_trust = want",
            "_trust = bool",
            "_trust = grant",
            'setattr(slot, "_trust"',
            "setattr(slot, '_trust'",
        ):
            assert revived not in src, f"{revived} is a backend trust grant"
        # Every write to the flag in this module must be a revocation.
        writes = re.findall(r"\._trust\s*=\s*([^\n]+)", src)
        self.assertTrue(writes, "expected the revocation writes to still exist")
        for value in writes:
            self.assertEqual(value.strip(), "False", f"non-revoking write to _trust: {value!r}")

    def test_the_grant_is_ttl_bounded_and_below_the_ceiling(self):
        """A scope with no TTL, or one at the 24h ceiling, would be the same
        unbounded grant wearing a scope key."""
        self.assertGreater(cr.TRUST_TTL_SECS, 0)
        self.assertLess(cr.TRUST_TTL_SECS, 86400)


class TestSharedApprovalPathIsUnchangedWithoutAScope(unittest.TestCase):
    """The scope check is STRICTLY ADDITIVE.

    Every ordinary chat session reaches the same code, so a slot that carries no
    scope key must take exactly the decision it took before this existed — in both
    directions. A change here is a change to approval semantics for every session.
    """

    class _Bare:
        """A slot from before ``_trust_scope`` existed: the attribute is absent."""

        def __init__(self, trust: bool) -> None:
            self._trust = trust

    def test_an_untrusted_slot_with_no_scope_key_is_untrusted(self):
        self.assertFalse(_effectively_trusted(self._Bare(False)))

    def test_a_trusted_slot_with_no_scope_key_is_trusted(self):
        self.assertTrue(_effectively_trusted(self._Bare(True)))

    def test_an_empty_scope_key_is_not_a_grant(self):
        slot = _FakeSlot("chat-1")
        self.assertEqual(slot._trust_scope, "")
        self.assertFalse(_effectively_trusted(slot))

    def test_no_scope_key_means_the_override_is_never_consulted(self):
        """Not just the same ANSWER — the same work. A live grant under some other
        key must not be reachable by a slot that names no scope."""
        with mock.patch.object(
            type(safety_override()), "is_scope_active", return_value=True
        ) as probe:
            self.assertFalse(_effectively_trusted(self._Bare(False)))
        probe.assert_not_called()

    def test_the_interactive_flag_still_short_circuits(self):
        """A human's grant needs no scope and must not depend on one being live."""
        slot = _FakeSlot("chat-1")
        slot._trust = True
        slot._trust_scope = "crew:c_x:autoapprove"  # not active
        self.assertTrue(_effectively_trusted(slot))


class TestAutoApproveProvenance(unittest.TestCase):
    """The SEL reason has to name WHICH grant approved a tool, or the audit trail
    cannot distinguish a human's session trust from a worker's expiring grant."""

    def test_yolo_outranks_everything(self):
        slot = _FakeSlot("chat-1")
        slot._trust_scope = "crew:c_x:autoapprove"
        self.assertEqual(chat_runner._auto_approve_reason(slot, True), "yolo")

    def test_a_scoped_grant_is_named_as_such(self):
        slot = _FakeSlot("crew-c_x")
        slot._trust_scope = "crew:c_x:autoapprove"
        self.assertEqual(chat_runner._auto_approve_reason(slot, False), "trust_scope")

    def test_session_trust_is_still_reported_as_trust(self):
        slot = _FakeSlot("chat-1")
        slot._trust = True
        self.assertEqual(chat_runner._auto_approve_reason(slot, False), "trust")


#: SEL roots already claimed by a test in this module, on this worker. Uniqueness
#: across workers needs no bookkeeping: the roots are ``tmp_path_factory`` dirs
#: under per-worker basetemps, so two workers cannot mint the same path. The
#: REUSE assertion below therefore only bites when both claim tests land on one
#: worker (``--dist load`` may split them); the not-the-shared-default leg holds
#: per test on every worker regardless.
_CLAIMED_SEL_ROOTS: set[str] = set()


class TestSelRootIsolation(unittest.IsolatedAsyncioTestCase):
    """The per-test SEL root, pinned differentially.

    Every trust assertion in this file requires a fail-closed critical SEL
    audit to WIN the chain lock, and on the event-loop thread that acquire is a
    single non-blocking attempt (``sel_chain_lock.py``) with a fail-closed refusal behind
    it (``safety_override_grant.py``) — both correct product behaviour, pinned by
    their own tests, and deliberately not touched here. What THESE tests pin is
    the test-isolation property that makes depending on that lock safe: the
    root the audit writes through belongs to this test alone, so no sibling
    test's writer can ever hold this test's lock.

    Written to FAIL on the shared-root arrangement this module had before
    (one session-scoped SEL directory per worker), not merely to pass on the
    private one — see each test's body for which leg is the differential.
    """

    def setUp(self):
        if getattr(sel_mod._default_dir, "__module__", "") == "kiro_crew.sel":
            # The rootdir conftest displaces ``_default_dir`` with a
            # session-scoped closure; the ORIGINAL still installed means that
            # isolation never ran (raw ``python -m unittest``, or pytest from
            # a foreign rootdir). In that world ``_default_dir()`` — and the
            # ``sel()`` the tests below touch — would resolve, CREATE, and
            # initialize against the operator's REAL data home before any
            # assertion could fail. Probe the seam by identity: even calling
            # ``config_dir()`` to compare paths would itself create the home
            # on first use, so the check must not invoke anything.
            self.skipTest("requires the rootdir conftest SEL isolation")
        reset_singleton()
        self.addCleanup(reset_singleton)

    async def test_a_holder_of_the_shared_default_root_cannot_refuse_trust(self):
        """A concurrent writer on the SHARED root does not reach this module.

        The holder below stands in for the writer the flake needed: it takes
        the chain lock of the DEFAULT SEL root — the directory ``sel()`` would
        resolve WITHOUT this module's per-test isolation, and the one a sibling
        test's writer would actually hold — through its own file description,
        which is how a foreign holder looks to ``flock``. On the shared-root
        arrangement the fail-closed trust audit loses its single-shot acquire
        against exactly this and the grant is refused (the original failure
        verbatim); with a private per-test root the holder is a stranger to the
        audit, and trust must be granted.
        """
        # setUp already skipped when the isolation seam is absent, so this is
        # the displaced session directory, never the operator's data home.
        shared = sel_mod._default_dir()
        lock_dir = shared / sel_mod._TRUST_SUBDIR
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_file = lock_dir / sel_mod._SEL_LOCK_FILE
        # Same flags the code under test opens the sidecar with (sel_chain_lock.py), so
        # the staged holder is byte-faithful on Windows too.
        fd = os.open(
            lock_file,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")  # Windows locks a byte RANGE; give it a byte
            # The displaced session singleton's writer may itself be flushing
            # to this root right now (its holds span one append batch), so a
            # single-shot acquire here would inherit the very flake this
            # commit retires. Retry non-blocking attempts — never a blocking
            # acquire on the event-loop thread — until the transient hold
            # clears; the budget is generous because a miss fails the test.
            try:
                await async_wait_until(
                    lambda: platform_compat.try_acquire_lock(fd, exclusive=True),
                    describe=lambda: "the writer's transient hold never cleared",
                )
            except AssertionError:
                self.fail("could not stage the foreign holder on the shared root")
            try:
                slot = _FakeSlot("crew-c_7029iso")
                slot._app = cr.APP_NAME
                granted = cr.sync_trust(
                    slot, {"id": "c_7029iso", "unattended": True, "enabled": True}
                )
                self.assertTrue(
                    granted,
                    "trust was refused: the audit contended for the SHARED root's "
                    "chain lock, so this test's SEL root is not private (#7029)",
                )
                self.assertTrue(_effectively_trusted(slot))
            finally:
                platform_compat.release_lock(fd)
        finally:
            os.close(fd)
        # The audit went somewhere real: fail-closed was not merely bypassed.
        body = sel_mod.sel()._path.read_text(encoding="utf-8")
        self.assertIn("safety_override:activate_scoped", body)

    async def test_this_tests_sel_root_is_private_first_claim(self):
        self._claim_root()

    async def test_this_tests_sel_root_is_private_second_claim(self):
        """Second claimant: on the shared-root arrangement both tests resolve the
        one session directory, so whichever of the pair runs second trips the
        reuse assertion (and both trip the shared-default one)."""
        self._claim_root()

    def _claim_root(self) -> None:
        root = str(sel_mod.sel()._dir)
        self.assertNotEqual(
            Path(root),
            sel_mod._default_dir(),
            "sel() resolved the SHARED default root: tests on this worker would "
            "contend for one chain lock (#7029)",
        )
        self.assertNotIn(root, _CLAIMED_SEL_ROOTS, "SEL root reused across tests (#7029)")
        _CLAIMED_SEL_ROOTS.add(root)
