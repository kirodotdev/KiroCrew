"""A pushed signal's absence never resolves its incident; an explicit cleared delivery does.

A push source (the webhook spool) announces a fault once and never re-asserts it, so its
signal is absent from every poll after the claim whether or not the fault is still live.
These tests pin the four places that rule lives:

* the rule functions in ``dispatch`` (what counts as a cleared event, the refusal text and
  the exact resolution written on one);
* the two ``store.transition`` hooks it is enforced through, decided under the index lock;
* the guard on ``POST /incident/transition`` for an automated (``internal_auth``) caller;
* the dispatch cycle's own resolve, which must run before the spool ack drops the event.

Each docstring names the wrong implementation the test exists to fail on.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from collections import deque
from collections.abc import Iterator
from typing import Any
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    dispatch,
    models,
    registry,
    routes,
    slack_out,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

_BASE = "/api/apps/ops-mission-control"
_NATIVE = "alert/1"
_SIGNAL_ID = f"webhook:{_NATIVE}"

#: Part A of the cleared-event resolution for ``_SIGNAL_ID``, and part B.
_PART_A = "Resolved on an explicit cleared event: webhook delivered state=ok for webhook:alert/1."
_PART_B = "Cleared without diagnosis, may recur."

#: The words the refusal must carry so an agent knows what is missing and what to do.
_REFUSAL_WORDS = (
    "is not evidence of recovery",
    "latest spooled event",
    "dispatch heartbeat",
    "set needs_human",
)


def _signal(
    state: str = models.STATE_FIRING, *, source: str = "webhook", native_id: str = _NATIVE
) -> models.Signal:
    return models.Signal.create(
        source=source, native_id=native_id, title="queue depth over limit", state=state
    )


def _spool(*states: str) -> None:
    """Spool deliveries for ``_SIGNAL_ID`` in order, bypassing signature verification."""
    for state in states:
        webhook._queue.append(_signal(state))


def _incident(source: str = "webhook") -> models.Incident:
    return models.Incident(
        incident_id="INV-7", signal=_signal(source=source), status=models.STATUS_DISPATCHED
    )


class _SnapshotSource:
    """A registered polled source that does not set ``is_snapshot``, so it IS a snapshot."""

    id = "fake"
    display_name = "Fake"

    def __init__(self) -> None:
        self.signals: list[models.Signal] = []

    def configured(self) -> bool:
        return True

    async def poll(self) -> list[models.Signal]:
        return list(self.signals)


class _ConfiguredWebhook(webhook.WebhookSignalSource):
    """The REAL push adapter, forced configured, so polls read the real spool."""

    def configured(self) -> bool:
        return True


class _Home(unittest.IsolatedAsyncioTestCase):
    """An isolated data home, an empty spool and a registry holding the webhook and a fake
    snapshot source (and nothing that reaches a network)."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.enterContext(mock.patch.dict(os.environ, {"KIROCREW_HOME": self.home}))
        registry.reset_registry()
        self.addCleanup(registry.reset_registry)
        webhook.reset_spool()
        self.addCleanup(webhook.reset_spool)
        self.snapshot_source = _SnapshotSource()
        fresh = registry.OpsProviderRegistry()
        fresh.register_signal_source(_ConfiguredWebhook())
        fresh.register_signal_source(self.snapshot_source)
        registry._registry = fresh

    @staticmethod
    def _claim(source: str = "webhook", native_id: str = _NATIVE) -> str:
        incident = store.claim(
            _signal(source=source, native_id=native_id), operating_mode=models.MODE_OBSERVE
        )
        assert incident is not None
        return incident.incident_id

    @staticmethod
    def _stored(incident_id: str) -> models.Incident:
        incident = store.get_incident(incident_id)
        assert incident is not None
        return incident


