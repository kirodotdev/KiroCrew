"""The contract ``backend.routes`` keeps as the composition root of the HTTP surface.

``routes.py`` is the module the gateway loads, the one the tests, the spec and the security
posture registry name, and the one whose attributes the suite monkeypatches. Its handlers are
composed from ``backend/http_routes/``. These tests pin what that composition must not change:

* **The observable surface.** The registration table, the enable gate answering before any
  handler work, the webhook ingress order, a proposal executing exactly once, a stale client
  unable to move a replacement incident, and cancellation propagating without a half-done
  write. Each is asserted through the real handlers.
* **The compatibility surface.** Every name ``routes`` defined keeps resolving with its
  signature, and the module objects the suite patches through stay the same objects.
* **The seams.** Patching ``routes.<seam>`` must control EVERY call site that reads the seam,
  whichever module the call site lives in. Each row of ``TestEverySeamControlsItsCallSites`` drives
  one handler to the point where it calls one seam and proves the patched binding ran.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import tempfile
import threading
import types
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    companion,
    dispatch,
    handover,
    ledger,
    ledger_sync,
    models,
    notify_out,
    policy_store,
    registry,
    rotation,
    routes,
    slack_out,
    slot_watch,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import set_top_level, webhook
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ActionResult
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import put_secret

_BASE = "/api/apps/ops-mission-control"

#: The registration table, in registration order, as ``(method, canonical path, handler)``.
#: aiohttp adds the ``HEAD`` twin of every ``GET``. Frozen from the router rather than the
#: source text, so it pins what the gateway serves, byte for byte.
_ROUTE_TABLE: tuple[tuple[str, str, str], ...] = (
    ("HEAD", f"{_BASE}/state", "_handle_state"),
    ("GET", f"{_BASE}/state", "_handle_state"),
    ("HEAD", f"{_BASE}/incidents", "_handle_incidents"),
    ("GET", f"{_BASE}/incidents", "_handle_incidents"),
    ("HEAD", f"{_BASE}/incident", "_handle_incident"),
    ("GET", f"{_BASE}/incident", "_handle_incident"),
    ("POST", f"{_BASE}/incident/transition", "_handle_transition"),
    ("POST", f"{_BASE}/incident/claim", "_handle_claim"),
    ("POST", f"{_BASE}/incident/action", "_handle_action"),
    ("POST", f"{_BASE}/incident/propose", "_handle_propose"),
    ("POST", f"{_BASE}/incident/proposal/decide", "_handle_decide_proposal"),
    ("HEAD", f"{_BASE}/proposals", "_handle_proposals"),
    ("GET", f"{_BASE}/proposals", "_handle_proposals"),
    ("POST", f"{_BASE}/dispatch", "_handle_dispatch"),
    ("HEAD", f"{_BASE}/signals", "_handle_signals"),
    ("GET", f"{_BASE}/signals", "_handle_signals"),
    ("HEAD", f"{_BASE}/handover", "_handle_handover"),
    ("GET", f"{_BASE}/handover", "_handle_handover"),
    ("HEAD", f"{_BASE}/providers", "_handle_providers"),
    ("GET", f"{_BASE}/providers", "_handle_providers"),
    ("PUT", f"{_BASE}/providers/{{provider_id}}/config", "_handle_put_provider_config"),
    ("PUT", f"{_BASE}/providers/{{provider_id}}/secret", "_handle_put_secret"),
    ("DELETE", f"{_BASE}/providers/{{provider_id}}/secret", "_handle_delete_secret"),
    ("PUT", f"{_BASE}/settings", "_handle_put_settings"),
    ("HEAD", f"{_BASE}/rotation", "_handle_rotation"),
    ("GET", f"{_BASE}/rotation", "_handle_rotation"),
    ("POST", f"{_BASE}/rotation/arm", "_handle_rotation_arm"),
    ("HEAD", f"{_BASE}/ledger", "_handle_get_ledger"),
    ("GET", f"{_BASE}/ledger", "_handle_get_ledger"),
    ("HEAD", f"{_BASE}/ledger/contradictions", "_handle_ledger_contradictions"),
    ("GET", f"{_BASE}/ledger/contradictions", "_handle_ledger_contradictions"),
    ("POST", f"{_BASE}/ledger", "_handle_post_ledger"),
    ("POST", f"{_BASE}/ledger/hygiene", "_handle_ledger_hygiene"),
    ("DELETE", f"{_BASE}/ledger", "_handle_delete_ledger"),
    ("POST", f"{_BASE}/webhook", "_handle_webhook"),
)

_HANDLER_SIGNATURE = "(request: 'web.Request') -> 'web.StreamResponse'"

#: Every callable ``routes`` defined, with its signature. A caller that reached one of these
#: through ``routes`` must keep working unchanged, whichever module owns the body.
_HISTORIC_SIGNATURES: dict[str, str] = {
    **{name: _HANDLER_SIGNATURE for _method, _path, name in _ROUTE_TABLE},
    "_Authorized": "(signal: 'Signal', action: 'str', reason: 'str') -> 'None'",
    "_audit": "(op: 'str', target: 'str', outcome: 'str', *, error: 'str' = '') -> 'None'",
    "_authorize": "(signal: 'Signal', action: 'str') -> 'tuple[_Authorized | None, str]'",
    "_execute_authorized": (
        "(sink: 'Any', permit: '_Authorized', payload: 'dict[str, Any]') -> 'Any'"
    ),
    "_execute_stored_proposal": (
        "(incident: 'Any', proposal: 'dict[str, Any]', permit: '_Authorized')"
        " -> 'dict[str, Any]'"
    ),
    "_index_ledger_safely": "() -> 'dict[str, int]'",
    "_json_body": "(request: 'web.Request') -> 'dict[str, Any] | None'",
    "_ledger_sync_status": "() -> 'dict[str, Any]'",
    "_provider_dict": "(info: 'Any') -> 'dict[str, Any]'",
    "_read_capped": "(request: 'web.Request', cap: 'int') -> 'bytes | None'",
    "_require_bool": (
        "(body: 'dict[str, Any]', field: 'str', *, default: 'bool | None' = None)"
        " -> 'bool | None'"
    ),
    "_require_enabled": "(handler: 'Handler') -> 'Handler'",
    "_safe_outbound": "(text: 'str') -> 'str'",
    "_schedule_verification": (
        "(incident_id: 'str', action: 'str', duration_secs: 'Any') -> 'tuple[str, str]'"
    ),
    "_settings_write_or_refuse": (
        "(fn: 'Any', *args: 'Any', code: 'str', applied: 'dict[str, Any]', **kwargs: 'Any')"
        " -> 'web.Response | None'"
    ),
    "_sink_refuses": "(sink: 'Any', action: 'str') -> 'str'",
    "_slack_client": "(request: 'web.Request') -> 'Any | None'",
    "_slot_state": "(request: 'web.Request', slot_key: 'str') -> 'dict[str, Any] | None'",
    "_store_read_refusal": "(exc: 'Exception', *, code: 'str') -> 'web.Response'",
    "_url_has_userinfo": "(remote: 'str') -> 'bool'",
    "_webhook_reject_status": "(detail: 'str') -> 'int'",
    "canonical_slot_key": "(incident_id: 'str') -> 'str'",
    "register_routes": "(app: 'web.Application') -> 'None'",
}

#: The async callables among them. ``inspect.signature`` cannot tell a coroutine function
#: from a plain one, and awaiting a plain return value is a ``TypeError`` at the caller.
_HISTORIC_COROUTINES = frozenset(
    {
        *(name for _method, _path, name in _ROUTE_TABLE),
        "_authorize",
        "_execute_authorized",
        "_execute_stored_proposal",
        "_json_body",
        "_read_capped",
        "_settings_write_or_refuse",
    }
)

#: Every value ``routes`` defined, with the value it had.
_HISTORIC_CONSTANTS: dict[str, Any] = {
    "APP_NAME": "ops-mission-control",
    "_BASE": _BASE,
    "MAX_INCIDENTS_RESPONSE": 200,
    "_MAX_SECRET_LEN": 512,
    "_MAX_NOTE_LEN": 4000,
    "_MAX_REMOTE_LEN": 512,
    "_MAX_PROVIDER_ID_LEN": 128,
    "_SPOOL_RETRY_AFTER_SECS": 120,
    "_WEBHOOK_AUTH_REJECTIONS": frozenset(
        {"webhook source is not enabled", "no signing secret configured", "signature mismatch"}
    ),
    "_WEBHOOK_PAYLOAD_REJECTIONS": frozenset(
        {"malformed JSON", "payload must be a JSON object", "payload has no title"}
    ),
}

#: The module objects the suite patches THROUGH ``routes`` (``routes.rotation.is_primary``,
#: ``routes.store.claim``, ...). They must stay the very module objects, or a patch applied
#: through ``routes`` lands on a copy the handlers never read.
_MODULE_ATTRIBUTES: dict[str, types.ModuleType] = {
    "companion": companion,
    "dispatch": dispatch,
    "handover": handover,
    "ledger": ledger,
    "notify_out": notify_out,
    "policy_store": policy_store,
    "rotation": rotation,
    "slack_out": slack_out,
    "slot_watch": slot_watch,
    "store": store,
    "webhook_mod": webhook,
}

#: The names the suite patches ON ``routes`` itself. Each must reach every call site that
#: reads it; ``TestEverySeamControlsItsCallSites`` proves that site by site.
_SEAMS = (
    "is_app_enabled",
    "get_registry",
    "_audit",
    "_safe_outbound",
    "put_secret",
    "delete_secret",
    "merge_provider_config",
    "_index_ledger_safely",
)

_SIGNING_SECRET = "composition-contract-signing-secret"


def _sign(body: bytes) -> str:
    return hmac.new(_SIGNING_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _request(
    body: Any = None,
    *,
    query: dict[str, str] | None = None,
    match: dict[str, str] | None = None,
    state: Any = None,
) -> Any:
    """A request carrying only what the handlers read: body, query, match info and state."""
    request = mock.MagicMock(spec=web.Request)
    request.json = mock.AsyncMock(return_value={} if body is None else body)
    request.query = query or {}
    request.match_info = match or {}
    request.app = {"state": state}
    return request


def _owner_request(*args: Any, **kwargs: Any) -> Any:
    """``_request`` carrying the dashboard owner's claims, for the owner-gated secret
    routes: the gate reads ``request.get("user")``, ``"app" in request`` and
    ``request["app"]``."""
    request = _request(*args, **kwargs)
    claims = {"user": "local-app", "app": ""}
    request.get = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]
    return request


def _payload(response: web.StreamResponse) -> Any:
    """The JSON body of a handler's response (every handler here answers ``json_response``)."""
    assert isinstance(response, web.Response), response
    assert response.text is not None
    return json.loads(response.text)


