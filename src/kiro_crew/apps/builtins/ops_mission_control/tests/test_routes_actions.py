"""Tests for the HTTP surface's provider writes: actions, proposals and their verification.

A provider write is reachable only through the autonomy gate, runs only against the provider
that raised the signal, and refuses a capability nobody confirmed. The note an action
publishes on the provider's system is redacted first. A successful action or an approved
proposal arms its own verification recheck, ``propose`` mode stores a draft that exactly one
approval can execute, and corruption met while scheduling the recheck is reported. The route
contract itself (registration, the enabled gate, write-only secrets) is in ``test_routes.py``.
"""

import json
import shutil
import tempfile
import unittest
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes
from kiro_crew.apps.builtins.ops_mission_control.tests.routes_helpers import (
    _handler_source,
    _http_surface_source,
)


class TestAnActionSchedulesItsOwnVerification(unittest.IsolatedAsyncioTestCase):
    """A 2xx from a provider is not the end of the story.

    Awaiting `sink.execute`, auditing and returning would make the response's `ok` mean
    only "transmitted". Checkmk documents exactly that gap for its Livestatus command
    dispatch; Nagios's command pipe returns nothing at all. The route records what was
    done and when to look again, and says which of the two it is doing.
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


class TestProposeLoop(unittest.IsolatedAsyncioTestCase):
    """`propose` mode must not be behaviourally identical to `observe`.

    `authorize_action` refuses anything below `act`, so without a stored draft, an
    approve endpoint and a timeout, the mode most operators live in — "tell me what you
    would do" — is prose in a chat transcript with nothing to approve.

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
        # `rglob`, not `glob`: the handlers live in `backend/http_routes/`, and a second
        # caller hidden one directory down is exactly what this must not miss.
        for path in sorted(backend.rglob("*.py")):
            # `encoding=` is required: these sources contain em-dashes, and a bare `read_text()`
            # decodes as cp1252 on Windows and raises before the assertion can run.
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bsink\.execute\(|\.execute\(.*signal", line):
                    offenders.append(
                        f"{path.relative_to(backend).as_posix()}:{lineno}: {line.strip()}"
                    )
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
        import re

        source = _http_surface_source()
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
        """Clipping first could sever a token so the pattern does not match."""

        source = _handler_source("_handle_action")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
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
        import re

        source = _http_surface_source()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        # The action note, the drafted proposal note and the approved proposal's stored note.
        self.assertGreaterEqual(
            len(re.findall(r'(body|proposal)\.get\(\s*"note"', code)),
            3,
            "the scan must see every note heading to a provider",
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
        import re

        source = _http_surface_source()
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


class TestCorruptionIsNotSwallowedWhenSchedulingVerification(unittest.TestCase):
    """Corruption while scheduling a recheck must be REPORTED -- neither swallowed nor raised.

    Two review lanes found opposite halves of this. Design Review caught that a corrupt index
    was silently swallowed: `_schedule_verification` catches `(KeyError, ValueError, OSError)`
    and `JSONDecodeError` subclasses `ValueError`, so corruption was filed under the
    raced-away case at debug level and the action ran with no recheck, forever, invisibly.
    GPT 5.6 then caught that simply re-raising is also wrong: both callers have ALREADY done
    the real external write by the time this runs, so a raise turns a completed action into a
    500 and invites a retry -- in `act` mode, a second real write against production.

    So corruption gets its own clause that logs and writes a SEL audit entry, then returns
    `("", "")` like the transient case. The action's true outcome still reaches the operator,
    and the lost verification is durably recorded rather than dropped.
    """

    @staticmethod
    def _raiser(exc):
        def _fn(*_a, **_kw):
            raise exc

        return _fn

    def test_a_corrupt_index_is_audited_rather_than_raising_after_the_action_ran(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)
        with mock.patch.object(store, "update_fields", self._raiser(corrupt)):
            with mock.patch.object(routes, "_audit") as audited:
                result = routes._schedule_verification("INV-1", "silence", 60)

        self.assertEqual(result, ("", ""), "a completed action must not be reported as failed")
        ops = [c.args[0] for c in audited.call_args_list]
        self.assertIn(
            "action_verification_unscheduled",
            ops,
            "corruption was swallowed with no durable record",
        )
        outcome = next(
            c for c in audited.call_args_list if c.args[0] == "action_verification_unscheduled"
        )
        self.assertEqual(outcome.args[2], "failure")

    def test_a_transient_failure_is_not_audited_as_an_unscheduled_verification(self):
        """The asymmetry: a transient miss is retried by the next pass, so it stays quiet."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(store, "update_fields", self._raiser(denied)):
            with mock.patch.object(routes, "_audit") as audited:
                result = routes._schedule_verification("INV-1", "silence", 60)

        self.assertEqual(result, ("", ""))
        ops = [c.args[0] for c in audited.call_args_list]
        self.assertNotIn("action_verification_unscheduled", ops)