class TestRefusalRule(unittest.TestCase):
    """The rule functions both the route guard and the dispatch resolve decide with."""

    def test_a_push_only_incident_without_a_cleared_event_is_refused(self) -> None:
        incident = _incident()
        refusal = dispatch.push_only_resolve_refusal(incident, snapshot_source=False, spooled=[])
        self.assertEqual(refusal, dispatch.refusal_text(incident))
        self.assertTrue(refusal)

    def test_no_spooled_entry_is_no_evidence(self) -> None:
        """Absence, a firing entry, and a cleared entry for ANOTHER id are all no evidence.

        Fails on a rule that reads any ok entry in the spool, or that treats "no entry" as
        "nothing firing".
        """
        self.assertIsNone(dispatch.cleared_event([], _SIGNAL_ID))
        self.assertIsNone(dispatch.cleared_event([_signal()], _SIGNAL_ID))
        other = _signal(models.STATE_OK, native_id="alert/2")
        self.assertIsNone(dispatch.cleared_event([other], _SIGNAL_ID))
        self.assertTrue(
            dispatch.push_only_resolve_refusal(_incident(), snapshot_source=False, spooled=[other])
        )

    def test_the_latest_spooled_cleared_event_allows_it(self) -> None:
        cleared = _signal(models.STATE_OK)
        spooled = [_signal(), cleared]
        self.assertIs(dispatch.cleared_event(spooled, _SIGNAL_ID), cleared)
        self.assertEqual(
            dispatch.push_only_resolve_refusal(_incident(), snapshot_source=False, spooled=spooled),
            "",
        )

    def test_a_cleared_event_followed_by_a_firing_redelivery_is_refused(self) -> None:
        """The LATEST entry decides: an ok followed by a new firing is a live fault."""
        spooled = [_signal(models.STATE_OK), _signal()]
        self.assertIsNone(dispatch.cleared_event(spooled, _SIGNAL_ID))
        incident = _incident()
        self.assertEqual(
            dispatch.push_only_resolve_refusal(incident, snapshot_source=False, spooled=spooled),
            dispatch.refusal_text(incident),
        )

    def test_a_snapshot_source_is_never_refused(self) -> None:
        self.assertEqual(
            dispatch.push_only_resolve_refusal(
                _incident(source="fake"), snapshot_source=True, spooled=[]
            ),
            "",
        )

    def test_registry_reports_the_webhook_as_push_only(self) -> None:
        reg = registry.OpsProviderRegistry()
        reg.register_signal_source(_ConfiguredWebhook())
        reg.register_signal_source(_SnapshotSource())
        self.assertFalse(reg.is_snapshot_source(webhook.PROVIDER_ID))
        self.assertTrue(reg.is_snapshot_source("fake"))

    def test_an_unregistered_source_is_not_a_snapshot_source(self) -> None:
        """A source with no loaded adapter fails closed, as push-only.

        Fails on ``is_snapshot_source`` delegating straight to ``_is_snapshot``, whose poll
        side default is True for an unknown id -- asserted here too, so the fixture can tell
        the two answers apart.
        """
        reg = registry.OpsProviderRegistry()
        reg.register_signal_source(_SnapshotSource())
        self.assertFalse(reg.is_snapshot_source("acme-push"))
        self.assertTrue(reg._is_snapshot("acme-push"))
        self.assertTrue(reg.is_snapshot_source("fake"))

    def test_the_refusal_text_names_the_missing_evidence(self) -> None:
        incident = _incident()
        text = dispatch.refusal_text(incident)
        for needle in (incident.incident_id, "webhook", _SIGNAL_ID, *_REFUSAL_WORDS):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_cleared_event_resolution_is_the_exact_concatenation(self) -> None:
        """Parts A, B, C in order, one ASCII space between, nothing else.

        Fails on any other separator, on B written for a diagnosed incident, on an unstripped
        note, and on B added when the note already says it (the phrase must occur once).
        """
        cleared = _signal(models.STATE_OK)
        cases = {
            (True, ""): _PART_A,
            (True, "r"): f"{_PART_A} r",
            (True, "  r \n"): f"{_PART_A} r",
            (False, ""): f"{_PART_A} {_PART_B}",
            (False, "r"): f"{_PART_A} {_PART_B} r",
        }
        for (diagnosed, note), expected in cases.items():
            with self.subTest(diagnosed=diagnosed, note=note):
                self.assertEqual(
                    dispatch.cleared_event_resolution(cleared, note=note, diagnosed=diagnosed),
                    expected,
                )
        pass_two = "cleared without diagnosis — may recur"
        text = dispatch.cleared_event_resolution(cleared, note=pass_two, diagnosed=False)
        self.assertEqual(text, f"{_PART_A} {pass_two}")
        self.assertEqual(text.lower().count("cleared without diagnosis"), 1)


