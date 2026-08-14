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
from typing import cast
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
    """`/incidents` used to serialize the ENTIRE index on every dashboard poll.

    Fine at three incidents. Once a flapping alarm has minted hundreds — which became
    possible when resolved alarms were made re-claimable — it is an ever-growing payload
    on a polled endpoint.
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
        """``?id=`` exists for the agent surface (see dashboard/server.py).

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
        leaked = "AKIAIOSFODNN7EXAMPLE"
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


class TestAnActionSchedulesItsOwnVerification(unittest.IsolatedAsyncioTestCase):
    """A 2xx from a provider is no longer the end of the story.

    `_handle_action` used to await `sink.execute`, audit, and return — so the response's
    `ok` meant only "transmitted". Checkmk documents exactly that gap for its Livestatus
    command dispatch; Nagios's command pipe returns nothing at all. The route now records
    what was done and when to look again, and says which of the two it is doing.
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

    def _incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        return inc

    async def test_a_silence_is_rechecked_at_the_end_of_its_own_window(self):
        """The schedule `ACTION_SILENCE`'s mandatory expiry buys.

        A suppression that expires straight back into the same firing condition is the
        strongest evidence available that nothing was fixed — so the recheck is anchored
        to the window, not to a flat interval invented for it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._incident()
        verdict, due = routes._schedule_verification(inc.incident_id, models.ACTION_SILENCE, 3600)
        self.assertEqual(verdict, models.VERIFY_PENDING)
        # 3600s out is well past the 5-minute default, which is how we know the window
        # (not the default) chose the time.
        flat = routes._schedule_verification(inc.incident_id, models.ACTION_RESOLVE, None)[1]
        self.assertGreater(due, flat)

    async def test_a_resolve_that_really_suppressed_is_rechecked_after_the_suppression(self):
        """The window comes from what HAPPENED, not from which verb was asked for.

        Datadog aliases `resolve` onto a bounded mute, and only `EXPIRING_ACTIONS` (i.e.
        `silence`) receives a `duration_secs` from the route — so a resolve that established
        a four-hour mute was scheduled on the five-minute default, rechecked inside its own
        suppression, and recorded a false miss against the cited ledger entries. The sink
        reports the window via `ActionResult.suppressed_secs` and the schedule honours it.

        Asserted on the SCHEDULE (a resolve carrying a window must land later than one
        without), which is the property the ledger accounting depends on. Found in review.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._incident()
        with_window = routes._schedule_verification(
            inc.incident_id, models.ACTION_RESOLVE, models.DEFAULT_SILENCE_SECS
        )[1]
        default = routes._schedule_verification(inc.incident_id, models.ACTION_RESOLVE, None)[1]
        self.assertGreater(
            with_window,
            default,
            "a resolve that suppressed for hours was rechecked on the flat default",
        )

    async def test_an_ack_is_recorded_as_not_checkable_rather_than_left_blank(self):
        """An ack leaves an alert firing BY DESIGN, so firing state proves nothing.

        `normalize_state` maps `acknowledged` onto `firing` on purpose. Deriving a verdict
        from the alarm's state would turn an unverifiable write into a confident one — so
        this says "cannot observe" instead, and schedules no recheck to mislead a later
        cycle.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = self._incident()
        verdict, due = routes._schedule_verification(inc.incident_id, models.ACTION_ACK, None)
        self.assertEqual(verdict, models.VERIFY_NOT_CHECKABLE)
        self.assertEqual(due, "")
        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertEqual(stored.last_action, models.ACTION_ACK)
        self.assertNotIn(stored.verification, models.OPEN_VERIFICATIONS)

    async def test_a_comment_is_not_verifiable_either(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._incident()
        verdict, _due = routes._schedule_verification(inc.incident_id, models.ACTION_COMMENT, None)
        self.assertEqual(verdict, models.VERIFY_NOT_CHECKABLE)

    async def test_a_vanished_incident_degrades_instead_of_failing_the_action(self):
        """The provider write already happened and cannot be undone.

        A bookkeeping failure must not turn a completed action into a 500 — it degrades to
        "no verification scheduled", which the response then reports honestly as "".
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self.assertEqual(
            routes._schedule_verification("INV-nope", models.ACTION_SILENCE, 60), ("", "")
        )

    async def test_an_observe_only_action_schedules_no_recheck_at_all(self):
        """`ok=True` from the noop sink means "we successfully did nothing".

        The recheck cannot tell that from a real provider write: it read the still-firing
        alarm as the ACTION having failed and charged a `miss_count` to every ledger entry
        the investigation cited. On a default install that is the ONLY path — `cloudwatch`
        and `webhook` register no ActionSink, so every action falls through to `noop` — so
        watching the proposal flow, which is exactly what an operator is told to do before
        granting real authority, demoted their own proven knowledge for a write nobody made.

        Asserted through the real handler rather than `_schedule_verification`, because the
        gate lives in `_handle_action` and the direct-call tests above cannot see it.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            ledger,
            policy_store,
            registry,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            LedgerEntry,
            Signal,
        )

        # A properly scoped act-rule, so the autonomy gate genuinely allows the write.
        # Seeded on the KEYSTONE floor, not in `config.json`: that file is agent-writable and
        # the read path deliberately ignores a ceiling found there (see `policy_store`).
        policy_store.set_mode("act")
        policy_store.set_rules(
            [
                {
                    "source": "cloudwatch",
                    "mode": "act",
                    "actions": ["resolve"],
                    # A glob with a literal, matching this test's `arn:dlq`. An
                    # all-wildcard `"*"` is refused as the blanket grant it is.
                    "resource_glob": "arn:*",
                }
            ]
        )

        registry.reset_registry()
        self.addCleanup(registry.reset_registry)
        signal = Signal.create(
            source="cloudwatch", native_id="alarm/dlq", title="DLQ deep", resource="arn:dlq"
        )
        entry = ledger.upsert(
            LedgerEntry.create(
                pattern="DLQ deep",
                fix="drain it",
                fingerprints=[signal.fingerprint],
                confidence="high",
                trust="verified",
            )
        )
        ledger.record_use(entry.entry_id)
        ledger.record_use(entry.entry_id)
        self.assertTrue(ledger.entry_unlocks_fast_path(ledger.read_entries()[0]))

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        incident = store.claim(signal, operating_mode="act")
        assert incident is not None
        store.update_fields(incident.incident_id, ledger_matches=[entry.entry_id])

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/apps/ops-mission-control/incident/action",
                    json={"id": incident.incident_id, "action": "resolve"},
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()

        # The write was "accepted" by the observe-only sink...
        self.assertTrue(body["ok"])
        # ...and nothing was scheduled, so no later cycle can reach a verdict about it.
        self.assertEqual(body["verification"], "")
        self.assertEqual(body["verify_after"], "")
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.verification, "")
        # The operator's proven entry is untouched and still on the fast path.
        self.assertEqual(ledger.read_entries()[0].miss_count, 0)
        self.assertTrue(ledger.entry_unlocks_fast_path(ledger.read_entries()[0]))