class _Home(unittest.IsolatedAsyncioTestCase):
    """An isolated data home, a fresh provider registry and an empty webhook spool.

    Each resource's cleanup is registered in the scope that created it, so a failing
    ``setUp`` cannot leak it.
    """

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.enterContext(mock.patch.dict(os.environ, {"KIROCREW_HOME": self.home}))
        registry.reset_registry()
        self.addCleanup(registry.reset_registry)
        webhook.reset_spool()
        self.addCleanup(webhook.reset_spool)

    async def _client(self) -> TestClient:
        app = web.Application()
        routes.register_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def _enable(self) -> None:
        self.enterContext(mock.patch.object(routes, "is_app_enabled", return_value=True))

    @staticmethod
    def _claim(source: str = "webhook", native_id: str = "probe-1", **kw: Any) -> models.Incident:
        incident = store.claim(
            models.Signal.create(source=source, native_id=native_id, title="latency", **kw),
            operating_mode=models.MODE_ACT,
        )
        assert incident is not None
        return incident


def _served() -> list[tuple[str, str, Any]]:
    """``(method, canonical path, the handler behind the gate)`` for every registered route."""
    app = web.Application()
    routes.register_routes(app)
    served = []
    for route in app.router.routes():
        assert route.resource is not None
        served.append(
            (route.method, route.resource.canonical, getattr(route.handler, "__wrapped__"))
        )
    return served


class TestTheRouteTableIsFrozen(unittest.TestCase):
    def setUp(self) -> None:
        # `register_routes` warms the process-wide registry; drop it when the test ends.
        registry.reset_registry()
        self.addCleanup(registry.reset_registry)

    def test_registration_order_methods_paths_and_handlers_are_unchanged(self) -> None:
        served = [(method, path, handler.__name__) for method, path, handler in _served()]
        self.assertEqual(served, list(_ROUTE_TABLE))

    def test_every_route_is_gated_over_the_facade_handler_of_its_name(self) -> None:
        """The table registers ``routes``' own handler objects, each behind the gate."""
        for method, path, handler in _served():
            with self.subTest(route=path, method=method):
                self.assertIs(handler, getattr(routes, handler.__name__))