class TestStoreHooks(_Home):
    """``require_open`` and ``check`` decide on the stored record, under the index lock."""

    def test_require_open_refuses_a_terminal_incident(self) -> None:
        incident_id = self._claim()
        store.transition(incident_id, models.STATUS_RESOLVED, resolution="x")
        with self.assertRaises(store.IncidentClosed):
            store.transition(incident_id, models.STATUS_RESOLVED, require_open=True, resolution="y")
        self.assertEqual(self._stored(incident_id).resolution, "x")
        self.assertTrue(issubclass(store.IncidentClosed, ValueError))

    def test_require_open_lets_an_open_incident_close(self) -> None:
        incident_id = self._claim()
        closed = store.transition(
            incident_id, models.STATUS_RESOLVED, require_open=True, resolution="y"
        )
        self.assertEqual(closed.status, models.STATUS_RESOLVED)
        self.assertEqual(self._stored(incident_id).resolution, "y")

    def test_check_refusal_writes_nothing(self) -> None:
        incident_id = self._claim()
        store.update_fields(incident_id, diagnosis="d0")
        before = self._stored(incident_id)

        def _refuse(_stored: models.Incident) -> dict[str, Any]:
            raise store.TransitionRefused("no evidence", "code_x")

        with self.assertRaises(store.TransitionRefused) as caught:
            store.transition(
                incident_id, models.STATUS_RESOLVED, check=_refuse, diagnosis="d1", resolution="r"
            )
        self.assertEqual(
            (caught.exception.message, caught.exception.code), ("no evidence", "code_x")
        )
        after = self._stored(incident_id)
        self.assertEqual(
            (after.status, after.diagnosis, after.resolution, after.updated_at),
            (before.status, before.diagnosis, before.resolution, before.updated_at),
        )
        self.assertFalse(issubclass(store.TransitionRefused, ValueError))

    def test_check_updates_override_keyword_updates(self) -> None:
        incident_id = self._claim()
        seen: list[str] = []

        def _rewrite(stored: models.Incident) -> dict[str, Any]:
            seen.append(stored.status)
            return {"resolution": "z"}

        store.transition(incident_id, models.STATUS_RESOLVED, check=_rewrite, resolution="y")
        self.assertEqual(self._stored(incident_id).resolution, "z")
        self.assertEqual(seen, [models.STATUS_DISPATCHED])

    def test_without_the_new_keywords_a_terminal_field_edit_is_unchanged(self) -> None:
        """Pin: ``resolved -> resolved`` is the same-status field edit when no hook is given."""
        incident_id = self._claim()
        store.transition(incident_id, models.STATUS_RESOLVED, resolution="x")
        store.transition(incident_id, models.STATUS_RESOLVED, resolution="y")
        self.assertEqual(self._stored(incident_id).resolution, "y")


@web.middleware
async def _as_automation(request: web.Request, handler: Any) -> web.StreamResponse:
    """What the token-auth middleware sets after an internal-secret match."""
    request["internal_auth"] = True
    return await handler(request)


class _RecordingLock:
    """Stands in for ``webhook._queue_lock`` and says whether it is held."""

    def __init__(self) -> None:
        self.held = False

    def __enter__(self) -> _RecordingLock:
        self.held = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.held = False


class _RecordingSpool(deque[models.Signal]):
    """Stands in for ``webhook._queue`` and records, per read, whether the lock was held."""

    lock: _RecordingLock
    reads: list[bool]

    def __iter__(self) -> Iterator[models.Signal]:
        self.reads.append(self.lock.held)
        return super().__iter__()