class TestAuthorityIsPerProvider(unittest.IsolatedAsyncioTestCase):
    """A grant on one provider must not execute against another.

    ``authorize_action`` gates on ``incident.signal`` and ``AutonomyRule.matches`` keys on
    ``signal.source``, so a rule only ever grants authority over the provider that RAISED
    the signal. ``sink`` was taken from the request body verbatim, so the two could
    disagree: a webhook signal carrying a Datadog monitor id, a webhook-scoped act-rule,
    and ``sink="datadog"`` passed the webhook check and then wrote to Datadog. The gate was
    right; the code did not act on what the gate had approved.
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

    def _webhook_incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(
                source="webhook",
                native_id="probe-1",
                title="checkout latency",
                labels={"dd_monitor_id": "12345"},
            ),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        return inc

    async def test_a_cross_provider_sink_is_refused(self):
        from aiohttp.test_utils import TestClient, TestServer

        incident = self._webhook_incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.rotation, "authorize_action", return_value=(True, "granted by rule")
            ):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.post(
                        "/api/apps/ops-mission-control/incident/action",
                        json={
                            "id": incident.incident_id,
                            "action": "silence",
                            "sink": "datadog",
                        },
                    )
                    self.assertEqual(resp.status, 403, "a cross-provider sink must be refused")
                    body = await resp.json()

        self.assertFalse(body["authorized"])
        self.assertIn("datadog", body["error"])
        self.assertIn("webhook", body["error"])

    async def test_naming_the_owning_sink_is_still_accepted(self):
        from aiohttp.test_utils import TestClient, TestServer

        """The guard must not break the honest case it exists to narrow."""
        incident = self._webhook_incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.rotation, "authorize_action", return_value=(True, "granted by rule")
            ):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.post(
                        "/api/apps/ops-mission-control/incident/action",
                        json={
                            "id": incident.incident_id,
                            "action": "comment",
                            "sink": "webhook",
                        },
                    )
                    self.assertEqual(resp.status, 200)


class TestTheAuthorizationGateDoesNotBlockTheLoop(unittest.IsolatedAsyncioTestCase):
    """``authorize_action`` must never run inline on the asyncio loop.

    It is synchronous by design, and its off-shift check reads the committed schedule and
    -- with no ``schedule-file.github_login`` configured, which is the documented default
    -- resolves this instance's identity by spawning ``gh api user``: a blocking HTTPS
    round trip with a 10s timeout, on the FIRST call of a fresh gateway process.

    ``/incident/action`` and ``/incident/proposal/decide`` are the two handlers that reach
    the gate without awaiting ``registry.resolve_shift()`` first, so nothing has warmed the
    login cache off-loop by the time they get there. Run inline, the spawn freezes every
    other task on the loop -- the user's chat turn and the liveness heartbeat -- for the
    whole window.

    Asserted behaviourally rather than by grepping for ``to_thread``: a heartbeat coroutine
    ticks while the request is in flight, so the test fails if the loop ever stalls,
    whatever mechanism a future refactor uses to stay off it.
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

    def _incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="webhook", native_id="probe-1", title="latency"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        return inc

    async def test_a_slow_identity_lookup_does_not_stall_other_tasks(self):
        import asyncio
        import time

        from aiohttp.test_utils import TestClient, TestServer

        incident = self._incident()
        app = web.Application()
        routes.register_routes(app)

        blocking_secs = 0.3

        def _slow_gate(_signal, _action):
            # Stands in for the `gh api user` spawn: synchronous, and long enough that a
            # stalled loop is unambiguous rather than a timing coincidence.
            time.sleep(blocking_secs)
            return (True, "granted by rule")

        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(blocking_secs / 10)
                ticks += 1

        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "authorize_action", _slow_gate):
                async with TestClient(TestServer(app)) as client:
                    beat = asyncio.create_task(_heartbeat())
                    try:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/incident/action",
                            json={
                                "id": incident.incident_id,
                                "action": "comment",
                                "sink": "webhook",
                            },
                        )
                        self.assertEqual(resp.status, 200)
                    finally:
                        beat.cancel()

        # Inline, the loop gets zero slices during the sleep. Off-loop it gets ~10; assert
        # a floor well under that so the test is not itself timing-flaky.
        self.assertGreaterEqual(
            ticks,
            3,
            "the event loop stalled during authorize_action — it must run off-loop "
            f"(only {ticks} heartbeat tick(s) in {blocking_secs}s)",
        )


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

        secret = "AKIAIOSFODNN7EXAMPLE"
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
                for secret in ("AKIAIOSFODNN7EXAMPLE", "AKIAI44QH8DHBEXAMPLE"):
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


class TestProposeLoop(unittest.IsolatedAsyncioTestCase):
    """`propose` mode used to be behaviourally identical to `observe`.

    `authorize_action` refuses anything below `act`, `proposed_action` was declared and
    never assigned, and there was no store, no approve endpoint and no timeout. So the
    mode most operators will live in — "tell me what you would do" — was prose in a chat
    transcript with nothing to approve.

    The load-bearing property is that **the drafted text is the contract**: an approval
    binds to the exact terms shown, and executes those, not whatever the request supplies.
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

    def _incident(self, mode=None):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(
                source="webhook", native_id="p-1", title="checkout 5xx", state="firing"
            ),
            operating_mode=mode or models.MODE_PROPOSE,
        )
        assert inc is not None
        return inc

    async def _post(self, client, path, body):
        return await client.post(f"/api/apps/ops-mission-control{path}", json=body)

    async def test_a_proposal_is_queued_and_visible(self):
        """The queue is the thing an operator could not see at all before."""
        from aiohttp.test_utils import TestClient, TestServer

        inc = self._incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await self._post(
                    client,
                    "/incident/propose",
                    {
                        "id": inc.incident_id,
                        "action": "comment",
                        "sink": "webhook",
                        "note": "Draining the stuck consumer.",
                    },
                )
                self.assertEqual(resp.status, 200)
                listed = await (await client.get("/api/apps/ops-mission-control/proposals")).json()

        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["proposals"][0]["state"], "pending")
        self.assertEqual(listed["proposals"][0]["note"], "Draining the stuck consumer.")

    async def test_approving_a_changed_draft_is_refused(self):
        """THE property. Approve the bytes you read, or re-read and decide again."""
        from aiohttp.test_utils import TestClient, TestServer

        inc = self._incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await self._post(
                    client,
                    "/incident/propose",
                    {"id": inc.incident_id, "action": "comment", "sink": "webhook", "note": "v1"},
                )
                resp = await self._post(
                    client,
                    "/incident/proposal/decide",
                    {"id": inc.incident_id, "approve": True, "digest": "0000000000000000"},
                )
                self.assertEqual(resp.status, 409)
                body = await resp.json()

        self.assertEqual(body["code"], "proposal_conflict")
        self.assertIn("contract", body["error"])

    async def test_approval_cannot_launder_a_write_past_the_autonomy_gate(self):
        """Approving on an `observe`/`propose` instance records the decision and refuses.

        Otherwise the propose loop would be an autonomy bypass: draft anything, approve
        it, and the mode ceiling never applies.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        inc = self._incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await self._post(
                    client,
                    "/incident/propose",
                    {"id": inc.incident_id, "action": "comment", "sink": "webhook", "note": "n"},
                )
                # A real approver reads the draft and echoes its digest — an approval
                # without one is refused now, which is the point of the mechanism.
                # Asserted rather than indexed straight through: `get_incident` returns
                # `Incident | None` and `proposed_action` is optional, so a missing draft
                # names itself here instead of surfacing as a TypeError two lines down.
                stored = store.get_incident(inc.incident_id)
                assert stored is not None and stored.proposed_action is not None
                digest = stored.proposed_action["digest"]
                resp = await self._post(
                    client,
                    "/incident/proposal/decide",
                    {"id": inc.incident_id, "approve": True, "digest": digest},
                )
                self.assertEqual(resp.status, 403)
                body = await resp.json()

        self.assertFalse(body["executed"])
        self.assertEqual(body["code"], "not_authorized")

    async def test_rejecting_records_the_decision_and_executes_nothing(self):
        from aiohttp.test_utils import TestClient, TestServer

        inc = self._incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await self._post(
                    client,
                    "/incident/propose",
                    {"id": inc.incident_id, "action": "comment", "sink": "webhook", "note": "n"},
                )
                resp = await self._post(
                    client, "/incident/proposal/decide", {"id": inc.incident_id, "approve": False}
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()

        self.assertFalse(body["executed"])
        self.assertEqual(body["proposal"]["state"], "rejected")

    async def test_proposing_is_allowed_below_act_because_it_changes_nothing(self):
        """The safe half of the loop must work in the mode people actually run."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._incident(mode=models.MODE_OBSERVE)
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await self._post(
                    client,
                    "/incident/propose",
                    {"id": inc.incident_id, "action": "ack", "sink": "webhook", "note": "n"},
                )
        self.assertEqual(resp.status, 200)

    async def test_an_unknown_action_cannot_be_proposed(self):
        from aiohttp.test_utils import TestClient, TestServer

        inc = self._incident()
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await self._post(
                    client,
                    "/incident/propose",
                    {"id": inc.incident_id, "action": "rm -rf", "sink": "webhook"},
                )
        self.assertEqual(resp.status, 400)

    async def test_an_approved_proposal_executes_the_stored_note_not_the_request(self):
        """The whole mechanism in one assertion.

        A decide request that could supply its own note would let the text change between
        the operator reading the draft and the action firing — so the executor must read
        from the store. Asserted by handing the sink a recorder and comparing what it
        received against what was drafted, while the approving request tries to smuggle
        different text.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = self._incident(mode=models.MODE_ACT)
        seen: list[dict] = []

        class _Recorder:
            id = "webhook"
            display_name = "Recorder"

            def configured(self):
                return True

            def supported_actions(self):
                return ("comment",)

            async def execute(self, signal, action, payload):
                seen.append({"action": action, "payload": dict(payload)})
                from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
                    ActionResult,
                )

                return ActionResult(ok=True, action=action, detail="recorded")

        app = web.Application()
        routes.register_routes(app)
        # Registering on the process-wide registry LEAKS: registration is ADD-only, so this
        # fake stays the incumbent `webhook` sink for every later test in the file (it made
        # one fail 1300 lines below). Drop the registry when this test ends.
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry as registry_mod

        self.addCleanup(registry_mod.reset_registry)
        registry = routes.get_registry()
        registry.register_action_sink(_Recorder())
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.rotation, "authorize_action", return_value=(True, "granted")
            ):
                async with TestClient(TestServer(app)) as client:
                    await self._post(
                        client,
                        "/incident/propose",
                        {
                            "id": inc.incident_id,
                            "action": "comment",
                            "sink": "webhook",
                            "note": "THE DRAFTED WORDS",
                        },
                    )
                    stored = store.get_incident(inc.incident_id)
                    assert stored is not None and stored.proposed_action is not None
                    digest = stored.proposed_action["digest"]
                    resp = await self._post(
                        client,
                        "/incident/proposal/decide",
                        # A hostile approver: correct digest (it DID read the draft), but
                        # trying to substitute its own text in the same request.
                        {
                            "id": inc.incident_id,
                            "approve": True,
                            "digest": digest,
                            "note": "SMUGGLED WORDS",
                        },
                    )
                    self.assertEqual(resp.status, 200)

        self.assertEqual(len(seen), 1, "the approved action must actually run")
        self.assertEqual(seen[0]["payload"]["note"], "THE DRAFTED WORDS")


