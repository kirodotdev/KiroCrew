"""Tests for the HTTP surface.

Two properties matter most here.

**The enabled gate.** Builtin routes are registered at gateway startup and exist
even while the app is disabled, so every handler must refuse when disabled. A
missing gate on a default-disabled opt-in app means it is silently callable.

**Secrets are write-only.** No read endpoint may ever return a stored token, even
to an authenticated caller. The test asserts against the real handler rather than
inspecting the code, so a future refactor that starts echoing config wholesale is
caught.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import models, routes, store


class TestRouteRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_all_routes_are_namespaced_under_the_app(self):
        """A builtin registering outside its own namespace would shadow core APIs."""
        app = web.Application()
        routes.register_routes(app)
        paths = [
            resource.canonical
            for resource in app.router.resources()
            if getattr(resource, "canonical", "")
        ]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(
                path.startswith("/api/apps/ops-mission-control"),
                f"route escapes the app namespace: {path}",
            )

    async def test_expected_surface_is_present(self):
        app = web.Application()
        routes.register_routes(app)
        paths = {
            resource.canonical
            for resource in app.router.resources()
            if getattr(resource, "canonical", "")
        }
        base = "/api/apps/ops-mission-control"
        for suffix in (
            "/state",
            "/incidents",
            "/incident",
            "/incident/transition",
            "/incident/claim",
            "/incident/action",
            "/signals",
            "/providers",
            "/rotation",
            # Server-side tier arming. The agent POSTs here instead of holding
            # `cron_pause`, so if this route goes missing the rotation-check cron has no
            # way to arm anything and the on-shift tier never fires.
            "/rotation/arm",
            "/ledger",
            "/webhook",
        ):
            self.assertIn(base + suffix, paths)

    async def test_register_routes_warms_the_registry_off_the_request_path(self):
        """`get_registry()` populates lazily — entry-point enumeration, signed-plugin
        admission I/O and companion import all on the first call. Every producer of that
        first call is a request handler, so the discovery cost landed on the event loop.
        `register_routes` runs synchronously at gateway startup, so warming it there pays
        the cost before any request is served. Found in review."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        self.assertIsNone(registry._registry)
        app = web.Application()
        routes.register_routes(app)
        self.assertIsNotNone(
            registry._registry,
            "register_routes did not warm the registry; the first request pays discovery",
        )

    async def test_a_registry_warmup_fault_does_not_break_startup(self):
        """This app is default-disabled; an install that never enables it must not crash
        gateway startup on a discovery fault."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        with mock.patch.object(routes, "get_registry", side_effect=RuntimeError("discovery boom")):
            app = web.Application()
            routes.register_routes(app)  # must not raise


class TestEnabledGate(unittest.IsolatedAsyncioTestCase):
    """Every handler must refuse while the app is disabled."""

    @staticmethod
    async def _invoke(handler: routes.Handler, *, enabled: bool) -> web.StreamResponse:
        """Drive a gated handler with the app enabled or disabled."""
        request = mock.MagicMock(spec=web.Request)
        with mock.patch.object(routes, "is_app_enabled", return_value=enabled):
            wrapped = routes._require_enabled(handler)
            return await wrapped(request)

    async def test_disabled_app_returns_403(self):
        async def _never_called(_request: web.Request) -> web.StreamResponse:
            raise AssertionError("handler ran while the app was disabled")

        response = await self._invoke(_never_called, enabled=False)
        self.assertEqual(response.status, 403)

    async def test_enabled_app_reaches_the_handler(self):
        async def _ok(_request: web.Request) -> web.StreamResponse:
            return web.json_response({"ok": True})

        response = await self._invoke(_ok, enabled=True)
        self.assertEqual(response.status, 200)

    async def test_every_registered_handler_is_gated(self):
        """Catches a new route added without the gate."""
        app = web.Application()
        routes.register_routes(app)
        ungated = []
        for resource in app.router.resources():
            for route in resource:
                handler = route.handler
                # ``_require_enabled`` uses functools.wraps, so a gated handler
                # carries __wrapped__ pointing at the real implementation.
                if not hasattr(handler, "__wrapped__"):
                    ungated.append(getattr(resource, "canonical", str(resource)))
        self.assertEqual(ungated, [], f"ungated routes: {ungated}")


class TestSecretsAreWriteOnly(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_provider_listing_never_contains_a_token(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry, secrets

        secret_value = "u+ThisIsTheActualSecretValue"
        secrets.put_secret("pagerduty", "api_token", secret_value)
        registry.reset_registry()
        try:
            listing = [routes._provider_dict(p) for p in registry.get_registry().catalog()]
            # ``ensure_ascii=False`` so the placeholder's bullets are not escaped
            # to • — we are asserting on content, not on JSON encoding.
            payload = json.dumps(listing, ensure_ascii=False)
            self.assertNotIn(secret_value, payload)
            self.assertNotIn("ThisIsTheActualSecretValue", payload)

            # ...but the UI must still learn that the field IS set.
            pagerduty = next(p for p in listing if p["id"] == "pagerduty")
            self.assertEqual(pagerduty["secrets"]["api_token"], secrets.REDACTED_PLACEHOLDER)

            # An unset field reports empty, so the UI can distinguish the two.
            datadog = next(p for p in listing if p["id"] == "datadog")
            self.assertEqual(datadog["secrets"]["api_key"], "")
        finally:
            registry.reset_registry()


class TestIncidentsPayloadIsBounded(unittest.IsolatedAsyncioTestCase):
    """`/incidents` must not serialize the ENTIRE index on every dashboard poll.

    Fine at three incidents. Once a flapping alarm has minted hundreds — which a
    re-claimable resolved alarm allows — it is an ever-growing payload on a polled
    endpoint.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _get_incidents(self):
        request = mock.MagicMock(spec=web.Request)
        request.query = {}
        response = await routes._handle_incidents(request)
        return json.loads(getattr(response, "text", "{}") or "{}")

    async def test_response_is_capped_and_says_when_it_truncated(self):
        """Silent truncation is how someone concludes an incident vanished."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        over = routes.MAX_INCIDENTS_RESPONSE + 5
        for n in range(over):
            inc = store.claim(
                models.Signal.create(
                    source="cloudwatch", native_id=f"alarm/{n}", title="t", resource="r"
                ),
                operating_mode=models.MODE_OBSERVE,
            )
            assert inc is not None

        payload = await self._get_incidents()
        self.assertEqual(len(payload["incidents"]), routes.MAX_INCIDENTS_RESPONSE)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total"], over)

    async def test_a_small_board_is_not_marked_truncated(self):
        """The common case must carry no scary flag."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        store.claim(
            models.Signal.create(
                source="cloudwatch", native_id="alarm/one", title="t", resource="r"
            ),
            operating_mode=models.MODE_OBSERVE,
        )
        payload = await self._get_incidents()
        self.assertEqual(len(payload["incidents"]), 1)
        self.assertNotIn("truncated", payload)

    async def test_id_filter_narrows_to_one_incident(self):
        """``?id=`` exists for the agent surface (see dashboard/server_internal_paths.py).

        The single-incident ``GET /incident`` route cannot be admitted to
        internal-secret callers without prefix-admitting the human-only
        proposal-decision route, so SOP-driven agents read one incident here.
        """
        first = store.claim(
            models.Signal.create(
                source="cloudwatch", native_id="alarm/one", title="t", resource="r"
            ),
            operating_mode=models.MODE_OBSERVE,
        )
        assert first is not None
        second = store.claim(
            models.Signal.create(
                source="cloudwatch", native_id="alarm/two", title="t", resource="r"
            ),
            operating_mode=models.MODE_OBSERVE,
        )
        assert second is not None

        request = mock.MagicMock(spec=web.Request)
        request.query = {"id": first.incident_id}
        response = await routes._handle_incidents(request)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertEqual(
            [inc["incident_id"] for inc in payload["incidents"]],
            [first.incident_id],
        )

        # An unknown id returns an empty list, not an error — absence is a
        # legitimate answer the SOPs handle.
        request.query = {"id": "INV-does-not-exist"}
        response = await routes._handle_incidents(request)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertEqual(payload["incidents"], [])