class TestTheEnableGateAnswersFirst(_Home):
    async def test_every_route_refuses_while_disabled_without_reading_its_body(self) -> None:
        """A malformed body must not get a 400 out of a disabled app: the gate runs first."""
        self.enterContext(mock.patch.object(routes, "is_app_enabled", return_value=False))
        client = await self._client()
        for method, path, _name in _ROUTE_TABLE:
            if method == "HEAD":
                continue
            with self.subTest(method=method, path=path):
                resp = await client.request(
                    method,
                    path.replace("{provider_id}", "pagerduty"),
                    data=b"{not json",
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(resp.status, 403)
                self.assertEqual(
                    await resp.json(),
                    {"error": "ops-mission-control is disabled", "code": "app_disabled"},
                )


class TestWebhookIngressOrder(_Home):
    """Size is refused before anything is read or verified; trust before the body is parsed."""

    def setUp(self) -> None:
        super().setUp()
        set_top_level("providers", {"webhook": {"enabled": True}})
        put_secret(webhook.PROVIDER_ID, "signing_secret", _SIGNING_SECRET)
        self._enable()
        self.audited = self.enterContext(mock.patch.object(routes, "_audit"))

    async def _post(self, body: bytes, *, signature: str | None = None) -> Any:
        client = await self._client()
        headers = {webhook.SIGNATURE_HEADER: signature} if signature is not None else {}
        return await client.post(f"{_BASE}/webhook", data=body, headers=headers)

    async def test_an_oversized_body_is_refused_before_enqueue_runs(self) -> None:
        with mock.patch.object(webhook, "enqueue", side_effect=AssertionError("enqueue ran")):
            resp = await self._post(b"x" * (webhook.MAX_BODY_BYTES + 1), signature="00")
        self.assertEqual(resp.status, 413)
        self.assertEqual(await resp.json(), {"error": "body too large", "code": "webhook_rejected"})
        self.assertEqual(
            self.audited.call_args_list,
            [mock.call("webhook_ingest", "body too large", "rejected", error="body too large")],
        )

    async def test_an_unsigned_delivery_is_a_401_and_queues_nothing(self) -> None:
        """The body is not even JSON: a 401 (not a 400) proves trust is checked before parse."""
        resp = await self._post(b"{not json")
        self.assertEqual(resp.status, 401)
        self.assertEqual(
            await resp.json(), {"error": "signature mismatch", "code": "webhook_rejected"}
        )
        self.assertEqual(webhook.queue_depth(), 0)
        self.assertEqual(
            self.audited.call_args_list,
            [
                mock.call(
                    "webhook_ingest", "signature mismatch", "rejected", error="signature mismatch"
                )
            ],
        )

    async def test_a_signed_body_that_is_not_json_is_a_400(self) -> None:
        body = b"not json at all"
        resp = await self._post(body, signature=_sign(body))
        self.assertEqual(resp.status, 400)
        self.assertEqual(await resp.json(), {"error": "malformed JSON", "code": "webhook_rejected"})

    async def test_a_signed_delivery_is_queued_and_reported(self) -> None:
        body = json.dumps({"title": "disk full", "id": "disk-1"}).encode("utf-8")
        resp = await self._post(body, signature=_sign(body))
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertEqual(payload, {"ok": True, "signal": payload["signal"], "queued": 1})
        self.assertTrue(payload["signal"].startswith("webhook:"), payload)
        self.assertEqual(
            self.audited.call_args_list,
            [mock.call("webhook_ingest", payload["signal"], "success", error="")],
        )

    async def test_a_full_spool_is_a_retriable_503_naming_when_to_retry(self) -> None:
        webhook._queue.extend(
            models.Signal.create(source="webhook", native_id=f"held-{n}", title="held")
            for n in range(webhook.MAX_QUEUED_SIGNALS)
        )
        body = json.dumps({"title": "one more"}).encode("utf-8")
        resp = await self._post(body, signature=_sign(body))
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers["Retry-After"], "120")
        self.assertEqual(await resp.json(), {"error": "spool is full", "code": "webhook_rejected"})


class TestAnApprovedProposalExecutesOnce(_Home):
    """A double-submitted approval must reach the provider once, however the two interleave."""

    def setUp(self) -> None:
        super().setUp()
        self.executed: list[str] = []
        executed = self.executed

        class _Sink:
            id = "webhook"
            display_name = "Recorder"

            def configured(self) -> bool:
                return True

            def supported_actions(self) -> frozenset[str]:
                return frozenset({models.ACTION_COMMENT})

            async def execute(self, signal: Any, action: str, payload: dict) -> ActionResult:
                executed.append(action)
                return ActionResult(ok=True, action=action, detail="recorded")

        fresh = registry.OpsProviderRegistry()
        fresh.register_action_sink(_Sink())
        registry._registry = fresh
        self._enable()
        self.enterContext(
            mock.patch.object(rotation, "authorize_action", return_value=(True, "granted"))
        )
        self.incident = self._claim()

    async def _propose(self, client: TestClient) -> str:
        resp = await client.post(
            f"{_BASE}/incident/propose",
            json={
                "id": self.incident.incident_id,
                "action": "comment",
                "sink": "webhook",
                "note": "drain it",
            },
        )
        self.assertEqual(resp.status, 200)
        stored = store.get_incident(self.incident.incident_id)
        assert stored is not None and stored.proposed_action is not None
        return str(stored.proposed_action["digest"])

    def _decide(self, client: TestClient, digest: str) -> Any:
        return client.post(
            f"{_BASE}/incident/proposal/decide",
            json={"id": self.incident.incident_id, "approve": True, "digest": digest},
        )

    async def test_a_second_approval_is_a_conflict_not_a_second_write(self) -> None:
        client = await self._client()
        digest = await self._propose(client)
        first = await self._decide(client, digest)
        second = await self._decide(client, digest)
        self.assertEqual(first.status, 200)
        self.assertTrue((await first.json())["executed"])
        self.assertEqual(second.status, 409)
        self.assertEqual((await second.json())["code"], "proposal_conflict")
        self.assertEqual(self.executed, [models.ACTION_COMMENT])

    async def test_two_concurrent_approvals_produce_exactly_one_write(self) -> None:
        """Both decisions are held at a barrier until each has entered the store, so they
        genuinely overlap rather than usually running one after the other."""
        client = await self._client()
        digest = await self._propose(client)
        barrier = threading.Barrier(2, timeout=5)
        decide = store.decide_proposal

        def _together(*args: Any, **kwargs: Any) -> Any:
            barrier.wait()
            return decide(*args, **kwargs)

        with mock.patch.object(store, "decide_proposal", _together):
            responses = await asyncio.wait_for(
                asyncio.gather(self._decide(client, digest), self._decide(client, digest)), 10
            )
        self.assertEqual(sorted(resp.status for resp in responses), [200, 409])
        self.assertEqual(self.executed, [models.ACTION_COMMENT])