class TestTheCapabilityProbeFailsClosed(unittest.TestCase):
    """`_sink_refuses` gates a real provider write, so an UNCONFIRMED capability must refuse.

    The check exists to stop an authorized action reaching an adapter with no defined
    behaviour for it (GitHub Issues supports only `{resolve, comment}`; an `ack` there is an
    undefined `execute` call against a real repo). It first failed OPEN in three ways — a
    missing `supported_actions`, a raising probe, and an EMPTY set all returned "" (allow) —
    which is the exact undefined call it exists to prevent, reached three other ways.

    "I could not confirm support" is not "it is supported." Authorization says the operator
    permits the verb, not that this sink can perform it, so a broken probe against a
    production write must refuse. `supported_actions` is part of the `ActionSink` protocol, so
    absence is a broken adapter, not a legacy one. Found in review (GPT 5.6).
    """

    def test_a_missing_probe_refuses(self):
        class NoProbe:
            id = "companion"

        self.assertTrue(routes._sink_refuses(NoProbe(), "ack"))

    def test_a_non_callable_probe_refuses(self):
        class Attr:
            id = "weird"
            supported_actions = frozenset({"ack"})  # an attribute, not a method

        self.assertTrue(routes._sink_refuses(Attr(), "ack"))

    def test_a_raising_probe_refuses_rather_than_crashing(self):
        class Raising:
            id = "flaky"

            def supported_actions(self):
                raise RuntimeError("boom")

        # Refuses (truthy reason) AND does not propagate the exception.
        self.assertTrue(routes._sink_refuses(Raising(), "ack"))

    def test_an_empty_set_refuses_every_action(self):
        class DeclaresNothing:
            id = "nothing"

            def supported_actions(self):
                return frozenset()

        self.assertTrue(routes._sink_refuses(DeclaresNothing(), "ack"))

    def test_a_declared_action_is_allowed_and_an_undeclared_one_is_not(self):
        class GitHubish:
            id = "gh"

            def supported_actions(self):
                return frozenset({"resolve", "comment"})

        self.assertEqual(routes._sink_refuses(GitHubish(), "resolve"), "")
        self.assertTrue(routes._sink_refuses(GitHubish(), "ack"))