class TestSignalsSplitsParkedFromFiring(unittest.IsolatedAsyncioTestCase):
    """`/signals` must expose provider-side suppression as its OWN bucket.

    Three different reasons a signal is absent from ``firing`` — it cleared, we could not
    look, or a human parked it — and the route is the only place that can tell a caller
    which. Before this a parked signal appeared ONLY in the raw ``signals`` array with
    state ``unknown``, so the panel counting that array under a column headed "Firing"
    rendered "3 firing" above an empty queue with nothing to explain the contradiction.
    """

    def setUp(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        registry.reset_registry()
        # Only fakes: the public adapters would try to reach real APIs.
        self.registry = registry.OpsProviderRegistry()
        registry._registry = self.registry

    def tearDown(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_source(self, signals):
        class _Fake:
            id = "fake"
            display_name = "Fake"

            def configured(self):
                return True

            async def poll(self):
                return list(signals)

        self.registry.register_signal_source(_Fake())

    async def _get_signals(self):
        request = mock.MagicMock(spec=web.Request)
        response = await routes._handle_signals(request)
        return json.loads(getattr(response, "text", "{}") or "{}")

    @staticmethod
    def _signal(**kw):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        kw.setdefault("native_id", "a")
        kw.setdefault("title", "an alarm")
        return Signal.create(source="fake", **kw)

    async def test_a_parked_signal_lands_in_suppressed_and_nowhere_else(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source(
            [
                self._signal(
                    state=STATE_SUPPRESSED, suppressed_by="7f3a", suppressed_reason="silenced"
                )
            ]
        )
        payload = await self._get_signals()
        self.assertEqual(len(payload["suppressed"]), 1)
        self.assertEqual(payload["firing"], [])
        self.assertEqual(payload["cleared"], [])
        self.assertEqual(payload["unclaimed"], [])

    async def test_the_attribution_reaches_the_client(self):
        """Without who, "the app ignored my alarm" and "someone silenced it" look the same."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source(
            [
                self._signal(
                    state=STATE_SUPPRESSED, suppressed_by="7f3a", suppressed_reason="silenced"
                )
            ]
        )
        payload = await self._get_signals()
        parked = payload["suppressed"][0]
        self.assertEqual(parked["suppressed_by"], "7f3a")
        self.assertEqual(parked["suppressed_reason"], "silenced")

    async def test_parked_is_not_folded_into_cleared(self):
        """`cleared` asserts recovery, which a suppression does not — reconcile resolves on it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATE_OK,
            STATE_SUPPRESSED,
        )

        self._add_source(
            [
                self._signal(native_id="parked", state=STATE_SUPPRESSED),
                self._signal(native_id="recovered", state=STATE_OK),
            ]
        )
        payload = await self._get_signals()
        self.assertEqual(len(payload["cleared"]), 1)
        self.assertEqual(payload["cleared"][0]["id"], "fake:recovered")
        self.assertEqual(payload["suppressed"][0]["id"], "fake:parked")

    async def test_firing_work_is_unaffected_by_a_parked_neighbour(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATE_FIRING,
            STATE_SUPPRESSED,
        )

        self._add_source(
            [
                self._signal(native_id="parked", state=STATE_SUPPRESSED),
                self._signal(native_id="live", state=STATE_FIRING),
            ]
        )
        payload = await self._get_signals()
        self.assertEqual([s["id"] for s in payload["firing"]], ["fake:live"])
        self.assertEqual([s["id"] for s in payload["unclaimed"]], ["fake:live"])


class TestIncidentServesItsPostmortem(unittest.IsolatedAsyncioTestCase):
    """``/incident`` must report the artifact honestly, including its absence.

    ``log`` was served and typed from the day the route shipped while the renderer had no
    caller, so it was structurally always ``""``. ``log_path`` is new and is the riskier of
    the two: a path is a promise that a file is at the other end, so it must be empty
    whenever there is nothing there rather than computed from the id.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    async def _get(incident_id: str):
        request = mock.MagicMock(spec=web.Request)
        request.query = {"id": incident_id}
        response = await routes._handle_incident(request)
        return response.status, json.loads(getattr(response, "text", "{}") or "{}")

    async def test_a_closed_incident_serves_its_artifact_and_where_it_lives(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="drained it")

        status, payload = await self._get(inc.incident_id)
        self.assertEqual(status, 200)
        self.assertIn("DLQ deep", payload["log"])
        self.assertIn("drained it", payload["log"])
        self.assertTrue(payload["log_path"].endswith(f"{inc.incident_id}.md"))
        self.assertTrue(Path(payload["log_path"]).is_file())

    async def test_an_open_incident_reports_no_path_at_all(self):
        """A path for a file that does not exist would send the operator to an empty ls."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/open", title="live"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        _status, payload = await self._get(inc.incident_id)
        self.assertEqual(payload["log"], "")
        self.assertEqual(payload["log_path"], "")


class TestLedgerHygieneWiring(unittest.IsolatedAsyncioTestCase):
    """The daily pass is where the git-native memory loop gets its only caller.

    ``ledger_sync`` and ``ledger_index.import_pending`` were both built and tested and
    wired to NOTHING: sync had no caller anywhere, and ``dispatch``'s semantic recall
    queried an index nothing ever populated — so on a real install recall returned zero
    hits forever while every unit test passed. These tests exist so that cannot recur.

    The other property pinned here is ORDER. pull → hygiene → index → push is not
    cosmetic: deduping before the merge leaves freshly-arrived duplicates for tomorrow,
    indexing before hygiene embeds rows about to be pruned, and pushing before hygiene
    makes every instance re-derive the same dedupe locally so the repo never converges.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    async def _run(*, calls=None, sync_result="", index_result=None, hygiene=None):
        """Drive the handler with sync and indexing stubbed, recording the call order."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_sync

        order = calls if calls is not None else []

        async def _sync(*, direction="pull"):
            order.append(f"sync:{direction}")
            return sync_result

        def _hygiene():
            order.append("hygiene")
            return hygiene if hygiene is not None else {"deduped": 0, "decayed": 0, "pruned": 0}

        def _index():
            order.append("index")
            return index_result or {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0}

        request = mock.MagicMock(spec=web.Request)
        with mock.patch.object(ledger_sync, "sync_safely", _sync):
            with mock.patch.object(ledger, "hygiene", _hygiene):
                with mock.patch.object(routes, "_index_ledger_safely", _index):
                    response = await routes._handle_ledger_hygiene(request)
        return response, order

    async def test_the_pass_runs_sync_hygiene_and_index(self):
        """Regression: all three were unreachable on a real install."""
        response, order = await self._run()
        self.assertEqual(response.status, 200)
        self.assertIn("sync:pull", order)
        self.assertIn("hygiene", order)
        self.assertIn("index", order)
        self.assertIn("sync:push", order)

    async def test_stage_order_is_pull_hygiene_index_push(self):
        _, order = await self._run()
        self.assertEqual(order, ["sync:pull", "hygiene", "index", "sync:push"])

    async def test_a_pull_that_brought_news_marks_the_pass_changed(self):
        """``changed`` decides whether the cron speaks. A teammate's lesson arriving
        changes what the agent knows tomorrow, so it is worth saying."""
        response, _ = await self._run(sync_result="pulled")
        self.assertTrue(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_newly_indexed_rows_mark_the_pass_changed(self):
        response, _ = await self._run(
            index_result={"scanned": 5, "written": 5, "skipped": 0, "embedded": 5}
        )
        self.assertTrue(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_a_quiet_pass_stays_silent(self):
        """Silence-by-default is a hard requirement — the cron must not speak daily
        just because it ran."""
        response, _ = await self._run()
        self.assertFalse(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_hygiene_alone_still_marks_changed(self):
        response, _ = await self._run(hygiene={"deduped": 2, "decayed": 0, "pruned": 1})
        self.assertTrue(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_the_response_reports_each_stage_separately(self):
        """An operator debugging "why is recall empty" needs to see WHICH stage did
        nothing — a single boolean cannot distinguish "no remote" from "no model"."""
        response, _ = await self._run(
            sync_result="pulled",
            index_result={"scanned": 3, "written": 3, "skipped": 0, "embedded": 3},
        )
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertIn("sync", payload)
        self.assertIn("pull", payload["sync"])
        self.assertIn("push", payload["sync"])
        self.assertEqual(payload["index"]["written"], 3)
        self.assertIn("summary", payload)

    async def test_indexing_failure_does_not_lose_the_hygiene_pass(self):
        """Local dedupe is the part that always works and always matters; a missing
        embedding model must not cost it."""
        # Import ledger_index explicitly and patch the MODULE OBJECT. A dotted-path
        # patch target fails in a fresh process ("module ... has no attribute
        # 'ledger_index'"): the package attribute only exists once something has
        # imported the submodule, and routes imports it lazily inside the handler.
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            ledger,
            ledger_index,
            ledger_sync,
        )

        async def _sync(*, direction="pull"):
            return ""

        def _boom(*_a, **_kw):
            raise RuntimeError("no embedding model")

        request = mock.MagicMock(spec=web.Request)
        with mock.patch.object(ledger_sync, "sync_safely", _sync):
            with mock.patch.object(ledger, "hygiene", return_value={"deduped": 1}):
                # Not stubbing _index_ledger_safely: this exercises its real
                # swallow-everything contract rather than asserting it exists.
                with mock.patch.object(ledger_index, "import_pending", _boom):
                    response = await routes._handle_ledger_hygiene(request)
        self.assertEqual(response.status, 200)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertEqual(payload["summary"]["deduped"], 1, "hygiene still ran")
        self.assertEqual(payload["index"]["written"], 0)

    async def test_unconfigured_sync_is_not_an_error(self):
        """The single-user case: no remote, and nothing scary in the response."""
        response, _ = await self._run(sync_result="")
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["sync"]["pull"], "")

    async def test_index_helper_survives_having_no_vector_store(self):
        """Exercises the real helper with no store available."""
        with mock.patch(
            "kiro_crew.vector_memory.VectorMemoryStore",
            side_effect=RuntimeError("faiss unavailable"),
        ):
            result = routes._index_ledger_safely()
        self.assertEqual(result, {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0})

    async def test_persistence_switch_skips_automatic_ledger_index_write(self):
        """The nightly app sweep must obey the global automatic-write switch."""
        cfg = mock.MagicMock()
        cfg.memory.persistence_enabled = False
        with mock.patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            with mock.patch("kiro_crew.vector_memory.VectorMemoryStore") as store_cls:
                result = routes._index_ledger_safely()

        store_cls.assert_not_called()
        self.assertEqual(result, {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0})

    async def test_a_prune_fault_cannot_cost_the_ledger_push(self):
        """`prune_closed` sits before the push, and making the index read strict gave it a
        new way to raise.

        Letting it escape would mean one EACCES skips pushing the ledger `hygiene` just
        deduped -- a later fault costing an earlier step's work, which is the rule
        `dispatch.run_cycle`'s maintenance guards exist to keep. Pruning is the most
        deferrable step here (the next run retires the same incidents); the push is what
        other instances are waiting on. Found in review (Opus 4.8), which named the ordering
        consequence rather than the status code.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        def _refuse():
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(store, "prune_closed", _refuse):
            response, order = await self._run()

        self.assertEqual(response.status, 200)
        self.assertIn("sync:push", order, "the push must still run after a failed prune")
        self.assertLess(order.index("hygiene"), order.index("sync:push"))

    async def test_a_corrupt_index_still_stops_the_hygiene_pass(self):
        """Corruption never self-heals, so it must not be skipped quietly on every run."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        def _corrupt():
            raise json.JSONDecodeError("Expecting value", "{ not json", 2)

        with mock.patch.object(store, "prune_closed", _corrupt):
            with self.assertRaises(json.JSONDecodeError):
                await self._run()


if __name__ == "__main__":
    unittest.main()


class TestNeedsHumanNotifiesOnTheEdgeOnly(unittest.IsolatedAsyncioTestCase):
    """``/incident/transition`` must push a desktop notification on the EDGE into
    ``needs_human`` and nowhere else.

    The pre-transition status is captured before the write for one specific reason:
    ``store.update_fields`` re-enters ``transition`` with the SAME status on an unrelated
    field edit, so without the comparison an incident parked on a tool approval would
    re-toast at critical priority on every subsequent write to it — the unchanged
    condition ``SKILL.md``'s noise discipline forbids.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.pushes: list[tuple] = []
        # Patched by IDENTITY off the module object `routes` itself holds, not by dotted
        # string. A dotted patch walks package attributes, and `test_ledger_sync_git`
        # evicts this app's modules from `sys.modules` to simulate two processes — after
        # which the string can resolve a DIFFERENT `notify_out` copy than the handler
        # calls, so the mock silently never applies. That is a test-order-dependent
        # failure that reads as a product bug.

        def _record(*args, **kwargs):
            # A named function, not a lambda: `append(...) or True` reads as returning
            # bool but is typed `None | bool`, and mypy is blocking here.
            self.pushes.append(args)
            return True

        self._patch = mock.patch.object(
            routes.notify_out, "notify_needs_human", side_effect=_record
        )
        self._patch.start()

    def tearDown(self):
        import os

        self._patch.stop()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _transition(self, incident_id: str, status: str, **extra):
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": object()}
        request.json = mock.AsyncMock(return_value={"id": incident_id, "status": status, **extra})
        response = await routes._handle_transition(request)
        return response.status, json.loads(getattr(response, "text", "{}") or "{}")

    def _claim(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        return inc

    async def test_entering_needs_human_notifies_once(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        status, _payload = await self._transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn(inc.incident_id, self.pushes[0])

    async def test_a_second_write_while_still_blocked_notifies_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        await self._transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        await self._transition(
            inc.incident_id, models.STATUS_NEEDS_HUMAN, diagnosis="still thinking"
        )
        self.assertEqual(len(self.pushes), 1, "a re-block must not re-notify")

    async def test_a_transition_to_any_other_status_notifies_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        await self._transition(inc.incident_id, models.STATUS_INVESTIGATING)
        await self._transition(inc.incident_id, models.STATUS_RESOLVED, resolution="drained it")
        self.assertEqual(self.pushes, [])

    async def test_a_failing_notifier_cannot_fail_the_transition(self):
        """The state change is already durable; a notification centre fault is not news."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        self._patch.stop()
        try:
            with mock.patch.object(
                routes.notify_out,
                "notify_needs_human",
                side_effect=RuntimeError("bus exploded"),
            ):
                with self.assertRaises(RuntimeError):
                    # notify_out itself never raises (see test_notify_out); this proves the
                    # route does not swallow a genuine programming fault silently, which is
                    # why the chokepoint and not the caller owns the try/except.
                    await self._transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        finally:
            self._patch.start()

    async def test_a_credential_in_an_agent_diagnosis_is_redacted_before_storage(self):
        """`diagnosis`/`resolution` are AGENT-AUTHORED and this app persists and renders them.

        An investigating agent that pastes a provider token into its writeup stored that token
        in the incident index and painted it on the board, in the handover digest and in the
        Slack mirror. The action-note, Slack and ledger sinks were already covered by
        `_safe_outbound`; this path was not. Found in review.

        Asserts on what `store` actually holds, not on the response: the durable copy is what
        every other surface reads.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = self._claim()
        leaked = "glpat-IOSFODNN7EXAMPLE"
        status, _payload = await self._transition(
            inc.incident_id,
            models.STATUS_NEEDS_HUMAN,
            diagnosis=f"the worker used {leaked} and 403'd",
            resolution=f"rotate {leaked}",
        )
        self.assertEqual(status, 200)

        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertNotIn(leaked, stored.diagnosis)
        self.assertNotIn(leaked, stored.resolution)
        # The surrounding prose survives — this is redaction, not truncation.
        self.assertIn("403", stored.diagnosis)
        self.assertIn("rotate", stored.resolution)

    async def test_machine_id_fields_are_not_run_through_the_redactor(self):
        """`slot_key`/`slack_thread_ts` are ids, shape-checked downstream. Redacting them
        would corrupt an id that happened to match a token pattern."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = self._claim()
        await self._transition(
            inc.incident_id,
            models.STATUS_NEEDS_HUMAN,
            slack_thread_ts="1717171717.123456",
        )
        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertEqual(stored.slack_thread_ts, "1717171717.123456")


class TestStateReportsTheNotificationChannel(unittest.IsolatedAsyncioTestCase):
    """``/state`` must carry ``notify`` so Settings can render it without a new endpoint.

    Readiness depends on live gateway state (is there a notification bus in this process),
    so it cannot be answered from the unauthenticated config file the panel already has.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_state_carries_the_notify_status_with_every_field_the_ui_types(self):
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": None}
        response = await routes._handle_state(request)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertIn("notify", payload)
        for key in ("enabled", "bus_available", "ready", "detail", "channels"):
            self.assertIn(key, payload["notify"])

    async def test_a_process_without_a_bus_is_not_reported_ready(self):
        """`enabled` alone must never paint as active — nothing would be delivered."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import notify_out

        notify_out.set_settings(enabled=True)
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": None}
        response = await routes._handle_state(request)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertTrue(payload["notify"]["enabled"])
        self.assertFalse(payload["notify"]["ready"])


class TestLedgerWritesAreRedacted(unittest.IsolatedAsyncioTestCase):
    """``ledger.jsonl`` is the one artifact that leaves the machine.

    ``ledger_sync`` commits and pushes it verbatim, and a ``fix`` field is the likeliest
    place for a pasted credential because that is what a fix looks like. Evidence->prompt
    and incident->Slack both pass a redaction chokepoint; this path did not.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_a_credential_in_a_fix_never_reaches_the_ledger_file(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        secret = "glpat-IOSFODNN7EXAMPLE"
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/apps/ops-mission-control/ledger",
                    json={
                        "pattern": "cross-account assume-role denied",
                        "fix": f"aws sts assume-role --access-key {secret}",
                    },
                )
                self.assertEqual(resp.status, 200)

        # Not in the returned entry...
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertNotIn(secret, entries[0].fix)
        # ...and not in the file that git actually pushes.
        self.assertNotIn(secret, ledger.ledger_path().read_text(encoding="utf-8"))

    async def test_redaction_happens_before_the_id_is_computed(self):
        from aiohttp.test_utils import TestClient, TestServer

        """Two entries differing only in a redacted secret SHOULD dedupe to one.

        The id is ``sha256(lower(pattern)|lower(fix))``, so redacting after hashing would
        keep one row per distinct secret -- the corpus would grow a row for every leaked
        credential while still storing none of them usefully.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                for secret in ("glpat-IOSFODNN7EXAMPLE", "glpat-I44QH8DHBEXAMPLE"):
                    resp = await client.post(
                        "/api/apps/ops-mission-control/ledger",
                        json={"pattern": "assume-role denied", "fix": f"key {secret}"},
                    )
                    self.assertEqual(resp.status, 200)

        self.assertEqual(len(ledger.read_entries()), 1, "both must collapse onto one id")

    async def test_ordinary_prose_is_not_mangled(self):
        from aiohttp.test_utils import TestClient, TestServer

        """Redaction on a human-authored field must not corrupt a real fix."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        fix = "drain the SQS queue, then scale the checkout ASG to 6"
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await client.post(
                    "/api/apps/ops-mission-control/ledger",
                    json={"pattern": "checkout p99 breach", "fix": fix},
                )
        self.assertEqual(ledger.read_entries()[0].fix, fix)


class TestHygieneIsPrimaryOnly(unittest.IsolatedAsyncioTestCase):
    """Exactly one instance may prune a shared ledger.

    `is_primary()` was written for this and then wired to no enforcement point, while
    `sops/rotation-check.md` told operators this route "self-gates on `is_primary()` at
    runtime" — true of no code. A SOP asserting a gate that does not exist is worse than
    no gate: it stops the next person looking for one.

    The cost asymmetry is the whole argument, and it is recorded in the app's own
    features log: a duplicate CLAIM wastes an agent turn, a duplicate PRUNE deletes
    knowledge. Concurrency on the maintenance path is the more expensive of the two.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_a_non_primary_instance_is_refused(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "is_primary", return_value=False):
                with mock.patch.object(routes.rotation, "primary_owner", return_value="alice"):
                    async with TestClient(TestServer(app)) as client:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/ledger/hygiene", json={}
                        )
                        # 409, not 403: the caller is authenticated and permitted, it is
                        # simply not this instance's job.
                        self.assertEqual(resp.status, 409)
                        body = await resp.json()

        self.assertEqual(body["code"], "not_primary")
        self.assertFalse(body["changed"])
        # Naming the leader is the difference between a refusal an operator can act on
        # and one that only tells them to look elsewhere.
        self.assertIn("alice", body["error"])

    async def test_the_refusal_runs_no_maintenance_at_all(self):
        """A refusal that had already pruned would defeat the point of refusing."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "is_primary", return_value=False):
                with mock.patch.object(ledger, "hygiene") as hygiene:
                    with mock.patch.object(routes.store, "prune_closed") as prune:
                        async with TestClient(TestServer(app)) as client:
                            await client.post(
                                "/api/apps/ops-mission-control/ledger/hygiene", json={}
                            )
        hygiene.assert_not_called()
        prune.assert_not_called()

    async def test_the_primary_still_runs_it(self):
        """The gate must not become a blanket block on maintenance ever happening."""
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "is_primary", return_value=True):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.post(
                        "/api/apps/ops-mission-control/ledger/hygiene", json={}
                    )
                    self.assertEqual(resp.status, 200)

    async def test_an_unnamed_leader_still_refuses_cleanly(self):
        """`primary_owner` is "" when the schedule names nobody — the refusal must still work."""
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "is_primary", return_value=False):
                with mock.patch.object(routes.rotation, "primary_owner", return_value=""):
                    async with TestClient(TestServer(app)) as client:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/ledger/hygiene", json={}
                        )
                        self.assertEqual(resp.status, 409)
                        self.assertEqual((await resp.json())["code"], "not_primary")