class TestAStaleClientCannotMoveTheReplacementIncident(_Home):
    """A re-fired signal is a NEW incident; a client holding the old id cannot touch it."""

    async def test_a_transition_naming_the_closed_incident_leaves_its_replacement_alone(
        self,
    ) -> None:
        old = self._claim()
        store.transition(old.incident_id, models.STATUS_RESOLVED, resolution="drained")
        replacement = self._claim()
        self.assertNotEqual(replacement.incident_id, old.incident_id)
        self._enable()
        client = await self._client()

        stale = await client.post(
            f"{_BASE}/incident/transition",
            json={"id": old.incident_id, "status": models.STATUS_INVESTIGATING},
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual((await stale.json())["code"], "illegal_transition")
        current = store.get_incident(replacement.incident_id)
        assert current is not None
        self.assertEqual(current.status, replacement.status)

        fresh = await client.post(
            f"{_BASE}/incident/transition",
            json={"id": replacement.incident_id, "status": models.STATUS_INVESTIGATING},
        )
        self.assertEqual(fresh.status, 200)

    async def test_the_board_reads_the_derived_slot_never_the_stored_one(self) -> None:
        incident = self._claim()
        store.update_fields(incident.incident_id, slot_key="ops-mission-control-someone-else")
        asked: list[str] = []

        class _State:
            @staticmethod
            def get_slot(key: str) -> None:
                asked.append(key)
                return None

        response = await routes._handle_state(_request(state=_State()))
        self.assertEqual(response.status, 200)
        self.assertEqual(asked, [routes.canonical_slot_key(incident.incident_id)])


class TestCancellationPropagates(_Home):
    """A cancelled request must surface ``CancelledError`` and leave no half-done write."""

    async def _cancel_once_entered(self, coro: Any, entered: asyncio.Event) -> None:
        task = asyncio.ensure_future(coro)
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    @staticmethod
    def _blocking(entered: asyncio.Event) -> Callable[..., Any]:
        async def _block(*_a: Any, **_kw: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

        return _block

    async def test_a_claim_cancelled_during_the_provider_poll_claims_nothing(self) -> None:
        entered = asyncio.Event()
        audited = self.enterContext(mock.patch.object(routes, "_audit"))
        self.enterContext(
            mock.patch.object(
                routes,
                "get_registry",
                return_value=mock.MagicMock(poll_all=self._blocking(entered)),
            )
        )
        await self._cancel_once_entered(
            routes._handle_claim(_request({"signal": {"id": "webhook:probe-1"}})), entered
        )
        self.assertEqual(store.read_index(), {})
        audited.assert_not_called()

    async def test_a_webhook_cancelled_mid_body_never_reaches_enqueue(self) -> None:
        entered = asyncio.Event()
        audited = self.enterContext(mock.patch.object(routes, "_audit"))
        enqueue = self.enterContext(mock.patch.object(webhook, "enqueue"))
        request = _request()
        request.content_length = None
        request.content = mock.MagicMock(read=self._blocking(entered))
        await self._cancel_once_entered(routes._handle_webhook(request), entered)
        enqueue.assert_not_called()
        audited.assert_not_called()

    async def test_hygiene_cancelled_during_the_pull_runs_no_maintenance(self) -> None:
        entered = asyncio.Event()
        self.enterContext(mock.patch.object(rotation, "is_primary", return_value=True))
        self.enterContext(
            mock.patch.object(ledger_sync, "sync_safely", side_effect=self._blocking(entered))
        )
        hygiene = self.enterContext(mock.patch.object(ledger, "hygiene"))
        await self._cancel_once_entered(routes._handle_ledger_hygiene(_request()), entered)
        hygiene.assert_not_called()


class TestTheFacadeKeepsItsHistoricSurface(unittest.TestCase):
    def test_routes_is_still_the_plain_module_at_its_historic_path(self) -> None:
        self.assertTrue(routes.__file__.endswith(os.path.join("backend", "routes.py")))
        self.assertFalse(hasattr(routes, "__path__"), "routes must not become a package")

    def test_every_historic_callable_resolves_with_its_signature(self) -> None:
        for name, expected in sorted(_HISTORIC_SIGNATURES.items()):
            with self.subTest(name=name):
                target = getattr(routes, name)
                self.assertEqual(str(inspect.signature(target)), expected)
                self.assertEqual(
                    inspect.iscoroutinefunction(target), name in _HISTORIC_COROUTINES, name
                )

    def test_the_historic_classes_and_values_resolve(self) -> None:
        self.assertTrue(issubclass(routes._NotABool, ValueError))
        self.assertTrue(callable(routes.Handler))
        self.assertEqual(routes.logger.name, routes.__name__)
        for name, expected in _HISTORIC_CONSTANTS.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(routes, name), expected)
        self.assertTrue(routes._SAFE_BRANCH_RE.fullmatch("main"))
        self.assertTrue(routes._SAFE_LOGIN_RE.fullmatch("octo-cat"))

    def test_the_patched_through_modules_are_the_same_objects(self) -> None:
        for name, module in _MODULE_ATTRIBUTES.items():
            with self.subTest(name=name):
                self.assertIs(getattr(routes, name), module)

    def test_every_seam_is_patchable_on_routes(self) -> None:
        for name in _SEAMS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(routes, name)))


class _SeamReached(Exception):
    """Raised by a patched seam, so reaching the call site is unmistakable."""