class TestTheAutonomyGateIsAChokepoint(unittest.IsolatedAsyncioTestCase):
    """The provider write must be unreachable without passing the autonomy gate.

    Review named the shape precisely: "autonomy enforcement is a convention, not a
    chokepoint -- a third caller can silently skip the gate." It was true.
    ``authorize_action`` ran at two independent call sites, ``ActionSink.execute`` policed
    nothing by design (spec 5.3), and the only thing joining them was a docstring saying
    callers MUST resolve the gate first.

    It is now structural: ``_authorize`` is the sole minter of an ``_Authorized`` permit,
    and ``_execute_authorized`` -- the only function that touches ``sink.execute`` -- takes
    one. These tests pin the two properties that make that hold, because both are the kind
    of thing a later refactor removes without noticing.
    """

    def setUp(self):
        import os

        # Own data home, like every other store-touching class here. Without it
        # `store.claim` writes against whatever home the previous test left behind and can
        # return None — which is how the behavioural case below first failed.
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

    def test_only_one_function_calls_sink_execute(self):
        """A second call site would be a write that skipped the permit."""
        import pathlib
        import re

        backend = pathlib.Path(routes.__file__).parent
        offenders = []
        for path in sorted(backend.glob("*.py")):
            # `encoding=` is required: these sources contain em-dashes, and a bare `read_text()`
            # decodes as cp1252 on Windows and raises before the assertion can run.
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if re.search(r"\bsink\.execute\(|\.execute\(.*signal", line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(
            len(offenders),
            1,
            "ActionSink.execute must have exactly ONE caller (_execute_authorized), so a "
            "provider write cannot be reached without a gate-minted permit. Found:\n  "
            + "\n  ".join(offenders),
        )
        self.assertIn("permit.signal, permit.action", offenders[0])

    def test_a_permit_can_only_be_minted_by_the_gate(self):
        """``_Authorized`` must not be constructed anywhere but ``_authorize``.

        A permit built beside the write would be a rubber stamp: the type would still be
        satisfied while no gate had run.
        """
        import inspect
        import re

        source = inspect.getsource(routes)
        # Constructor calls, not the class definition or type annotations.
        constructions = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"(?<!class )\b_Authorized\(", line)
        ]
        self.assertEqual(
            constructions,
            ["return _Authorized(signal, action, reason), reason"],
            "_Authorized must be minted ONLY by _authorize, which is what makes holding "
            f"one proof the gate allowed the action. Found: {constructions}",
        )

    async def test_a_denied_action_never_reaches_the_sink(self):
        """The behavioural half: a deny must produce no provider write at all."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="webhook", native_id="probe-1", title="latency"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None

        executed = []

        class _Recorder:
            id = "webhook"
            display_name = "Webhook"

            def configured(self):
                return True

            def supported_actions(self):
                return frozenset(models.VALID_ACTIONS)

            async def execute(self, signal, action, payload):
                executed.append(action)
                raise AssertionError("a denied action reached the sink")

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.rotation, "authorize_action", return_value=(False, "no rule grants ack")
            ):
                with mock.patch.object(
                    routes.get_registry(), "action_sink", return_value=_Recorder()
                ):
                    async with TestClient(TestServer(app)) as client:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/incident/action",
                            json={"id": inc.incident_id, "action": "ack", "sink": "webhook"},
                        )
                        self.assertEqual(resp.status, 403)

        self.assertEqual(executed, [], "a denied action must produce no provider write")


class TestBlockedStateReadsThePublicSlotContract(unittest.IsolatedAsyncioTestCase):
    """"Waiting on you" must not depend on a private attribute of another module.

    ``_slot_state`` derived ``pending_approval`` by reading ``slot._approval_futures``
    directly. Review flagged it and was right: a private attribute is not a contract, so a
    core refactor that renamed it would silently turn "waiting on you" into "progressing" on
    this board -- the operator stops being told an incident needs them -- with nothing
    failing anywhere to say so.

    ``_ChatSlot.to_dict()`` is the owner's public serializer and already derives the same
    fact. These tests pin BOTH that we ask it, and that our answer agrees with the core's
    across the states that matter -- against the real class, not a stand-in, because a mock
    would happily agree with a contract that no longer exists.
    """

    def test_no_private_slot_attribute_is_read(self):
        import inspect
        import re

        source = inspect.getsource(routes._slot_state)
        # CODE only. The comment above the fix names `_approval_futures` to record what was
        # wrong and why, so a bare substring check would forbid explaining the very thing it
        # enforces -- the same reason this file has a `rendered()` helper on the frontend
        # side. Strip comments, then look for actual access (attribute or getattr).
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        access = re.findall(r"\.\s*_approval_futures|getattr\([^)]*_approval_futures", code)
        self.assertEqual(
            access,
            [],
            "_slot_state must not reach into DashboardState privates -- ask to_dict(), "
            f"which the core keeps correct. Found: {access}",
        )
        self.assertIn("to_dict", code)

    async def test_it_agrees_with_the_core_across_the_approval_lifecycle(self):
        import asyncio

        from kiro_crew.dashboard import state as state_mod

        slot = state_mod._ChatSlot("probe")

        class _State:
            @staticmethod
            def get_slot(key):
                return slot

        class _Req:
            app = {"state": _State()}

        # `cast`, not a real Request: `_slot_state` only ever touches `.app["state"]`, and
        # building an aiohttp Request to carry one dict would test the framework, not this.
        request = cast(web.Request, _Req())

        def ours() -> bool:
            state = routes._slot_state(request, "probe")
            assert state is not None, "the stub always resolves a slot"
            return bool(state["pending_approval"])

        def theirs() -> bool:
            return bool(slot.to_dict().get("pending_approval"))

        self.assertEqual(ours(), theirs(), "baseline")
        self.assertFalse(ours())

        # A real pending approval, registered the way the core registers one.
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-1"] = future
        self.assertEqual(ours(), theirs(), "while an approval is pending")
        self.assertTrue(ours(), "a pending approval must read as blocked")

        future.set_result("allow")
        self.assertEqual(ours(), theirs(), "after the approval resolves")
        self.assertFalse(ours(), "a resolved approval must stop reading as blocked")

    async def test_a_faulty_serializer_degrades_instead_of_blanking_the_board(self):
        """This read paints the whole board, so a slot fault must not 500 it."""

        class _Exploding:
            running = True
            pending_approval = True
            waiting_for_input = False
            messages: list = []

            def to_dict(self, **_kw):
                raise RuntimeError("slot serializer blew up")

        class _State:
            @staticmethod
            def get_slot(key):
                return _Exploding()

        class _Req:
            app = {"state": _State()}

        result = routes._slot_state(cast(web.Request, _Req()), "probe")
        assert result is not None, "a serializer fault must still yield a state dict"
        # Falls back to the PUBLIC attribute, not to the old private reach-in.
        self.assertTrue(result["pending_approval"])


class TestRotationDescribeDoesNotBlockTheLoop(unittest.IsolatedAsyncioTestCase):
    """``rotation.describe`` reaches the same ``gh api user`` spawn the gate does.

    ``describe()`` -> ``is_primary()`` -> ``_schedule_me()`` -> ``resolve_login()``, which
    spawns ``gh api user`` synchronously with a 10s timeout on a cold login cache.

    An earlier revision of these handlers argued the inline call was safe because the awaited
    ``registry.resolve_shift()`` above always warms that cache first. That was WRONG and
    review caught it: ``resolve_shift`` wraps each source in ``asyncio.wait_for(...,
    DEFAULT_POLL_TIMEOUT_SECS)``, and a timeout cancels the AWAITING coroutine while the
    ``to_thread`` worker keeps running -- so the poll can give up with the cache still unset,
    and ``describe()`` then pays the full spawn on the loop. "Something upstream probably
    warmed it" is not a guarantee.

    One of the three sites was subtler than the other two:
    ``to_thread(handover.build, providers, rotation.describe(shift))`` moved *build* off the
    loop while still evaluating ``describe()`` on it, because arguments are computed before
    the call.
    """

    def test_no_async_function_evaluates_tier_states_on_the_loop(self):
        """`tier_states` reaches the same `gh api user` spawn as `describe`, and two ASYNC
        callers evaluated it inline: `dispatch.run_cycle` (the 120s heartbeat) and
        `rotation.apply_tiers` (awaited from the default-enabled 300s rotation-check cron via
        `POST /rotation/arm`). Same 10s frozen-loop exposure `describe` had; found in review.

        `rotation.describe` is deliberately NOT flagged: it is SYNC and its callers already
        offload it (pinned by the sibling test above). This walks only `async def`s, so a sync
        helper computing `tier_states` for a caller to offload does not trip it.
        """
        import ast
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation

        offenders = []
        for mod in (dispatch, rotation):
            tree = ast.parse(inspect.getsource(mod))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                nested = {
                    id(n)
                    for inner in ast.walk(fn)
                    if inner is not fn
                    and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for n in ast.walk(inner)
                }
                for node in ast.walk(fn):
                    if id(node) in nested or not isinstance(node, ast.Call):
                        continue
                    fname = node.func.attr if isinstance(node.func, ast.Attribute) else (
                        node.func.id if isinstance(node.func, ast.Name) else ""
                    )
                    if fname != "tier_states":
                        continue
                    # An `await asyncio.to_thread(tier_states, ...)` passes tier_states as an
                    # ARG, so the offending shape is tier_states being CALLED (it has args that
                    # are the shift, i.e. it is `tier_states(shift)`), not named.
                    if node.args or node.keywords:
                        offenders.append(f"{mod.__name__}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "tier_states() can spawn `gh api user`; every async caller must "
            f"`await asyncio.to_thread(tier_states, shift)`. Inline at: {offenders}",
        )

    def test_no_handler_evaluates_describe_on_the_loop(self):
        import inspect
        import re

        source = inspect.getsource(routes)
        # Code only: the comments explaining the fix necessarily quote the broken shape.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        inline = [
            line.strip()
            for line in code.splitlines()
            if "rotation.describe" in line
            and not re.search(r"to_thread\(\s*rotation\.describe", line)
        ]
        self.assertEqual(
            inline,
            [],
            "every rotation.describe() call must go through asyncio.to_thread -- it can "
            f"spawn `gh api user` on a cold cache. Inline: {inline}",
        )

    async def test_a_slow_identity_lookup_does_not_stall_the_state_handler(self):
        """Behavioural: the loop must keep running while /state resolves the rotation."""
        import asyncio
        import time

        from aiohttp.test_utils import TestClient, TestServer

        blocking_secs = 0.3

        def _slow_describe(_shift):
            time.sleep(blocking_secs)
            return {"mode": "observe", "primary": False}

        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(blocking_secs / 10)
                ticks += 1

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "describe", _slow_describe):
                async with TestClient(TestServer(app)) as client:
                    beat = asyncio.create_task(_heartbeat())
                    try:
                        resp = await client.get("/api/apps/ops-mission-control/rotation")
                        self.assertEqual(resp.status, 200)
                    finally:
                        beat.cancel()

        self.assertGreaterEqual(
            ticks,
            3,
            "the event loop stalled while resolving the rotation -- describe() must run "
            f"off-loop (only {ticks} tick(s) in {blocking_secs}s)",
        )


class TestStatePollDoesNotBlockOnEntryPointDiscovery(unittest.IsolatedAsyncioTestCase):
    """`/state` is POLLED, so anything slow in it stalls the loop on every poll.

    `companion.companion_summary()` walks `importlib.metadata.entry_points()`, which
    enumerates every installed distribution's metadata from disk. Measured in this
    environment at ~9.3ms against ~0.03-0.05ms for the sibling reads in the same handler
    (`store.counts_by_status`, `ledger.stats`) -- roughly 200x, and that is with a warm page
    cache and a small site-packages. Found in review.

    The sibling reads are deliberately left inline: they touch one small per-instance JSON
    file each, and a thread hop would cost more than it saves. The measurement is what
    decided that, not a guess.
    """

    def test_companion_discovery_is_off_the_loop(self):
        import inspect
        import re

        source = inspect.getsource(routes._handle_state)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        inline = [
            line.strip()
            for line in code.splitlines()
            if "companion_summary" in line
            and not re.search(r"to_thread\(\s*companion\.companion_summary", line)
        ]
        self.assertEqual(
            inline,
            [],
            f"companion_summary() scans installed distributions; keep it off-loop: {inline}",
        )

    async def test_a_slow_entry_point_scan_does_not_stall_the_loop(self):
        import asyncio
        import time

        from aiohttp.test_utils import TestClient, TestServer

        blocking_secs = 0.3

        def _slow_summary():
            time.sleep(blocking_secs)
            return []

        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(blocking_secs / 10)
                ticks += 1

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.companion, "companion_summary", _slow_summary):
                async with TestClient(TestServer(app)) as client:
                    beat = asyncio.create_task(_heartbeat())
                    try:
                        resp = await client.get("/api/apps/ops-mission-control/state")
                        self.assertEqual(resp.status, 200)
                    finally:
                        beat.cancel()

        self.assertGreaterEqual(
            ticks,
            3,
            "the event loop stalled during /state -- companion discovery must run off-loop "
            f"(only {ticks} tick(s) in {blocking_secs}s)",
        )


class TestOutboundNotesAreRedacted(unittest.IsolatedAsyncioTestCase):
    """An action note is published on SOMEONE ELSE'S system, where we cannot unpublish it.

    `/incident/action` and the approved-proposal path forward the note verbatim into an
    acknowledgement comment, a resolve reason or a mute note at the provider. The note is
    agent- or operator-authored free text, so an agent that pasted a provider token into its
    diagnosis published that token into the provider's own comment thread. Found in review.

    The Slack sink and the ledger write path already had this floor; this was the third
    outbound surface and the one without it.
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

    async def test_a_token_in_the_note_never_reaches_the_sink(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="webhook", native_id="probe-1", title="latency"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None

        token = "abcdef0123456789abcdef0123456789abcdef01"  # Datadog app-key shape
        seen: list[dict] = []

        class _Recorder:
            id = "webhook"
            display_name = "Webhook"

            def configured(self):
                return True

            def supported_actions(self):
                return frozenset(models.VALID_ACTIONS)

            async def execute(self, signal, action, payload):
                seen.append(dict(payload))
                from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (  # noqa: E501
                    ActionResult,
                )

                return ActionResult(ok=True, action=action, detail="done", error="")

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.rotation, "authorize_action", return_value=(True, "granted by rule")
            ):
                with mock.patch.object(
                    routes.get_registry(), "action_sink", return_value=_Recorder()
                ):
                    async with TestClient(TestServer(app)) as client:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/incident/action",
                            json={
                                "id": inc.incident_id,
                                "action": "comment",
                                "sink": "webhook",
                                "note": f"root cause: dd key {token}",
                            },
                        )
                        self.assertEqual(resp.status, 200)

        self.assertEqual(len(seen), 1, "the sink must have been called")
        self.assertNotIn(token, seen[0].get("note", ""), "a token must never leave in a note")

    def test_redaction_happens_before_the_length_clip(self):
        """Clipping first could sever a token so the pattern no longer matches."""
        import inspect

        source = inspect.getsource(routes._handle_action)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        note_lines = [ln.strip() for ln in code.splitlines() if "_MAX_NOTE_LEN" in ln]
        self.assertTrue(note_lines)
        for line in note_lines:
            self.assertIn("_safe_outbound", line, f"redact before clipping: {line}")
            self.assertLess(
                line.index("_safe_outbound"),
                line.index("_MAX_NOTE_LEN"),
                f"the redaction must wrap the value, not the clipped result: {line}",
            )

    def test_no_outbound_note_bypasses_the_floor(self):
        """Guards the class, not just the two sites — the URL slip taught that lesson."""
        import inspect
        import re

        source = inspect.getsource(routes)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        raw = [
            ln.strip()
            for ln in code.splitlines()
            if re.search(r'(body|proposal)\.get\(\s*"note"', ln) and "_safe_outbound" not in ln
        ]
        self.assertEqual(
            raw,
            [],
            f"every note heading to a provider must pass _safe_outbound(): {raw}",
        )


