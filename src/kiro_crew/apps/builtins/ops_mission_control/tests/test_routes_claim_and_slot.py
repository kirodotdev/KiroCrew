"""Tests for claiming an incident by hand, and for the incident's chat slot.

``POST /incident/claim`` authorizes against the provider's own copy of the signal, refuses a
signal that is not firing, and acks the push spool. The slot key is derived rather than read
back from the record, and "waiting on you" reads the slot's public contract. One test reads
the frontend's ``IncidentChat.tsx`` to pin the slot-key shape, which makes this file a
cross-surface guard.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes
from kiro_crew.apps.builtins.ops_mission_control.tests.routes_helpers import (
    _handler_source,
    _http_surface_source,
)


class TestBlockedStateReadsThePublicSlotContract(unittest.IsolatedAsyncioTestCase):
    """\"Waiting on you" must not depend on a private attribute of another module.

    ``_slot_state`` derived ``pending_approval`` by reading ``slot._approval_futures``
    directly. Review flagged it and was right: a private attribute is not a contract, so a
    core refactor that renamed it would silently turn "waiting on you" into "progressing" on
    this board -- the operator stops being told an incident needs them -- with nothing
    failing anywhere to say so.

    ``_ChatSlot.to_dict()`` is the owner's public serializer and already derives the same
    fact. These tests pin BOTH that we ask it, and that our answer agrees with the core's
    across the states that matter -- against the real class, not a stand-in, because a mock
    would happily agree with a contract that does not exist.
    """

    def test_no_private_slot_attribute_is_read(self):
        import inspect
        import re

        source = inspect.getsource(routes._slot_state)
        # CODE only. The comment above the fix names `_approval_futures` to record what was
        # wrong and why, so a bare substring check would forbid explaining the very thing it
        # enforces -- the same reason this file has a `rendered()` helper on the frontend
        # side. Strip comments, then look for actual access (attribute or getattr).
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
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

    def test_waiting_for_input_is_read_from_the_serializer(self):
        """The chat slot exposes its open-question flag only through ``to_dict()``."""

        class _Slot:
            running = False
            messages: list = []

            def __init__(self, public: dict | None):
                self.public = public

            def to_dict(self, **_kw):
                if self.public is None:
                    raise RuntimeError("slot serializer blew up")
                return self.public

        def read(slot):
            class _State:
                @staticmethod
                def get_slot(key):
                    return slot

            class _Req:
                app = {"state": _State()}

            result = routes._slot_state(cast(web.Request, _Req()), "probe")
            assert result is not None, "the stub always resolves a slot"
            return result["waiting_for_input"]

        self.assertTrue(read(_Slot({"needs_input": True, "waiting_for_input": True})))
        # A finished turn (assistant spoke last, nothing asked) is not blocked on input.
        self.assertFalse(read(_Slot({"needs_input": False, "waiting_for_input": True})))
        self.assertFalse(read(_Slot(None)), "a serializer fault degrades to False")


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

        source = _handler_source("_handle_claim")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
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
                    with mock.patch.object(
                        routes.dispatch, "gather_evidence_safely", mock.AsyncMock(return_value=[])
                    ):
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

        source = _http_surface_source()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn("canonical_slot_key(", code, "the scan must see the key derivation")
        self.assertNotIn(
            "inc.slot_key",
            code,
            "a resolution path reads the agent-reported slot_key instead of deriving it",
        )
        self.assertNotIn("incident.slot_key", code)


class TestManualClaimRequiresAFiringSignal(unittest.IsolatedAsyncioTestCase):
    """`POST /incident/claim` must refuse a signal that is not firing.

    `poll_all` returns EVERY state — firing, ok and suppressed — so matching on id alone is
    wrong, and a local named `firing` is what hides it: a signal that recovered between the
    board's poll and this one comes back as `ok`, matches, and mints an incident for a
    fault that has already cleared. The two other `poll_all` consumers
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