class TestTransitionGuard(_Home):
    """``POST /incident/transition`` refuses an automated push-only resolve without evidence."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(routes, "is_app_enabled", return_value=True))
        self.audit: Any = self.enterContext(mock.patch.object(routes, "_audit"))
        self.publish: Any = self.enterContext(mock.patch.object(slack_out, "publish"))
        self.post_detail: Any = self.enterContext(mock.patch.object(slack_out, "post_detail"))
        self.incident_id = self._claim()

    async def _resolve(self, *, automated: bool = True, **body: Any) -> Any:
        app = web.Application(middlewares=[_as_automation] if automated else [])
        routes.register_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        incident_id = body.pop("incident_id", self.incident_id)
        return await client.post(
            f"{_BASE}/incident/transition",
            json={"id": incident_id, "status": models.STATUS_RESOLVED, **body},
        )

    async def _assert_refused(self, resp: Any, incident_id: str) -> None:
        self.assertEqual(resp.status, 409)
        payload = await resp.json()
        self.assertEqual(payload["code"], dispatch.RECOVERY_EVIDENCE_REQUIRED)
        self.assertEqual(self._stored(incident_id).status, models.STATUS_DISPATCHED)

    async def test_an_automated_resolve_of_a_webhook_incident_without_evidence_is_refused(
        self,
    ) -> None:
        """Fails on main, which answers 200 and stores resolved for a signal merely absent."""
        store.update_fields(self.incident_id, diagnosis="d1")
        resp = await self._resolve(resolution="r", diagnosis="d2")
        await self._assert_refused(resp, self.incident_id)
        error = (await resp.json())["error"]
        for needle in (self.incident_id, "webhook", _SIGNAL_ID, *_REFUSAL_WORDS):
            with self.subTest(needle=needle):
                self.assertIn(needle, error)
        stored = self._stored(self.incident_id)
        self.assertEqual((stored.diagnosis, stored.resolution), ("d1", ""))
        self.audit.assert_called_once_with(
            "incident_transition",
            f"{self.incident_id}->resolved",
            "denied",
            error=dispatch.RECOVERY_EVIDENCE_REQUIRED,
        )
        self.publish.assert_not_awaited()
        self.post_detail.assert_not_awaited()

    async def test_a_cleared_event_followed_by_a_firing_redelivery_is_refused(self) -> None:
        _spool(models.STATE_OK, models.STATE_FIRING)
        await self._assert_refused(await self._resolve(resolution="r"), self.incident_id)

    async def test_an_automated_resolve_with_a_latest_cleared_event_stores_the_cleared_sentence(
        self,
    ) -> None:
        """The stored resolution names the evidence, and the Slack thread gets that same text.

        Fails on main (the resolution is the caller's ``r``) and on a route that posts the
        request body to Slack while the board stores the rewritten resolution.
        """
        store.update_fields(self.incident_id, diagnosis="d1")
        _spool(models.STATE_OK)
        resp = await self._resolve(resolution="r")
        self.assertEqual(resp.status, 200)
        stored = self._stored(self.incident_id)
        self.assertEqual(stored.status, models.STATUS_RESOLVED)
        self.assertEqual(stored.resolution, f"{_PART_A} r")
        self.post_detail.assert_awaited_once()
        self.assertEqual(self.post_detail.await_args.args[1], f"{_PART_A} r")

    async def test_an_operator_resolve_of_a_webhook_incident_is_allowed(self) -> None:
        """Pin: no ``internal_auth`` (the dashboard button), no guard."""
        resp = await self._resolve(automated=False, resolution="r")
        self.assertEqual(resp.status, 200)
        stored = self._stored(self.incident_id)
        self.assertEqual((stored.status, stored.resolution), (models.STATUS_RESOLVED, "r"))

    async def test_an_automated_resolve_of_a_snapshot_incident_is_unchanged(self) -> None:
        """Pin: a registered snapshot source keeps the reconcile absence rule as it is."""
        snapshot_id = self._claim(source="fake", native_id="alarm/x")
        resp = await self._resolve(incident_id=snapshot_id, resolution="r")
        self.assertEqual(resp.status, 200)
        stored = self._stored(snapshot_id)
        self.assertEqual((stored.status, stored.resolution), (models.STATUS_RESOLVED, "r"))

    async def test_an_automated_re_resolve_of_a_resolved_webhook_incident_is_a_field_edit(
        self,
    ) -> None:
        """Pin: a terminal incident is outside the guard. Fails on a check with no terminal
        clause, which refuses this with an empty spool."""
        store.transition(self.incident_id, models.STATUS_RESOLVED, resolution="r1")
        resp = await self._resolve(resolution="r2")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._stored(self.incident_id).resolution, "r2")

    async def test_the_guard_does_not_depend_on_the_display_read(self) -> None:
        """Fails on a guard keyed on the route's ``get_incident``, which is None on an
        unreadable index (and on main, which answers 200)."""
        with mock.patch.object(store, "get_incident", return_value=None):
            resp = await self._resolve(resolution="r")
        await self._assert_refused(resp, self.incident_id)

    async def test_an_automated_resolve_of_an_incident_from_an_unloaded_source_is_refused(
        self,
    ) -> None:
        """Fails on main, and on ``is_snapshot_source`` answering True for an unknown id."""
        unloaded_id = self._claim(source="acme-push", native_id="page/9")
        await self._assert_refused(
            await self._resolve(incident_id=unloaded_id, resolution="r"), unloaded_id
        )

    async def test_the_guard_reads_the_spool_under_its_lock(self) -> None:
        """The latest-entry rule depends on order, and ``ack`` reorders mid-rotation.

        Fails on a ``peek`` that copies the spool without ``_queue_lock``.
        """
        lock = _RecordingLock()
        spool = _RecordingSpool([_signal(models.STATE_OK)])
        spool.lock = lock
        spool.reads = []
        with (
            mock.patch.object(webhook, "_queue", spool),
            mock.patch.object(webhook, "_queue_lock", lock),
        ):
            resp = await self._resolve(resolution="r")
        self.assertEqual(resp.status, 200)
        self.assertTrue(spool.reads, "the guard never read the spool")
        self.assertTrue(all(spool.reads), f"a spool read ran without its lock: {spool.reads}")

    async def test_an_unreadable_index_answers_the_store_refusal_not_200(self) -> None:
        """Pin: the locked read's failure is the route's store refusal for every class."""
        cases = {
            "unreadable": (OSError(5, "Input/output error"), 503, "dispatch_index_unreadable"),
            "corrupt": (
                models.CorruptDocumentError("Expecting value", "{", 0),
                500,
                "dispatch_index_corrupt",
            ),
        }
        for label, (failure, status, code) in cases.items():
            with self.subTest(case=label):
                with mock.patch.object(store, "_read_index_for_update", side_effect=failure):
                    resp = await self._resolve(resolution="r")
                self.assertEqual(resp.status, status)
                self.assertEqual((await resp.json())["code"], code)
                self.assertEqual(self._stored(self.incident_id).status, models.STATUS_DISPATCHED)


class TestClearedPushEvent(_Home):
    """``run_cycle`` resolves an open push-only incident on its cleared delivery, only then."""

    async def _claim_cycle(self) -> str:
        _spool(models.STATE_FIRING)
        first = await dispatch.run_cycle()
        self.assertEqual([c.incident.signal.id for c in first.claimed], [_SIGNAL_ID])
        self.assertEqual(webhook.queue_depth(), 0)
        return first.claimed[0].incident.incident_id

    def _patch_cycle_two(self) -> tuple[Any, Any, Any]:
        """The Slack mirror and the SEL writer, patched after the claiming cycle."""
        publish_all = self.enterContext(mock.patch.object(slack_out, "publish_all"))
        post_detail = self.enterContext(mock.patch.object(slack_out, "post_detail"))
        sel = self.enterContext(mock.patch.object(dispatch, "sel"))
        return publish_all, post_detail, sel

    async def test_a_cleared_event_resolves_its_open_webhook_incident(self) -> None:
        """Fails on main, where the ack drops the cleared event and the incident stays open."""
        incident_id = await self._claim_cycle()
        _spool(models.STATE_OK)
        publish_all, post_detail, sel = self._patch_cycle_two()

        result = await dispatch.run_cycle()

        stored = self._stored(incident_id)
        self.assertEqual(stored.status, models.STATUS_RESOLVED)
        self.assertEqual(stored.resolution, f"{_PART_A} {_PART_B}")
        self.assertEqual(result.resolved, [incident_id])
        self.assertEqual(result.to_dict()["resolved"], [incident_id])
        self.assertIs(result.changed, False)
        publish_all.assert_awaited_once()
        self.assertEqual([inc.incident_id for inc in publish_all.await_args.args[0]], [incident_id])
        post_detail.assert_awaited_once()
        self.assertEqual(post_detail.await_args.args[0].incident_id, incident_id)
        self.assertEqual(post_detail.await_args.args[1], f"{_PART_A} {_PART_B}")
        self.assertEqual(webhook.queue_depth(), 0)
        sel.return_value.log_api_access.assert_called_once()
        logged = sel.return_value.log_api_access.call_args.kwargs
        self.assertEqual(logged["operation"], "dispatch_cycle")
        self.assertEqual(logged["resources"], f"claimed=[] released=[] resolved={[incident_id]!r}")

    async def test_a_failed_resolve_keeps_the_cleared_event_spooled(self) -> None:
        """Fails on main (the ack drops it) and on an ack that ignores the failed set."""
        incident_id = await self._claim_cycle()
        _spool(models.STATE_OK)
        real = store.transition

        def _refuse_the_resolve(incident: str, new_status: Any, *args: Any, **kw: Any) -> Any:
            if new_status == models.STATUS_RESOLVED:
                raise OSError(5, "Input/output error")
            return real(incident, new_status, *args, **kw)

        with mock.patch.object(store, "transition", side_effect=_refuse_the_resolve):
            result = await dispatch.run_cycle()
        self.assertEqual(webhook.queue_depth(), 1)
        self.assertEqual(self._stored(incident_id).status, models.STATUS_DISPATCHED)
        self.assertEqual(result.resolved, [])

        retried = await dispatch.run_cycle()
        self.assertEqual(retried.resolved, [incident_id])
        self.assertEqual(webhook.queue_depth(), 0)

    async def test_a_cleared_event_followed_by_a_new_firing_resolves_nothing(self) -> None:
        """Pin: the latest delivery is a live fault again."""
        incident_id = await self._claim_cycle()
        _spool(models.STATE_OK, models.STATE_FIRING)
        result = await dispatch.run_cycle()
        self.assertEqual(self._stored(incident_id).status, models.STATUS_DISPATCHED)
        self.assertEqual(result.resolved, [])

    async def test_an_absent_signal_leaves_its_open_webhook_incident_open(self) -> None:
        """Pin: nothing spooled is no evidence. Fails on a resolve that runs when no entry is
        found, or when no FIRING entry is found."""
        incident_id = await self._claim_cycle()
        publish_all, post_detail, _sel = self._patch_cycle_two()

        result = await dispatch.run_cycle()

        stored = self._stored(incident_id)
        self.assertEqual((stored.status, stored.resolution), (models.STATUS_DISPATCHED, ""))
        self.assertEqual(result.resolved, [])
        publish_all.assert_not_awaited()
        post_detail.assert_not_awaited()

    async def test_a_close_between_the_read_and_the_write_is_kept(self) -> None:
        """Pin: whoever closed it first keeps their resolution, and the id is still acked.

        Fails on a resolve without ``require_open`` (a same-status edit overwrites ``by
        reconcile``) and on one that files ``IncidentClosed`` as a failure (the spool keeps
        the entry).
        """
        incident_id = await self._claim_cycle()
        snapshot = store.read_index()
        store.transition(incident_id, models.STATUS_RESOLVED, resolution="by reconcile")
        _spool(models.STATE_OK)
        _publish_all, post_detail, _sel = self._patch_cycle_two()

        with mock.patch.object(store, "read_index", return_value=snapshot):
            result = await dispatch.run_cycle()

        self.assertEqual(self._stored(incident_id).resolution, "by reconcile")
        self.assertEqual(result.resolved, [])
        post_detail.assert_not_awaited()
        self.assertEqual(webhook.queue_depth(), 0)

    async def test_a_diagnosis_written_after_the_cycle_read_is_honoured(self) -> None:
        """Fails on main (still dispatched) and on ``diagnosed`` read off the cycle's index
        copy, which predates the diagnosis and writes the may-recur caveat."""
        incident_id = await self._claim_cycle()
        snapshot = store.read_index()
        store.update_fields(incident_id, diagnosis="root cause")
        _spool(models.STATE_OK)

        with mock.patch.object(store, "read_index", return_value=snapshot):
            await dispatch.run_cycle()

        stored = self._stored(incident_id)
        self.assertEqual(stored.status, models.STATUS_RESOLVED)
        self.assertEqual(stored.resolution, _PART_A)
        self.assertEqual(stored.diagnosis, "root cause")

    async def test_a_quiet_cycle_writes_no_sel_entry(self) -> None:
        """Pin: nothing claimed, released or resolved is nothing to audit."""
        await self._claim_cycle()
        _publish_all, _post_detail, sel = self._patch_cycle_two()
        await dispatch.run_cycle()
        sel.return_value.log_api_access.assert_not_called()

    async def test_a_cleared_snapshot_signal_is_left_to_reconcile(self) -> None:
        """Pin: dispatch never resolves a snapshot source's incident, ok signal or not."""
        self.snapshot_source.signals = [_signal(source="fake", native_id="alarm/x")]
        first = await dispatch.run_cycle()
        self.assertEqual(len(first.claimed), 1)
        incident_id = first.claimed[0].incident.incident_id

        self.snapshot_source.signals = [
            _signal(models.STATE_OK, source="fake", native_id="alarm/x")
        ]
        result = await dispatch.run_cycle()

        self.assertEqual(self._stored(incident_id).status, models.STATUS_DISPATCHED)
        self.assertEqual(result.resolved, [])


if __name__ == "__main__":
    unittest.main()