class TestACredentialBearingRemoteIsRefused(unittest.IsolatedAsyncioTestCase):
    """`data/config.json` is served over `/api/apps/<name>/config` WITHOUT session auth.

    `redact_tokens` has no pattern for a PAT embedded in a URL, so
    `https://user:ghp_xxx@github.com/org/repo.git` pasted into the ledger-sync remote was
    persisted verbatim into a world-readable file and echoed into SEL output. The frontend's
    `displayRemote()` strips userinfo for DISPLAY only — and its own docstring said so
    outright, calling the stored value out as still exposed. A documented hole rather than a
    fixed one; review blocked on it, correctly.

    Refused rather than silently stripped: the pasted token is compromised either way, and a
    remote quietly rewritten to an unauthenticated URL would fail to push later with no hint
    why.
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

    async def test_a_token_remote_is_rejected_and_never_stored(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        token = "ghp_ThisIsTheActualTokenValue"
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/apps/ops-mission-control/settings",
                    json={"ledger_sync_remote": f"https://u:{token}@github.com/org/repo.git"},
                )
                self.assertEqual(resp.status, 400)
                body = await resp.json()

        self.assertEqual(body["code"], "remote_has_credentials")
        # The refusal must not echo the token back either.
        self.assertNotIn(token, json.dumps(body))
        # ...and nothing was persisted.
        self.assertNotIn(token, json.dumps(read_config()))

    async def test_an_ordinary_remote_still_saves(self):
        """The guard must not break the feature it protects."""
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                for remote in (
                    "https://github.com/org/repo.git",
                    "git@github.com:org/repo.git",  # scp-style, the commonest form
                    "ssh://git@github.com/org/repo.git",
                ):
                    resp = await client.put(
                        "/api/apps/ops-mission-control/settings",
                        json={"ledger_sync_remote": remote},
                    )
                    self.assertEqual(resp.status, 200, f"{remote} must be accepted")

    def test_userinfo_detection_covers_the_shapes_that_matter(self):
        """ANY userinfo on http(s) is refused; scp-style and `ssh://git@` still work.

        The row that matters most here is `https://ghp_xxx@github.com/...` — a token in the
        USERNAME position with no password. An earlier version of this test asserted that as
        ACCEPTABLE, on the reasoning that a bare `user@` is a username rather than a secret.
        That is GitHub's own documented token-remote form, so the likeliest way an operator
        pastes a credential was the one shape the guard let through, blessed by a test. Review
        caught it.

        On http(s) there is no userinfo worth storing in a world-readable file: a real
        username comes from a credential helper, and anything else is a secret.
        """
        for remote, refuse in (
            ("https://user:ghp_secret@github.com/org/repo.git", True),
            ("https://u:p@host:8443/r.git", True),
            # GitHub's documented PAT form: token as username, NO password.
            ("https://ghp_secret@github.com/org/repo.git", True),
            ("https://github_pat_11ABC@github.com/org/repo.git", True),
            ("git@github.com:org/repo.git", False),  # no `://`, so no userinfo component
            ("ssh://git@github.com/org/repo.git", False),  # key auth; username is not a secret
            ("https://github.com/org/repo.git", False),
            ("https://github.com/org/re@po.git", False),  # `@` in the PATH, not the authority
        ):
            self.assertEqual(
                routes._url_has_userinfo(remote), refuse, f"wrong verdict for {remote!r}"
            )


class TestOnlyOneApprovalCanWin(unittest.IsolatedAsyncioTestCase):
    """Moving a proposal out of `pending` must be a compare-and-set.

    `decide_proposal` read the incident, tested `state == pending`, then wrote through
    `update_fields` — three separate index accesses with no lock held across them. Two
    approvals arriving together (a double-click, a retried request, two operators, Slack plus
    the dashboard) both observed `pending`, both were told `ok`, and the caller executed the
    provider action TWICE. Acking twice is untidy; resolving or silencing twice is a
    duplicated write on someone else's production tooling, and a second `silence` re-arms a
    window the first had already bounded. Found in review.

    Now the whole read-check-write runs under `_IndexLock` — the same compare-and-set `claim`
    uses, for the same "exactly one winner" reason.
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

    def _proposed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="webhook", native_id="probe-1", title="latency"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        store.propose_action(inc.incident_id, action="comment", sink="webhook", note="do it")
        stored = store.get_incident(inc.incident_id)
        assert stored is not None and stored.proposed_action is not None
        return inc.incident_id, str(stored.proposed_action["digest"])

    def test_a_second_approval_is_refused_not_re_executed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        incident_id, digest = self._proposed()
        first = store.decide_proposal(incident_id, approve=True, digest=digest)
        second = store.decide_proposal(incident_id, approve=True, digest=digest)

        self.assertTrue(first["ok"], "the first approval must win")
        self.assertFalse(second["ok"], "the second must be refused, not executed again")
        self.assertIn("already", second["reason"])

    def test_the_check_and_the_write_happen_under_one_lock(self):
        """Structural, and the honest test to write here — see below.

        I could not build a harness that makes two approvals both win against the PRE-FIX
        code, and tried several: pausing on `utc_now_iso`, on `get_incident`, on
        `_read_index_unlocked`, and on `update_fields`, plus barriers. Every attempt ended
        with one winner. The reason is that the old write path went through
        `update_fields` -> `transition`, which takes `_IndexLock` and RE-READS the index —
        so the two decisions were already serialised at the write, and whichever ran second
        found `approved` on its own state check.

        So the window is narrower than the review described: not "both execute the provider
        action", but "both pass the state check, and the second write lands on top of the
        first" — because `transition` re-reads the index and then overwrites
        `proposed_action` UNCONDITIONALLY, without re-checking the proposal state it was
        handed. Reachable, but only if a caller is descheduled for the whole duration of
        another caller's complete decision.

        Rather than ship a concurrency test that passes against the bug it names — which is
        worse than no test, and which I nearly did here — this asserts the property directly:
        the state check and the write are inside ONE `_IndexLock` block, so no interleaving
        can exist to reproduce. The fix stands on being the same compare-and-set `claim`
        already uses, not on a demo of the race.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        source = inspect.getsource(store.decide_proposal)
        lock_at = source.find("with _IndexLock():")
        check_at = source.find("PROPOSAL_PENDING")
        self.assertGreater(lock_at, -1, "decide_proposal must hold _IndexLock")
        self.assertGreater(check_at, lock_at, "the state check must be INSIDE the lock")

        # The write must not go back through `update_fields`: that re-enters the lock (and
        # re-reads the index), which is what let the old code overwrite a decision made by
        # a caller that had already committed.
        decided = inspect.getsource(store._decide_locked)
        self.assertNotIn(
            "update_fields(",
            decided,
            "the locked write must use _write_index_unlocked, not re-enter the lock",
        )
        self.assertIn("_write_index_unlocked", decided)

    def test_the_decision_is_persisted_by_the_locked_write(self):
        """The lock body writes the index directly, so verify it actually landed."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        incident_id, digest = self._proposed()
        store.decide_proposal(incident_id, approve=True, digest=digest)
        stored = store.get_incident(incident_id)
        assert stored is not None and stored.proposed_action is not None
        self.assertEqual(stored.proposed_action["state"], models.PROPOSAL_APPROVED)
        self.assertTrue(stored.proposed_action["decided_at"])