class TestEverySeamControlsItsCallSites(_Home):
    """``mock.patch.object(routes, <seam>)`` must reach every call site that reads the seam.

    Each test patches one seam on ``routes`` with a function that raises ``_SeamReached``,
    drives one handler to the point where it calls that seam, and asserts the raise. A call
    site that resolved the name anywhere but ``routes``' own binding would run the real
    function instead and the assertion would fail.
    """

    def _patch_seam(self, name: str) -> None:
        self.enterContext(mock.patch.object(routes, name, side_effect=_SeamReached(name)))

    async def _reaches(self, seam: str, call: Callable[[], Any]) -> None:
        self._patch_seam(seam)
        with self.assertRaises(_SeamReached):
            result = call()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, 10)

    def _act_on(self, incident: models.Incident, action: str = models.ACTION_COMMENT) -> Any:
        return _request(
            {"id": incident.incident_id, "action": action, "sink": "webhook", "note": "n"}
        )

    # -- is_app_enabled -------------------------------------------------------------------

    async def test_is_app_enabled_is_read_by_the_gate(self) -> None:
        async def _never(_request: web.Request) -> web.StreamResponse:
            raise AssertionError("the handler ran before the gate")

        await self._reaches("is_app_enabled", lambda: routes._require_enabled(_never)(_request()))

    # -- get_registry ---------------------------------------------------------------------

    async def test_get_registry_is_read_by_every_handler_that_resolves_a_provider(self) -> None:
        cases: dict[str, Callable[[], Any]] = {
            "state": lambda: routes._handle_state(_request()),
            "handover": lambda: routes._handle_handover(_request()),
            "signals": lambda: routes._handle_signals(_request()),
            "providers": lambda: routes._handle_providers(_request()),
            "rotation": lambda: routes._handle_rotation(_request()),
            "rotation_arm": lambda: routes._handle_rotation_arm(
                _request(state=types.SimpleNamespace(crons=object()))
            ),
            "claim": lambda: routes._handle_claim(_request({"signal": {"id": "x"}})),
            "put_provider_config": lambda: routes._handle_put_provider_config(
                _owner_request({"site": "datadoghq.eu"}, match={"provider_id": "datadog"})
            ),
            "put_secret": lambda: routes._handle_put_secret(
                _owner_request(
                    {"field": "api_token", "value": "v"}, match={"provider_id": "pagerduty"}
                )
            ),
        }
        for label, call in cases.items():
            with self.subTest(handler=label):
                with mock.patch.object(routes, "get_registry", side_effect=_SeamReached(label)):
                    with self.assertRaises(_SeamReached):
                        await asyncio.wait_for(call(), 10)

    async def test_get_registry_is_read_on_both_execution_paths(self) -> None:
        incident = self._claim()
        self.enterContext(
            mock.patch.object(rotation, "authorize_action", return_value=(True, "granted"))
        )
        permit, _reason = await routes._authorize(incident.signal, models.ACTION_COMMENT)
        assert permit is not None
        cases: dict[str, Callable[[], Any]] = {
            "action": lambda: routes._handle_action(self._act_on(incident)),
            "stored_proposal": lambda: routes._execute_stored_proposal(
                incident, {"action": models.ACTION_COMMENT, "note": "n"}, permit
            ),
        }
        for label, call in cases.items():
            with self.subTest(path=label):
                with mock.patch.object(routes, "get_registry", side_effect=_SeamReached(label)):
                    with self.assertRaises(_SeamReached):
                        await asyncio.wait_for(call(), 10)

    # -- _audit ---------------------------------------------------------------------------

    async def test_audit_is_written_through_routes_on_the_refusal_paths(self) -> None:
        self.enterContext(mock.patch.object(rotation, "is_primary", return_value=False))
        self.enterContext(mock.patch.object(rotation, "primary_owner", return_value=""))
        oversized = _request()
        oversized.content_length = webhook.MAX_BODY_BYTES + 1
        refuse = mock.Mock(side_effect=PermissionError(13, "Permission denied"))
        cases: dict[str, Callable[[], Any]] = {
            "webhook": lambda: routes._handle_webhook(oversized),
            "secret_on_config_route": lambda: routes._handle_put_provider_config(
                _owner_request({"api_token": "x"}, match={"provider_id": "pagerduty"})
            ),
            "hygiene_not_primary": lambda: routes._handle_ledger_hygiene(_request()),
            "settings_write_refused": lambda: routes._settings_write_or_refuse(
                refuse, code="policy_store_unwritable", applied={}
            ),
        }
        for label, call in cases.items():
            with self.subTest(site=label):
                with mock.patch.object(routes, "_audit", side_effect=_SeamReached(label)):
                    with self.assertRaises(_SeamReached):
                        await asyncio.wait_for(call(), 10)

    async def test_audit_is_written_through_routes_on_the_store_refusals(self) -> None:
        refuse = mock.Mock(side_effect=PermissionError(13, "Permission denied"))
        corrupt = mock.Mock(side_effect=json.JSONDecodeError("Expecting value", "{", 0))
        cases: dict[str, tuple[Any, str, Any, Callable[[], Any]]] = {
            "ledger_post": (
                ledger,
                "upsert",
                refuse,
                lambda: routes._handle_post_ledger(_request({"pattern": "p", "fix": "f"})),
            ),
            "ledger_delete": (
                ledger,
                "remove",
                refuse,
                lambda: routes._handle_delete_ledger(_request(query={"id": "abc"})),
            ),
            "decide_refused": (
                store,
                "decide_proposal",
                refuse,
                lambda: routes._handle_decide_proposal(_request({"id": "INV-1", "approve": True})),
            ),
            "provider_config_refused": (
                routes,
                "merge_provider_config",
                refuse,
                lambda: routes._handle_put_provider_config(
                    _owner_request({"site": "datadoghq.eu"}, match={"provider_id": "datadog"})
                ),
            ),
            "verification_unscheduled": (
                store,
                "update_fields",
                corrupt,
                lambda: routes._schedule_verification("INV-1", models.ACTION_SILENCE, 60),
            ),
        }
        for label, (owner, attr, failure, call) in cases.items():
            with self.subTest(site=label):
                with mock.patch.object(owner, attr, failure):
                    with mock.patch.object(routes, "_audit", side_effect=_SeamReached(label)):
                        with self.assertRaises(_SeamReached):
                            result = call()
                            if inspect.isawaitable(result):
                                await asyncio.wait_for(result, 10)

    async def test_audit_is_written_through_routes_on_the_success_paths(self) -> None:
        incident = self._claim()
        self.enterContext(
            mock.patch.object(rotation, "authorize_action", return_value=(True, "granted"))
        )
        permit, _reason = await routes._authorize(incident.signal, models.ACTION_COMMENT)
        assert permit is not None
        firing = incident.signal
        other = self._claim(native_id="probe-2")
        cases: dict[str, Callable[[], Any]] = {
            "transition": lambda: routes._handle_transition(
                _request({"id": incident.incident_id, "status": models.STATUS_INVESTIGATING})
            ),
            "propose": lambda: routes._handle_propose(self._act_on(other)),
            "action": lambda: routes._handle_action(self._act_on(incident)),
            "stored_proposal": lambda: routes._execute_stored_proposal(
                incident, {"action": models.ACTION_COMMENT, "note": "n"}, permit
            ),
            "settings": lambda: routes._handle_put_settings(_owner_request({"mode": "observe"})),
        }
        for label, call in cases.items():
            with self.subTest(site=label):
                with mock.patch.object(routes, "_audit", side_effect=_SeamReached(label)):
                    with self.assertRaises(_SeamReached):
                        await asyncio.wait_for(call(), 10)

        # The manual claim audits last, after the claim and its evidence.
        store.transition(incident.incident_id, models.STATUS_RESOLVED, resolution="done")
        poll = mock.AsyncMock(return_value=([firing], {}))
        with mock.patch.object(registry.get_registry(), "poll_all", poll):
            with mock.patch.object(routes, "_audit", side_effect=_SeamReached("claim")):
                with self.assertRaises(_SeamReached):
                    await asyncio.wait_for(
                        routes._handle_claim(_request({"signal": {"id": firing.id}})), 10
                    )

    # -- _safe_outbound -------------------------------------------------------------------

    async def test_the_redaction_floor_is_applied_through_routes(self) -> None:
        incident = self._claim()
        self.enterContext(
            mock.patch.object(rotation, "authorize_action", return_value=(True, "granted"))
        )
        permit, _reason = await routes._authorize(incident.signal, models.ACTION_COMMENT)
        assert permit is not None
        cases: dict[str, Callable[[], Any]] = {
            "action_note": lambda: routes._handle_action(self._act_on(incident)),
            "proposal_note": lambda: routes._handle_propose(self._act_on(incident)),
            "transition_diagnosis": lambda: routes._handle_transition(
                _request({"id": incident.incident_id, "status": "investigating", "diagnosis": "d"})
            ),
            "stored_proposal_note": lambda: routes._execute_stored_proposal(
                incident, {"action": models.ACTION_COMMENT, "note": "n"}, permit
            ),
            # The ledger write redacts through the same floor, before the entry's id exists.
            "ledger_write": lambda: routes._handle_post_ledger(
                _request({"pattern": "p", "fix": "f"})
            ),
        }
        for label, call in cases.items():
            with self.subTest(site=label):
                with mock.patch.object(routes, "_safe_outbound", side_effect=_SeamReached(label)):
                    with self.assertRaises(_SeamReached):
                        await asyncio.wait_for(call(), 10)

    # -- the keystone and config writers --------------------------------------------------

    async def test_put_secret_is_read_by_the_secret_route(self) -> None:
        await self._reaches(
            "put_secret",
            lambda: routes._handle_put_secret(
                _owner_request(
                    {"field": "api_token", "value": "v"}, match={"provider_id": "pagerduty"}
                )
            ),
        )

    async def test_delete_secret_is_read_by_the_revocation_route(self) -> None:
        await self._reaches(
            "delete_secret",
            lambda: routes._handle_delete_secret(
                _owner_request(match={"provider_id": "pagerduty"})
            ),
        )

    async def test_merge_provider_config_is_read_by_the_config_route(self) -> None:
        await self._reaches(
            "merge_provider_config",
            lambda: routes._handle_put_provider_config(
                _owner_request({"site": "datadoghq.eu"}, match={"provider_id": "datadog"})
            ),
        )

    async def test_index_ledger_safely_is_read_by_the_hygiene_pass(self) -> None:
        self.enterContext(mock.patch.object(rotation, "is_primary", return_value=True))
        self.enterContext(mock.patch.object(ledger_sync, "sync_safely", mock.AsyncMock()))
        self.enterContext(mock.patch.object(ledger, "hygiene", return_value={}))
        await self._reaches(
            "_index_ledger_safely", lambda: routes._handle_ledger_hygiene(_request())
        )