class TestNoHandlerParsesAStoredFileOnTheLoop(unittest.IsolatedAsyncioTestCase):
    """Every store/ledger read in `routes.py` must go through `asyncio.to_thread`.

    These helpers parse a JSON/JSONL file whose size grows with use, and they sit on POLLED
    endpoints. Measured in this environment:

      ledger.stats()             0.1ms @0    1.8ms @100    13ms @1k    93ms @5k   275ms @20k
      store.counts_by_status()   0.1ms @0    4.3ms @100    42ms @1k   188ms @5k
      store.open_incidents()     0.2ms @0    2.3ms @100    70ms @1k   251ms @5k

    A previous round measured `ledger.stats()` and `store.counts_by_status()` at ~0.03ms and
    left them inline as negligible. That measurement was taken against an EMPTY store and the
    conclusion generalised from it — the one input where the cost is zero by construction.
    Review caught `ledger.stats`; auditing for the class then found `counts_by_status`,
    `open_incidents` (called TWICE inline in `/state`), and inline index parses in
    `/handover`, `/incidents` and `/signals`.

    Asserted as a class rather than per call site, because the per-site version of this
    lesson has now been learned twice.
    """

    #: Helpers that read and parse a file whose size grows with use, split for the AST walk.
    #:
    #: `get_incident`/`find_by_signal` look like cheap single-record lookups and are not:
    #: both are `read_index().get(...)`, so each pays the FULL index parse. They were missing
    #: from the first version of this list, and review then found six inline calls across the
    #: incident, transition, action and proposal handlers. `read_log` reads a per-incident
    #: postmortem file. If a helper touches disk, it belongs here — "it only fetches one row"
    #: is about the return value, not about the work.
    FILE_PARSING_MODULES = frozenset({"store", "ledger"})
    FILE_PARSING_ATTRS = frozenset(
        {
            "open_incidents",
            "counts_by_status",
            "read_index",
            "recent_incidents",
            "get_incident",
            "find_by_signal",
            "read_log",
            "stats",
            "read_entries",
            # WRITES, not just reads. The first version of this list held only readers, and
            # review then found `store.update_fields` called inline in `slack_out.publish` —
            # a full read-modify-write of the incident index, on a coroutine `run_cycle`
            # awaits. A write parses the same file a read does and then rewrites it, so it is
            # strictly worse; "reads a growing file" was the wrong frame for the list.
            "update_fields",
            "transition",
            "claim",
            "decide_proposal",
        }
    )
    #: Deliberately NOT listed: `cron_service.list_jobs()`. It looks like exactly the shape
    #: this guard hunts and is not — `CronService.list_jobs` is documented cache-only (no
    #: lock open, no read, no hash) precisely so the dashboard WebSocket push and the Slack
    #: handlers can call it on the loop. Adding it here would flag correct code across the
    #: repo. `rotation.apply_tiers` still uses `list_jobs_async`, for FRESHNESS rather than
    #: for blocking: the snapshot only refreshes once per timer poll, so a pause made from
    #: the CLI could otherwise read as active and get "resumed".
    FILE_PARSING_ANY_RECEIVER: frozenset[str] = frozenset()
    #: Module-LOCAL helpers that read/scan a whole file. Called bare (`_helper()`), not as
    #: `module.attr()`, so the attribute walk above cannot see them — which is exactly how
    #: `ledger_sync._credential_bearing_lines` (a full read + regex scan of `ledger.jsonl`,
    #: called inline from `async def push`) reached review unnoticed after two rounds of
    #: widening this guard. Bare names are matched separately.
    #:
    #: The conflict probes joined after review found all four of them called bare inside
    #: `async def pull`/`push`/`_resolve_schedule_conflict`. They read like predicates, which is
    #: what hid them: `has_conflict()` reads the WHOLE ledger and scans every line for markers,
    #: and `resolve_conflict()` re-parses it and REWRITES it — on the one file in this app that
    #: grows without bound, from coroutines the heartbeat awaits. Same lesson as
    #: `_credential_bearing_lines` one round earlier: a bare name is invisible to the attribute
    #: walk, so every module-local disk toucher has to be listed by hand.
    FILE_PARSING_LOCALS = frozenset(
        {
            "_credential_bearing_lines",
            "has_conflict",
            "resolve_conflict",
            "schedule_has_conflict",
        }
    )
    #: Dotted form, for the `to_thread(f, g())`-shape check which is line-based by nature.
    FILE_PARSING = (
        "store.open_incidents",
        "store.counts_by_status",
        "store.read_index",
        "ledger.stats",
        "ledger.read_entries",
    )

    def test_no_async_function_parses_a_stored_file_inline(self):
        """AST over the WHOLE backend package, not a regex over one module.

        The first version of this guard was a line-based scan of `routes.py` only. Review then
        found `store.read_index()` inline in `dispatch.run_cycle` — a coroutine one module
        over, in the exact class this test was written to close. Two lessons, both applied
        here: match the LANGUAGE (an `ast` walk knows what is inside an `async def` and what
        is merely a call passed to `to_thread`, where a regex is guessing), and scope to the
        package rather than to the file where the bug happened to be found.
        """
        import ast
        import pathlib

        backend = pathlib.Path(routes.__file__).parent
        offenders = []
        for path in sorted(backend.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr in self.FILE_PARSING_ATTRS
                        and isinstance(func.value, ast.Name)
                        and func.value.id in self.FILE_PARSING_MODULES
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} "
                            f"{func.value.id}.{func.attr}() inside async {fn.name}()"
                        )
                    # Any receiver: `cron_service.list_jobs()` parses the cron store but the
                    # receiver is a parameter, so the module-scoped branch above is blind to it.
                    elif (
                        isinstance(func, ast.Attribute)
                        and func.attr in self.FILE_PARSING_ANY_RECEIVER
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} .{func.attr}() inside async {fn.name}()"
                        )
                    # Bare local calls: `_credential_bearing_lines()` is a whole-file read plus
                    # a regex sweep, and no `module.attr` shape makes it visible above.
                    elif isinstance(func, ast.Name) and func.id in self.FILE_PARSING_LOCALS:
                        offenders.append(
                            f"{path.name}:{node.lineno} {func.id}() inside async {fn.name}()"
                        )
                # Synchronous `Path.read_text`/`write_text` DIRECTLY in the coroutine body —
                # `ledger_sync._ensure_repo` did this to a `.gitignore` on a path awaited
                # straight off the loop (`sync_safely`), the same class the module/local
                # checks above cover for the store parses. Skipped inside a nested `def`,
                # because that is precisely the fix (wrap the I/O and hand the callable to
                # `to_thread`) — flagging it there would forbid the remedy.
                nested_ids = {
                    id(n)
                    for inner in ast.walk(fn)
                    if inner is not fn and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for n in ast.walk(inner)
                }
                for node in ast.walk(fn):
                    if id(node) in nested_ids:
                        continue
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"read_text", "write_text"}
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} .{node.func.attr}() "
                            f"inside async {fn.name}()"
                        )
        self.assertEqual(
            offenders,
            [],
            "these parse a growing file ON the event loop — pass them to asyncio.to_thread "
            "instead of calling them:\n  " + "\n  ".join(offenders),
        )

    def test_no_slow_call_is_evaluated_as_a_to_thread_argument(self):
        """`to_thread(f, g())` moves `f` off-loop and runs `g` ON it.

        This shape shipped once — `to_thread(handover.build, providers,
        rotation.describe(shift))` — and reads as fixed at a glance, which is why it needs
        its own check.
        """
        import inspect
        import re

        source = inspect.getsource(routes)
        offenders = []
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith("#") or "to_thread(" not in line:
                continue
            args = line.split("to_thread(", 1)[1]
            # A `(` after the first argument name means a call is being evaluated inline.
            for helper in (*self.FILE_PARSING, "rotation.describe", "companion.companion_summary"):
                if re.search(rf"{re.escape(helper)}\s*\(", args):
                    offenders.append(f"{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [], "pass the callable to to_thread, do not call it:\n  " + "\n  ".join(offenders)
        )

    #: Helpers that reach LOOP-OWNED objects on `DashboardState` — the slot dicts, the
    #: Slack reverse index, or the notification bus (whose sink ends in `_broadcast`:
    #: `asyncio.Queue.put_nowait` per SSE client plus `asyncio.Event.set()`).
    LOOP_OWNED_STATE_HELPERS = (
        "slack_out.link_thread_to_investigation",
        "notify_out.notify_needs_human",
        "notify_out.notify_source_unhealthy",
        "notify_out.notify_work_released",
    )

    def test_no_loop_owned_state_helper_is_pushed_off_the_loop(self):
        """The INVERSE of the guard above, and the reason both are needed.

        `to_thread` is the right answer for file I/O and the wrong answer for loop-owned
        state. These helpers mutate `DashboardState` and broadcast to SSE clients through
        `asyncio` primitives that are only safe on the loop's own thread — `Event.set()`
        resolves waiter futures via `loop.call_soon`, whose contract is loop-thread-only
        (`call_soon_threadsafe` is the cross-thread door).

        The failure mode is what makes a test necessary rather than a comment: off the loop
        it *appears* to work, because the waiter future is marked done synchronously and the
        loop notices on its next poll. Nothing fails, so only a structural check keeps a
        future "wrap the blocking call" sweep from quietly reintroducing it — the same sweep
        that correctly wrapped every store parse on this path is what put it here. Found in
        review (GPT 5.6).

        Fails against the pre-fix source, which had both helpers inside `to_thread(...)`.
        """
        import inspect
        import re

        source = inspect.getsource(routes)
        offenders = []
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith("#") or "to_thread(" not in line:
                continue
            for helper in self.LOOP_OWNED_STATE_HELPERS:
                if re.search(rf"\b{re.escape(helper)}\b", line):
                    offenders.append(f"{lineno}: {line.strip()}")
        # The call may also sit on a continuation line, so scan the whole statement.
        for helper in self.LOOP_OWNED_STATE_HELPERS:
            for match in re.finditer(
                rf"to_thread\(\s*\n?\s*{re.escape(helper)}\b", source, re.MULTILINE
            ):
                offenders.append(f"{source[: match.start()].count(chr(10)) + 1}: {helper}")
        self.assertEqual(
            sorted(set(offenders)),
            [],
            "these mutate loop-owned DashboardState and broadcast through asyncio "
            "primitives — call them directly on the loop, not via to_thread:\n  "
            + "\n  ".join(sorted(set(offenders))),
        )


class TestManualClaimResolvesTheSignalServerSide(unittest.IsolatedAsyncioTestCase):
    """A manual claim must authorize against the PROVIDER's signal, not the caller's.

    `/incident/claim` took a fully caller-supplied `Signal` and `resolve_mode` matched
    act-rules on its `source`/`resource`/`labels`. A caller who controls the whole object can
    pair a resource an operator's rule authorizes (`resource="prod-db-1"` matching
    `resource_glob="prod-*"`) with a DIFFERENT provider target in `labels` — the resource
    passes the gate while another field drives the downstream sink, so the authorization
    describes a signal that does not exist. Found in review.

    The route now polls and takes the server's own signal with the claimed id, refusing the
    claim if that id is not currently firing.
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

    async def test_a_fabricated_signal_is_refused_when_not_firing(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            # Provider reports NOTHING firing.
            with mock.patch.object(
                routes.get_registry(), "poll_all", mock.AsyncMock(return_value=([], {}))
            ):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.post(
                        "/api/apps/ops-mission-control/incident/claim",
                        json={
                            "signal": {
                                "id": "forged-1",
                                "source": "cloudwatch",
                                "resource": "prod-db-1",  # would match an operator rule
                                "labels": {"dd_monitor_id": "victim"},  # but drives another sink
                            }
                        },
                    )
                    self.assertEqual(resp.status, 409)
                    body = await resp.json()
        self.assertEqual(body["code"], "signal_not_firing")

    async def test_the_servers_own_signal_is_used_not_the_callers(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        # What the provider ACTUALLY reports for this id.
        real = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/real",
            title="real alarm",
            labels={"alarm_name": "real"},
        )
        captured = {}
        real_claim = store.claim  # bind before patching, or the helper recurses into the mock

        def _capture_claim(signal, **kw):
            captured["signal"] = signal
            return real_claim(signal, **kw)

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.get_registry(), "poll_all", mock.AsyncMock(return_value=([real], {}))
            ):
                with mock.patch.object(routes.store, "claim", _capture_claim):
                    async with TestClient(TestServer(app)) as client:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/incident/claim",
                            # Caller sends the right id but LIES about everything else.
                            json={
                                "signal": {
                                    "id": real.id,
                                    "source": "cloudwatch",
                                    "resource": "attacker-chosen",
                                    "labels": {"dd_monitor_id": "victim"},
                                }
                            },
                        )
                        self.assertEqual(resp.status, 200)

        used = captured["signal"]
        self.assertEqual(used.resource, real.resource, "must use the provider's resource")
        self.assertEqual(used.labels, real.labels, "must use the provider's labels")
        self.assertNotIn("dd_monitor_id", used.labels, "the caller's forged label is discarded")

    def test_the_route_does_not_authorize_against_the_request_body(self):
        """Structural: the claimed signal must come from poll_all, not Signal.from_dict."""
        import inspect

        source = inspect.getsource(routes._handle_claim)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn(
            "Signal.from_dict",
            code,
            "the claim must be resolved from the provider poll, not built from the body",
        )
        self.assertIn("poll_all", code)


class TestManualClaimAcksThePushSpool(unittest.IsolatedAsyncioTestCase):
    """A hand-claimed webhook signal must leave the bounded spool.

    `dispatch.run_cycle` acks what IT claims, and `POST /incident/claim` is the SECOND place a
    claim becomes durable. Without an ack there the signal stayed spooled forever, and on a
    full (200-entry) spool the next signed delivery evicted the OLDEST unclaimed entry to make
    room for it — a real alert lost to a duplicate nobody needed. A direct consequence of
    moving consumption off `poll()`; the old `drain()` covered this path by accident. Found in
    review.
    """

    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        webhook.reset_spool()

    def tearDown(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        webhook.reset_spool()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_claiming_a_webhook_signal_removes_it_from_the_spool(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        spooled = models.Signal.create(
            source="webhook", native_id="alert/1", title="queue backed up"
        )
        other = models.Signal.create(source="webhook", native_id="alert/2", title="disk full")
        webhook._queue.extend([spooled, other])

        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": object()}
        request.json = mock.AsyncMock(return_value={"signal": {"id": spooled.id}})

        # The route re-polls to resolve the signal server-side (it must not trust the body),
        # so the registry has to report it as firing.
        async def _poll_all():
            return [spooled, other], {}

        with mock.patch.object(routes, "get_registry") as get_registry:
            get_registry.return_value = mock.MagicMock(poll_all=_poll_all)
            with mock.patch.object(routes.rotation, "resolve_mode", return_value="observe"):
                with mock.patch.object(routes.slack_out, "publish", mock.AsyncMock()):
                    with mock.patch.object(routes.dispatch, "gather_evidence_safely",
                                           mock.AsyncMock(return_value=[])):
                        with mock.patch.object(routes, "is_app_enabled", return_value=True):
                            response = await routes._handle_claim(request)

        self.assertEqual(response.status, 200)
        remaining = {s.id for s in webhook.peek()}
        self.assertNotIn(
            spooled.id, remaining, "a hand-claimed signal stayed spooled and can evict others"
        )
        # The one nobody claimed is untouched — ack is per-id, not a drain.
        self.assertIn(other.id, remaining)


class TestTheSlotKeyIsDerivedNotTrusted(unittest.TestCase):
    """The chat-slot key is computed, not read back from the record.

    The dispatch cron prompt said the key must be "EXACTLY
    `ops-mission-control-<incident_id>`" and that sentence was the only thing enforcing it —
    the same objection this change makes about tier arming, one path over. A misfollowed turn
    produced an incident whose panel silently showed nothing. Raised in design review.
    """

    def test_the_key_matches_the_frontend_derivation(self):
        """Both sides must agree or the panel polls a key nothing writes."""
        from pathlib import Path as _Path

        key = routes.canonical_slot_key("INV-7")
        self.assertEqual(key, "ops-mission-control-INV-7")

        # Pinned against the frontend's own expression rather than restating it: if
        # `incidentSlotKey` changes shape, this fails instead of drifting silently.
        web_src = (
            _Path(routes.__file__).resolve().parents[5]
            / "website/src/apps/ops-mission-control/IncidentChat.tsx"
        )
        if web_src.is_file():
            self.assertIn(
                "`ops-mission-control-${incidentId}`",
                web_src.read_text(encoding="utf-8"),
            )

    def test_a_wrong_stored_key_is_ignored(self):
        """Structural: no resolution path may read `slot_key` off the record.

        A behavioural test would need a live slot registry; what actually matters is that the
        field is never consulted, so that is asserted directly.
        """
        import inspect

        source = inspect.getsource(routes)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn(
            "inc.slot_key",
            code,
            "a resolution path reads the agent-reported slot_key instead of deriving it",
        )
        self.assertNotIn("incident.slot_key", code)


class TestManualClaimRequiresAFiringSignal(unittest.IsolatedAsyncioTestCase):
    """`POST /incident/claim` must refuse a signal that is no longer firing.

    `poll_all` returns EVERY state — firing, ok and suppressed — and this handler matched on
    id alone. The local was even named `firing`, which is what hid it: a signal that recovered
    between the board's poll and this one came back as `ok`, matched, and minted an incident
    for a fault that had already cleared. The two other `poll_all` consumers
    (`dispatch.run_cycle`, `GET /signals`) both filter explicitly. Found in review.
    """

    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _claim(self, signal):
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": object()}
        request.json = mock.AsyncMock(return_value={"signal": {"id": signal.id}})

        async def _poll_all():
            return [signal], {}

        with mock.patch.object(routes, "get_registry") as get_registry:
            get_registry.return_value = mock.MagicMock(poll_all=_poll_all)
            with mock.patch.object(routes.rotation, "resolve_mode", return_value="observe"):
                with mock.patch.object(routes.slack_out, "publish", mock.AsyncMock()):
                    with mock.patch.object(
                        routes.dispatch, "gather_evidence_safely", mock.AsyncMock(return_value=[])
                    ):
                        with mock.patch.object(routes, "is_app_enabled", return_value=True):
                            return await routes._handle_claim(request)

    async def test_a_recovered_signal_is_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        recovered = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/dlq",
            title="DLQ deep",
            state=models.STATE_OK,
        )
        response = await self._claim(recovered)
        self.assertEqual(response.status, 409)
        self.assertIn("signal_not_firing", getattr(response, "text", ""))

    async def test_a_suppressed_signal_is_refused(self):
        """Somebody parked it at the provider — claiming is what they asked not to happen."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        parked = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/dlq",
            title="DLQ deep",
            state=models.STATE_SUPPRESSED,
        )
        response = await self._claim(parked)
        self.assertEqual(response.status, 409)

    async def test_a_firing_signal_is_still_claimable(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        live = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/dlq",
            title="DLQ deep",
            state=models.STATE_FIRING,
        )
        response = await self._claim(live)
        self.assertEqual(response.status, 200)