class TestSeamsReachTheHelpersBehindAHandler(_Home):
    """A helper that calls a seam receives it from the handler that calls the helper.

    The rows above prove each handler's own call sites. These prove the ones a handler
    reaches only through a helper: the post-action recheck, the settings write refusal and
    the approved proposal's execution. A helper that fell back to any binding but the
    facade's would write its audit row, resolve its registry or redact its note with an
    object the patch never touched.
    """

    def _verifiable_sink(self) -> None:
        class _Sink:
            id = "webhook"
            display_name = "Recorder"

            def configured(self) -> bool:
                return True

            def supported_actions(self) -> frozenset[str]:
                return frozenset(models.VALID_ACTIONS)

            async def execute(self, signal: Any, action: str, payload: dict) -> ActionResult:
                return ActionResult(ok=True, action=action, detail="done")

        fresh = registry.OpsProviderRegistry()
        fresh.register_action_sink(_Sink())
        registry._registry = fresh

    async def test_the_recheck_after_a_direct_action_audits_through_routes(self) -> None:
        self._verifiable_sink()
        incident = self._claim()
        self.enterContext(
            mock.patch.object(rotation, "authorize_action", return_value=(True, "granted"))
        )
        audited = self.enterContext(mock.patch.object(routes, "_audit"))
        corrupt = json.JSONDecodeError("Expecting value", "{", 0)
        with mock.patch.object(store, "update_fields", side_effect=corrupt):
            response = await routes._handle_action(
                _request({"id": incident.incident_id, "action": models.ACTION_RESOLVE})
            )
        self.assertEqual(response.status, 200)
        self.assertIn(
            "action_verification_unscheduled", [c.args[0] for c in audited.call_args_list]
        )

    async def test_a_refused_settings_write_audits_through_routes(self) -> None:
        audited = self.enterContext(mock.patch.object(routes, "_audit"))
        refuse = PermissionError(13, "Permission denied")
        with mock.patch.object(policy_store, "set_ceiling", side_effect=refuse):
            response = await routes._handle_put_settings(_owner_request({"mode": "observe"}))
        self.assertEqual(response.status, 503)
        self.assertEqual(
            audited.call_args_list, [mock.call("settings_put", "refused after []", "failure")]
        )

    async def _approvable(self) -> tuple[models.Incident, str]:
        self._verifiable_sink()
        incident = self._claim()
        store.propose_action(
            incident.incident_id, action=models.ACTION_COMMENT, sink="webhook", note="drain it"
        )
        stored = store.get_incident(incident.incident_id)
        assert stored is not None and stored.proposed_action is not None
        self.enterContext(
            mock.patch.object(rotation, "authorize_action", return_value=(True, "granted"))
        )
        return incident, str(stored.proposed_action["digest"])

    def _approve(self, incident: models.Incident, digest: str) -> Any:
        return routes._handle_decide_proposal(
            _request({"id": incident.incident_id, "approve": True, "digest": digest})
        )

    async def test_an_approved_proposal_resolves_its_sink_through_routes(self) -> None:
        incident, digest = await self._approvable()
        with mock.patch.object(routes, "get_registry", side_effect=_SeamReached("registry")):
            with self.assertRaises(_SeamReached):
                await asyncio.wait_for(self._approve(incident, digest), 10)

    async def test_an_approved_proposal_redacts_its_note_through_routes(self) -> None:
        incident, digest = await self._approvable()
        with mock.patch.object(routes, "_safe_outbound", side_effect=_SeamReached("floor")):
            with self.assertRaises(_SeamReached):
                await asyncio.wait_for(self._approve(incident, digest), 10)

    async def test_an_approved_proposal_audits_its_write_through_routes(self) -> None:
        incident, digest = await self._approvable()
        audited = self.enterContext(mock.patch.object(routes, "_audit"))
        response = await asyncio.wait_for(self._approve(incident, digest), 10)
        self.assertEqual(response.status, 200)
        self.assertIn(
            mock.call(
                "incident_action",
                f"{incident.incident_id} comment via webhook (approved proposal)",
                "success",
                error="",
            ),
            audited.call_args_list,
        )