class TestApprovedProposalSchedulesVerification(unittest.IsolatedAsyncioTestCase):
    """An approved proposal executes a real write, so it must arm the recheck too.

    `_handle_action` records `last_action`/`last_action_at` and calls `_schedule_verification`
    after a real success; `_execute_stored_proposal` executed the SAME write through
    `_execute_authorized` and did neither. A resolve or silence approved from the queue
    therefore went out to the provider with `last_action` left empty and no recheck scheduled —
    the incident record and postmortem showed a write that "never happened". Found in review.
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

    async def _run(self, action, *, simulated, suppressed_secs=0, native_id="p-1"):
        # Both halves of the ceiling: app mode `act` AND a rule matching this signal.
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            policy_store,
            rotation,
            store,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ActionResult,
        )

        # Both halves of the ceiling: app mode `act` AND a rule matching this signal.
        # `checkout*`, not `*`: an all-wildcard glob is the blanket grant the gate refuses.
        rotation.save_rules([{"source": "webhook", "mode": "act", "resource_glob": "checkout*"}])
        policy_store.set_mode(models.MODE_ACT)

        inc = store.claim(
            models.Signal.create(
                source="webhook",
                native_id=native_id,
                title="checkout 5xx",
                resource="checkout",
            ),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        proposal = {"action": action, "sink": "webhook", "note": "x"}

        class _Sink:
            id = "webhook"
            display_name = "Webhook"

            def configured(self):
                return True

            def supported_actions(self):
                return set(models.VALID_ACTIONS)

            async def execute(self, *a, **k):
                return ActionResult(
                    ok=True,
                    action=action,
                    simulated=simulated,
                    suppressed_secs=suppressed_secs,
                )

        # A FRESH registry, not `get_registry()`. Registration is ADD-only (the incumbent
        # wins), so whatever already holds the `webhook` sink id keeps it and this fake is
        # silently dropped — which is why this passed alone and failed inside the file: an
        # earlier test had left its own `webhook` fake on the process-wide registry.
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        reg = registry.OpsProviderRegistry()
        registry._registry = reg
        self.addCleanup(registry.reset_registry)
        reg.register_action_sink(_Sink())

        # Mint a real permit through the sole minter, exactly as the decide handler does.
        permit, reason = await routes._authorize(inc.signal, action)
        assert permit is not None, reason
        result = await routes._execute_stored_proposal(inc, proposal, permit)
        return inc.incident_id, result

    async def test_a_real_resolve_records_last_action_and_schedules_a_recheck(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        iid, result = await self._run(models.ACTION_RESOLVE, simulated=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verification"], models.VERIFY_PENDING)
        self.assertTrue(result["verify_after"])
        stored = store.get_incident(iid)
        assert stored is not None
        self.assertEqual(stored.last_action, models.ACTION_RESOLVE)
        self.assertIn(stored.verification, models.OPEN_VERIFICATIONS)

    async def test_an_approved_resolve_honours_the_sinks_reported_suppression(self):
        """The sink's window must win on THIS path too, not just on `/incident/action`.

        Datadog aliases `resolve` onto a bounded mute and only `EXPIRING_ACTIONS` (i.e.
        `silence`) receives a `duration_secs` from the route, so reading the payload alone
        scheduled a five-minute recheck against a four-hour suppression and charged a false
        miss to the cited ledger entries. The fix landed on the direct-action path and missed
        this one — the two paths converge on `_execute_authorized` for the WRITE, so their
        follow-up has to converge too. Found in review (GPT 5.6).

        Fails against a handler that reads only `payload.get("duration_secs")`.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        # Distinct `native_id`s: `store.claim` dedups by signal, so reusing one returns None.
        _, plain = await self._run(models.ACTION_RESOLVE, simulated=False, native_id="p-plain")
        _, muted = await self._run(
            models.ACTION_RESOLVE,
            simulated=False,
            suppressed_secs=models.DEFAULT_SILENCE_SECS,
            native_id="p-muted",
        )
        self.assertTrue(plain["verify_after"] and muted["verify_after"])
        self.assertGreater(
            muted["verify_after"],
            plain["verify_after"],
            "a resolve that suppressed for hours was rechecked on the flat default",
        )

    def test_both_execution_paths_schedule_verification_identically(self):
        """Pins the CONVERGENCE, not just today's argument.

        This is the second time these two paths drifted (the first: the approved path did not
        arm verification at all), so the property worth asserting is that both pass the same
        expression to `_schedule_verification` — a future edit to one has to touch the other or
        this fails. Cheaper than discovering the drift from a false ledger miss.
        """
        import inspect
        import re

        source = inspect.getsource(routes)
        calls = re.findall(r"_schedule_verification,\s*(.*?)\)", source, re.DOTALL)
        # The `def` line has no `incident` argument name; keep only the two call sites.
        arg_blobs = [c for c in calls if "incident" in c]
        self.assertEqual(len(arg_blobs), 2, f"expected 2 call sites, found {len(arg_blobs)}")
        # Both must derive the window the same way; compare the duration ARGUMENT specifically.
        durations = {"".join(blob.split()).split(",")[-1].rstrip(",") for blob in arg_blobs}
        self.assertEqual(
            len(durations),
            1,
            "the two execution paths pass DIFFERENT duration expressions to "
            f"_schedule_verification, so their recheck schedules can drift: {durations}",
        )
        self.assertIn("result.suppressed_secs", durations.pop())

    async def test_a_simulated_write_schedules_no_recheck(self):
        """A `noop`-simulated write changed nothing, so rechecking it would charge a false
        miss to the cited ledger entries — the exact reason `_handle_action` gates on
        `not result.simulated`."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        iid, result = await self._run(models.ACTION_RESOLVE, simulated=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verification"], "")
        stored = store.get_incident(iid)
        assert stored is not None
        self.assertNotIn(stored.verification, models.OPEN_VERIFICATIONS)