def _projection_modules() -> list[types.ModuleType]:
    """Every module of the ``http_routes`` package, sub-packages included, from its directory."""
    package = Path(routes.__file__).parent / "http_routes"
    base = f"{routes.__package__}.http_routes"
    modules = []
    for path in sorted(package.rglob("*.py")):
        parts = path.relative_to(package).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(importlib.import_module(".".join((base, *parts))))
    return modules


def _module_tree(module: types.ModuleType) -> ast.Module:
    return ast.parse(inspect.getsource(module))


#: `test/test_security_posture.py`'s redactor call-site pattern. A module whose text matches
#: it must be a registered redaction sink, and only `routes.py` is registered for this app's
#: HTTP surface — so the projections must not match it, not even in prose.
_REDACTOR_CALL_RE = re.compile(r"\bStreamRedactor\(|\b\w*redact\w*\(|\.redact\(")


class TestTheProjectionsStayBehindTheFacade(unittest.TestCase):
    """The structure that makes the facade's seams hold, pinned where a later edit breaks it."""

    def test_the_package_is_complete_and_importable(self) -> None:
        names = {module.__name__.rsplit(".", 1)[-1] for module in _projection_modules()}
        self.assertEqual(
            names,
            {
                "http_routes",
                "_shared",
                "actions",
                "board",
                "configuration",
                "ledger",
                "lifecycle",
                "webhook",
            },
        )

    def test_no_projection_imports_the_facade(self) -> None:
        """``routes`` imports the projections; the reverse would be a cycle.

        Relative imports are resolved against the importing module's package, so
        ``from .. import routes`` is caught the same as the absolute spelling.
        """
        facade = routes.__name__
        for module in _projection_modules():
            package = module.__name__ if hasattr(module, "__path__") else module.__package__
            for node in ast.walk(_module_tree(module)):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    source = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package
                    )
                    imported = [source] + [f"{source}.{a.name}" for a in node.names]
                with self.subTest(module=module.__name__, line=getattr(node, "lineno", 0)):
                    self.assertNotIn(facade, imported)

    def test_no_projection_binds_a_seam_of_its_own(self) -> None:
        """A projection that bound a seam at module scope would read it when the facade's
        binding was not passed — the one way a dropped argument fails silently.

        Asserted on the imported module's namespace, not its syntax, so a binding made
        through a conditional import, an annotated assignment or a later rebind is caught
        too; and no module may hold the facade itself under any name.
        """
        for module in _projection_modules():
            with self.subTest(module=module.__name__):
                self.assertEqual(sorted(set(vars(module)) & set(_SEAMS)), [])
                self.assertFalse(any(value is routes for value in vars(module).values()))

    def test_a_seam_parameter_is_keyword_only_with_no_default(self) -> None:
        """A default would let a call site that forgot the facade's binding run anyway, and a
        positional seam could be filled by an argument meant for something else."""
        for module in _projection_modules():
            for node in ast.walk(_module_tree(module)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = node.args
                positional = {a.arg for a in args.posonlyargs + args.args}
                defaulted = {
                    a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
                }
                with self.subTest(function=f"{module.__name__}.{node.name}"):
                    self.assertEqual(sorted(positional & set(_SEAMS)), [])
                    self.assertEqual(sorted(defaulted & set(_SEAMS)), [])

    def test_every_facade_handler_is_one_delegation_passing_only_seams(self) -> None:
        """No handler logic hides in the facade, and every argument it adds is a seam."""
        tree = _module_tree(routes)
        aliases = {
            alias.asname: f"{node.module}.{alias.name}"
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
            if alias.asname
        }
        shells = [
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_handle_")
        ]
        self.assertGreaterEqual(len(shells), 19)
        for shell in shells:
            with self.subTest(handler=shell.name):
                self.assertEqual(len(shell.body), 1, "a shell is exactly one statement")
                (statement,) = shell.body
                self.assertIsInstance(statement, ast.Return)
                assert isinstance(statement, ast.Return)
                self.assertIsInstance(statement.value, ast.Await)
                assert isinstance(statement.value, ast.Await)
                call = statement.value.value
                assert isinstance(call, ast.Call)
                assert isinstance(call.func, ast.Attribute)
                assert isinstance(call.func.value, ast.Name)
                self.assertEqual(call.func.attr, shell.name, "delegates to its same-named body")
                target = aliases[call.func.value.id]
                self.assertTrue(target.startswith(f"{routes.__package__}.http_routes."), target)
                self.assertEqual([ast.unparse(a) for a in call.args], ["request"])
                for keyword in call.keywords:
                    self.assertIn(keyword.arg, _SEAMS)
                    self.assertEqual(ast.unparse(keyword.value), keyword.arg)

    def test_the_whole_surface_logs_under_the_facade_name(self) -> None:
        for module in _projection_modules():
            logger = getattr(module, "logger", None)
            if logger is not None:
                with self.subTest(module=module.__name__):
                    self.assertIs(logger, routes.logger)

    def test_redaction_stays_in_the_registered_sink(self) -> None:
        self.assertRegex(Path(routes.__file__).read_text(encoding="utf-8"), _REDACTOR_CALL_RE)
        for module in _projection_modules():
            with self.subTest(module=module.__name__):
                text = Path(str(module.__file__)).read_text(encoding="utf-8")
                self.assertIsNone(_REDACTOR_CALL_RE.search(text))


class TestTheBoardReads(_Home):
    """The read projections, through the handlers the router serves."""

    async def test_the_handover_digest_carries_its_rendered_text(self) -> None:
        self._claim()
        response = await routes._handle_handover(_request())
        self.assertEqual(response.status, 200)
        payload = _payload(response)
        self.assertIsInstance(payload["text"], str)
        self.assertTrue(payload["text"])

    async def test_the_provider_listing_names_every_catalogued_provider(self) -> None:
        response = await routes._handle_providers(_request())
        self.assertEqual(response.status, 200)
        ids = [p["id"] for p in _payload(response)["providers"]]
        self.assertEqual(ids, [p.id for p in registry.get_registry().catalog()])

    async def test_an_unknown_or_missing_incident_is_a_coded_404(self) -> None:
        for query in ({}, {"id": "INV-nope"}):
            with self.subTest(query=query):
                response = await routes._handle_incident(_request(query=query))
                self.assertEqual(response.status, 404)
                self.assertEqual(_payload(response)["code"], "unknown_incident")

    def test_a_slot_read_degrades_to_no_evidence(self) -> None:
        """Every way the slot lookup cannot answer reads as "no evidence", never a 500."""

        class _Raising:
            @staticmethod
            def get_slot(key: str) -> None:
                raise RuntimeError("state exploded")

        class _Empty:
            @staticmethod
            def get_slot(key: str) -> None:
                return None

        self.assertIsNone(routes._slot_state(_request(state=_Empty()), ""))
        self.assertIsNone(routes._slot_state(_request(state=object()), "k"))
        self.assertIsNone(routes._slot_state(_request(state=_Raising()), "k"))
        self.assertIsNone(routes._slot_state(_request(state=_Empty()), "k"))

    def test_a_slot_reports_its_message_roles(self) -> None:
        slot = types.SimpleNamespace(
            running=True,
            waiting_for_input=False,
            messages=[types.SimpleNamespace(role="user"), {"role": "assistant"}],
        )
        state = types.SimpleNamespace(get_slot=lambda key: slot)
        self.assertEqual(
            routes._slot_state(_request(state=state), "k"),
            {
                "running": True,
                "pending_approval": False,
                "waiting_for_input": False,
                "messages": [{"role": "user"}, {"role": "assistant"}],
            },
        )


class TestTheLifecycleAndConfigurationEdges(_Home):
    """The request-shape refusals, which answer before any store is touched."""

    async def test_a_dispatch_run_returns_a_brief_per_claim(self) -> None:
        incident = self._claim()
        claimed = dispatch.ClaimedIncident(incident=incident)
        result = mock.MagicMock(claimed=[claimed])
        result.to_dict.return_value = {"changed": True}
        with mock.patch.object(dispatch, "run_cycle", mock.AsyncMock(return_value=result)):
            response = await routes._handle_dispatch(_request())
        payload = _payload(response)
        self.assertTrue(payload["changed"])
        self.assertEqual(list(payload["briefs"]), [incident.incident_id])

    async def test_a_malformed_body_is_refused_before_any_work(self) -> None:
        for name in (
            "_handle_transition",
            "_handle_claim",
            "_handle_action",
            "_handle_propose",
            "_handle_decide_proposal",
            "_handle_put_settings",
            "_handle_post_ledger",
        ):
            with self.subTest(handler=name):
                request = _owner_request()
                request.json = mock.AsyncMock(side_effect=ValueError("not json"))
                response = await getattr(routes, name)(request)
                self.assertEqual(response.status, 400)
                self.assertEqual(_payload(response)["code"], "body_not_object")

    async def test_the_secret_route_refuses_each_malformed_request(self) -> None:
        cases = {
            "no field": ({"value": "v"}, "pagerduty", 400, "missing_required_field"),
            "empty value": ({"field": "api_token", "value": ""}, "pagerduty", 400, None),
            "too long": ({"field": "api_token", "value": "v" * 513}, "pagerduty", 400, None),
            "unknown provider": ({"field": "api_token", "value": "v"}, "nope", 404, None),
            "unknown field": ({"field": "nope", "value": "v"}, "pagerduty", 400, None),
        }
        codes = {
            "empty value": "missing_required_field",
            "too long": "value_too_long",
            "unknown provider": "unknown_provider",
            "unknown field": "unknown_secret_field",
        }
        for label, (body, provider, status, _code) in cases.items():
            with self.subTest(case=label):
                response = await routes._handle_put_secret(
                    _owner_request(body, match={"provider_id": provider})
                )
                self.assertEqual(response.status, status)
                expected = codes.get(label, "missing_required_field")
                self.assertEqual(_payload(response)["code"], expected)

    async def test_a_secret_round_trips_through_save_and_revoke(self) -> None:
        saved = await routes._handle_put_secret(
            _owner_request(
                {"field": "api_token", "value": "u+token"}, match={"provider_id": "pagerduty"}
            )
        )
        self.assertEqual(
            _payload(saved), {"ok": True, "provider": "pagerduty", "field": "api_token"}
        )
        revoked = await routes._handle_delete_secret(
            _owner_request(match={"provider_id": "pagerduty"})
        )
        self.assertEqual(_payload(revoked), {"ok": True, "removed": True})
        missing = await routes._handle_delete_secret(_owner_request(match={}))
        self.assertEqual(missing.status, 400)

    async def test_the_ledger_reads_and_a_missing_entry_is_a_coded_404(self) -> None:
        written = await routes._handle_post_ledger(
            _request({"pattern": "disk full", "fix": "prune"})
        )
        entry_id = _payload(written)["entry"]["entry_id"]
        listed = _payload(await routes._handle_get_ledger(_request()))
        self.assertEqual([e["entry_id"] for e in listed["entries"]], [entry_id])
        found = _payload(await routes._handle_ledger_contradictions(_request()))
        self.assertEqual(found, {"contradictions": [], "count": 0})
        gone = await routes._handle_delete_ledger(_request(query={"id": "not-an-entry"}))
        self.assertEqual(gone.status, 404)
        self.assertEqual(_payload(gone)["code"], "not_found")
        removed = await routes._handle_delete_ledger(_request(query={"id": entry_id}))
        self.assertEqual(_payload(removed), {"ok": True, "removed": True})
        unnamed = await routes._handle_delete_ledger(_request())
        self.assertEqual(unnamed.status, 400)

    async def test_every_settings_field_lands_in_one_request(self) -> None:
        body = {
            "mode": "observe",
            "autonomy_rules": [],
            "schedule_github_login": "octo-cat",
            "schedule_strict_gating": True,
            "pagerduty_user_id": "PXXXXXX",
            "incidentio_user_id": "01HZZZ",
            "primary_instance": True,
            "slack_enabled": False,
            "slack_channel": "C123456",
            "notify_enabled": False,
            "ledger_sync_remote": "git@github.com:org/repo.git",
            "ledger_sync_branch": "main",
            "ledger_sync_enabled": False,
            "max_claims_per_cycle": 3,
            "stale_after_secs": 600,
            "needs_human_stale_after_secs": 900,
        }
        response = await routes._handle_put_settings(_owner_request(body))
        self.assertEqual(response.status, 200)
        self.assertEqual(sorted(_payload(response)["applied"]), sorted(body))


if __name__ == "__main__":
    unittest.main()
