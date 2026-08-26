"""Tests for the inbound webhook — the app's only externally-reachable ingress.

This is the one adapter where a mistake is directly exploitable: everything else
POLLS a provider the operator configured, while this ACCEPTS input from whoever can
reach the port. It had no test coverage at all.

Ordered by blast radius:

1. **Fail-closed.** Disabled, or no signing secret, rejects everything — enabling
   the app must never open an unauthenticated path that manufactures board work.
2. **Forgery is refused.** Missing, wrong, truncated, and other-body signatures.
3. **Nothing unauthenticated is parsed.** Signature is checked BEFORE `json.loads`,
   and an oversized body is refused before it is hashed.
4. **Input validation.** Non-object payloads, missing titles, and field lengths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    set_top_level,
    webhook,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.routes import (
    _webhook_reject_status,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import put_secret

_SECRET = "unit-test-signing-secret"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _body(**kw: object) -> bytes:
    payload: dict[str, object] = {"title": "Disk 91% on web-3", "severity": "warning"}
    payload.update(kw)
    return json.dumps(payload).encode("utf-8")


class _Env(unittest.TestCase):
    """Isolated data home, drained queue, provider enabled + secret set.

    The home is isolated HERE rather than by a fixture: these tests live under
    ``src/``, so ``test/conftest.py`` (whose autouse fixture pins ``KIROCREW_HOME``)
    never loads for them — the sibling app tests all do the same. Without it a
    "no secret configured must reject" assertion passes only because another test
    wrote one, which is exactly what a fail-closed test must not do.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self._tmp.name
        webhook.reset_spool()

    def tearDown(self) -> None:
        webhook.reset_spool()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        self._tmp.cleanup()

    @staticmethod
    def _enable(*, secret: str | None = _SECRET) -> None:
        set_top_level("providers", {"webhook": {"enabled": True}})
        if secret is not None:
            put_secret(webhook.PROVIDER_ID, "signing_secret", secret)


class TestFailClosed(_Env):
    def test_disabled_rejects_even_a_valid_signature(self) -> None:
        """A correct signature must not be enough when the source is off."""
        put_secret(webhook.PROVIDER_ID, "signing_secret", _SECRET)
        body = _body()
        accepted, detail = webhook.enqueue(body, _sign(body))
        self.assertFalse(accepted)
        self.assertIn("not enabled", detail)

    def test_no_secret_rejects_everything(self) -> None:
        """Enabling without a secret must not become an open endpoint."""
        self._enable(secret=None)
        body = _body()
        accepted, detail = webhook.enqueue(body, _sign(body))
        self.assertFalse(accepted)
        self.assertIn("no signing secret", detail)

    def test_nothing_is_queued_by_a_rejected_delivery(self) -> None:
        self._enable()
        webhook.enqueue(_body(), "")
        self.assertEqual(webhook.queue_depth(), 0)


class TestSignature(_Env):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_valid_signature_is_accepted(self) -> None:
        body = _body(id="disk-web3")
        accepted, detail = webhook.enqueue(body, _sign(body))
        self.assertTrue(accepted, detail)
        self.assertEqual(webhook.queue_depth(), 1)

    def test_uppercase_signature_is_accepted(self) -> None:
        """Senders differ on hex case; rejecting it would be a false negative."""
        body = _body()
        accepted, _ = webhook.enqueue(body, _sign(body).upper())
        self.assertTrue(accepted)

    def test_missing_signature_is_rejected(self) -> None:
        accepted, detail = webhook.enqueue(_body(), "")
        self.assertFalse(accepted)
        self.assertEqual(detail, "signature mismatch")

    def test_wrong_signature_is_rejected(self) -> None:
        accepted, _ = webhook.enqueue(_body(), "0" * 64)
        self.assertFalse(accepted)

    def test_truncated_valid_signature_is_rejected(self) -> None:
        """A prefix must not pass — the guard against a sloppy compare."""
        body = _body()
        accepted, _ = webhook.enqueue(body, _sign(body)[:32])
        self.assertFalse(accepted)

    def test_signature_for_a_different_body_is_rejected(self) -> None:
        accepted, _ = webhook.enqueue(_body(), _sign(b'{"title":"something else"}'))
        self.assertFalse(accepted)

    def test_tampered_body_with_captured_signature_is_rejected(self) -> None:
        """The realistic attack: replay a real signature over an edited body."""
        original = _body(severity="warning")
        signature = _sign(original)
        tampered = _body(severity="critical")
        accepted, _ = webhook.enqueue(tampered, signature)
        self.assertFalse(accepted)

    def test_a_different_secret_does_not_validate(self) -> None:
        body = _body()
        accepted, _ = webhook.enqueue(body, _sign(body, "some-other-secret"))
        self.assertFalse(accepted)

    def test_verify_signature_is_constant_time(self) -> None:
        """Pin the use of compare_digest rather than ``==``."""
        import inspect

        source = inspect.getsource(webhook.verify_signature)
        self.assertIn("compare_digest", source)


class TestUnauthenticatedInputIsNeverParsed(_Env):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_malformed_json_fails_on_the_signature_first(self) -> None:
        """An unsigned body must be refused for its SIGNATURE, not its syntax.

        If the order ever inverts, the endpoint parses attacker-controlled bytes
        before establishing any trust.
        """
        accepted, detail = webhook.enqueue(b"not json at all", "")
        self.assertFalse(accepted)
        self.assertEqual(detail, "signature mismatch")

    def test_oversized_body_is_refused_before_hashing(self) -> None:
        """Size is checked before the HMAC, so a huge body costs no hash."""
        huge = b"x" * (webhook.MAX_BODY_BYTES + 1)
        accepted, detail = webhook.enqueue(huge, _sign(huge))
        self.assertFalse(accepted)
        self.assertEqual(detail, "body too large")

    def test_validly_signed_malformed_json_is_a_payload_error(self) -> None:
        raw = b"not json at all"
        accepted, detail = webhook.enqueue(raw, _sign(raw))
        self.assertFalse(accepted)
        self.assertEqual(detail, "malformed JSON")


class TestPayloadValidation(_Env):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def _send(self, raw: bytes) -> tuple[bool, str]:
        return webhook.enqueue(raw, _sign(raw))

    def test_non_object_payload_is_rejected(self) -> None:
        for raw in (b"[1,2]", b'"a string"', b"42", b"null"):
            accepted, detail = self._send(raw)
            self.assertFalse(accepted, raw)
            self.assertEqual(detail, "payload must be a JSON object", raw)

    def test_missing_title_is_rejected(self) -> None:
        """A signal with no title is an unreadable board row."""
        accepted, detail = self._send(b'{"severity":"warning"}')
        self.assertFalse(accepted)
        self.assertEqual(detail, "payload has no title")

    def test_blank_title_is_rejected(self) -> None:
        accepted, detail = self._send(b'{"title":"   "}')
        self.assertFalse(accepted)
        self.assertEqual(detail, "payload has no title")

    def test_summary_is_accepted_as_a_title(self) -> None:
        """Alertmanager-style payloads use `summary`."""
        accepted, _ = self._send(b'{"summary":"Broker unreachable"}')
        self.assertTrue(accepted)

    def test_long_fields_are_capped(self) -> None:
        raw = json.dumps(
            {"title": "t", "resource": "r" * 500, "url": "u" * 900, "id": "i" * 500}
        ).encode()
        accepted, _ = self._send(raw)
        self.assertTrue(accepted)
        signal = webhook.peek()[0]
        self.assertLessEqual(len(signal.resource), 200)
        self.assertLessEqual(len(signal.url), 500)

    def test_non_dict_labels_do_not_raise(self) -> None:
        accepted, _ = self._send(b'{"title":"t","labels":"not-a-dict"}')
        self.assertTrue(accepted)

    def test_peek_does_not_consume_the_queue(self) -> None:
        """A READ must not destroy delivered signals.

        This test used to assert the opposite (`drain` emptying the spool), which is the
        bug: `poll_all` is called by the Signals-tab read and by the claim-authorization
        check as well as by the heartbeat, so a "Poll now" click permanently destroyed
        every queued alert — signature-verified, delivered, and then silently nothing.
        """
        self._send(_body(id="a"))
        self._send(_body(id="b"))
        self.assertEqual(webhook.queue_depth(), 2)
        self.assertEqual(len(webhook.peek()), 2)
        # Still there. Twice, because idempotence is the property that makes it safe to
        # call from three different consumers.
        self.assertEqual(webhook.queue_depth(), 2)
        self.assertEqual(len(webhook.peek()), 2)
        self.assertEqual(webhook.queue_depth(), 2)

    def test_ack_removes_only_the_named_signals(self) -> None:
        """Consumption is per-id, which is what makes the per-cycle claim cap safe.

        `run_cycle` claims at most `max_claims` per cycle; if acking dropped the whole
        batch, a burst larger than the cap would lose its remainder to the very poll that
        delivered it.
        """
        self._send(_body(id="a"))
        self._send(_body(id="b"))
        self._send(_body(id="c"))
        before = {s.id for s in webhook.peek()}
        self.assertEqual(len(before), 3)

        target = sorted(before)[0]
        self.assertEqual(webhook.ack({target}), 1)
        self.assertEqual({s.id for s in webhook.peek()}, before - {target})

        # Acking nothing is a no-op, not a clear.
        self.assertEqual(webhook.ack(set()), 0)
        self.assertEqual(webhook.queue_depth(), 2)
        # Acking an id that is not spooled removes nothing.
        self.assertEqual(webhook.ack({"webhook:nope"}), 0)
        self.assertEqual(webhook.queue_depth(), 2)


class TestAlertmanagerEnvelope(_Env):
    """The v4 ``{status, alerts:[...]}`` body — the most common machine-readable alert.

    Previously rejected outright: a raw Alertmanager body carries no top-level
    ``title``/``summary``, so ``signal_from_payload`` returned None and the ingress
    answered 400 "payload has no title" — while this module's own docstring named
    Alertmanager as a supported sender.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def _send(self, payload: object) -> tuple[bool, str]:
        raw = json.dumps(payload).encode("utf-8")
        return webhook.enqueue(raw, _sign(raw))

    @staticmethod
    def _alertmanager(*, count: int = 2, status: str = "firing") -> dict[str, object]:
        return {
            "status": status,
            "commonLabels": {"cluster": "prod"},
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "alertname": "HighErrorRate",
                        "severity": "critical",
                        "instance": f"web-{i}:9100",
                    },
                    "annotations": {"summary": f"error rate high on web-{i}"},
                    "startsAt": "2026-08-01T00:00:00Z",
                    "generatorURL": "https://prom.example/graph?g0.expr=up",
                    "fingerprint": f"fp{i}",
                }
                for i in range(count)
            ],
        }

    def test_a_raw_alertmanager_body_is_accepted(self) -> None:
        accepted, _ = self._send(self._alertmanager(count=1))
        self.assertTrue(accepted)

    def test_each_alert_becomes_its_own_signal(self) -> None:
        """Alertmanager groups by design; collapsing a group loses which hosts are hit."""
        accepted, detail = self._send(self._alertmanager(count=3))
        self.assertTrue(accepted)
        self.assertIn("3 signals", detail)
        signals = webhook.peek()
        self.assertEqual(len(signals), 3)
        self.assertEqual(
            {s.resource for s in signals},
            {"web-0:9100", "web-1:9100", "web-2:9100"},
        )

    def test_the_providers_own_fingerprint_becomes_the_exact_match_key(self) -> None:
        self._send(self._alertmanager(count=1))
        signal = webhook.peek()[0]
        self.assertEqual(signal.provider_key, "webhook:fp0")

    def test_title_falls_back_through_annotations_then_alertname(self) -> None:
        self._send(
            {
                "alerts": [
                    {"labels": {"alertname": "OnlyARuleName"}},
                    {
                        "labels": {"alertname": "ignored"},
                        "annotations": {"description": "a described failure"},
                    },
                ]
            }
        )
        titles = {s.title for s in webhook.peek()}
        self.assertEqual(titles, {"OnlyARuleName", "a described failure"})

    def test_common_labels_merge_under_per_alert_labels(self) -> None:
        self._send(self._alertmanager(count=1))
        signal = webhook.peek()[0]
        self.assertEqual(signal.labels.get("cluster"), "prod")
        self.assertEqual(signal.labels.get("alertname"), "HighErrorRate")

    def test_grafana_values_are_kept_as_evidence(self) -> None:
        """Grafana sends the actual breaching numbers; they are free evidence."""
        self._send(
            {
                "alerts": [
                    {
                        "labels": {"alertname": "Latency"},
                        "values": {"A": 512.5, "B": 1},
                    }
                ]
            }
        )
        signal = webhook.peek()[0]
        self.assertIn("A=512.5", signal.labels.get("values", ""))

    def test_a_resolved_alert_arrives_as_ok_not_firing(self) -> None:
        """Without this a sender could create work but never retract it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send(self._alertmanager(count=1, status="resolved"))
        signal = webhook.peek()[0]
        self.assertEqual(signal.state, models.STATE_OK)

    def test_a_per_alert_status_overrides_the_envelope(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send(
            {
                "status": "firing",
                "alerts": [
                    {"labels": {"alertname": "A"}, "status": "resolved"},
                    {"labels": {"alertname": "B"}, "status": "firing"},
                ],
            }
        )
        by_title = {s.title: s.state for s in webhook.peek()}
        self.assertEqual(by_title["A"], models.STATE_OK)
        self.assertEqual(by_title["B"], models.STATE_FIRING)

    def test_one_malformed_alert_does_not_discard_the_rest(self) -> None:
        accepted, _ = self._send(
            {
                "alerts": [
                    {"labels": {"alertname": "good-one"}},
                    "not-a-dict",
                    {"labels": {}},  # no title derivable
                    {"labels": {"alertname": "good-two"}},
                ]
            }
        )
        self.assertTrue(accepted)
        self.assertEqual({s.title for s in webhook.peek()}, {"good-one", "good-two"})

    def test_an_alerts_group_with_nothing_usable_is_refused(self) -> None:
        accepted, detail = self._send({"alerts": [{"labels": {}}, "junk"]})
        self.assertFalse(accepted)
        self.assertEqual(detail, "payload has no title")

    def test_an_empty_alerts_list_falls_back_to_the_flat_envelope(self) -> None:
        """``alerts: []`` must not shadow a body that is otherwise valid."""
        accepted, _ = self._send({"alerts": [], "title": "a flat signal"})
        self.assertTrue(accepted)
        self.assertEqual(webhook.peek()[0].title, "a flat signal")

    def test_the_fan_out_is_bounded(self) -> None:
        """A sender must not be able to mint unbounded work in one POST."""
        huge = {
            "alerts": [
                {"labels": {"alertname": f"a{i}"}} for i in range(webhook.MAX_QUEUED_SIGNALS + 50)
            ]
        }
        accepted, _ = self._send(huge)
        self.assertTrue(accepted)
        self.assertLessEqual(webhook.queue_depth(), webhook.MAX_QUEUED_SIGNALS)

    def test_the_flat_envelope_can_now_report_a_clearance(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send({"title": "it recovered", "state": "ok"})
        self.assertEqual(webhook.peek()[0].state, models.STATE_OK)

    def test_an_unparseable_state_does_not_manufacture_firing_work(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send({"title": "who knows", "state": "banana"})
        self.assertEqual(webhook.peek()[0].state, models.STATE_UNKNOWN)


class TestProviderSideSuppressionIsHonoured(_Env):
    """Alertmanager publishes suppression two ways, and only one shape used to parse.

    The v4 webhook envelope sends a scalar ``status``. Anything relaying
    ``GET /api/v2/alerts`` sends the ``gettableAlert`` OBJECT
    ``{"state": "suppressed", "silencedBy": [...]}``, which the previous scalar-only read
    stringified — so it normalized to ``unknown`` and ``silencedBy`` was dropped entirely.
    A sender being perfectly explicit about a human having parked the alert produced a
    signal indistinguishable from garbage.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def _send(self, payload: object) -> tuple[bool, str]:
        raw = json.dumps(payload).encode("utf-8")
        return webhook.enqueue(raw, _sign(raw))

    def test_a_v2_status_object_is_read_as_suppressed(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send(
            {
                "alerts": [
                    {
                        "labels": {"alertname": "DiskFull"},
                        "status": {"state": "suppressed", "silencedBy": ["7f3a"]},
                    }
                ]
            }
        )
        signal = webhook.peek()[0]
        self.assertEqual(signal.state, models.STATE_SUPPRESSED)

    def test_silenced_by_survives_instead_of_being_stringified_away(self) -> None:
        """The attribution is what tells 'we ignored it' from 'someone silenced it'."""
        self._send(
            {
                "alerts": [
                    {
                        "labels": {"alertname": "DiskFull"},
                        "status": {"state": "suppressed", "silencedBy": ["7f3a", "9c1b"]},
                    }
                ]
            }
        )
        signal = webhook.peek()[0]
        self.assertIn("7f3a", signal.suppressed_by)
        self.assertIn("9c1b", signal.suppressed_by)
        self.assertEqual(signal.suppressed_reason, "silenced")

    def test_an_inhibition_is_reported_as_a_different_kind(self) -> None:
        """A person's silence and an alert masking another alert need different next moves."""
        self._send(
            {
                "alerts": [
                    {
                        "labels": {"alertname": "PodRestart"},
                        "status": {"state": "suppressed", "inhibitedBy": ["ClusterDown"]},
                    }
                ]
            }
        )
        signal = webhook.peek()[0]
        self.assertEqual(signal.suppressed_reason, "inhibited")
        self.assertEqual(signal.suppressed_by, "ClusterDown")

    def test_a_v4_scalar_status_still_works(self) -> None:
        """The object handling must not regress the shape Alertmanager's webhook sends."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send({"status": "firing", "alerts": [{"labels": {"alertname": "A"}}]})
        self.assertEqual(webhook.peek()[0].state, models.STATE_FIRING)

    def test_a_status_object_with_no_attribution_admits_it(self) -> None:
        """An invented owner is worse than a blank one."""
        self._send({"alerts": [{"labels": {"alertname": "A"}, "status": {"state": "suppressed"}}]})
        signal = webhook.peek()[0]
        self.assertEqual(signal.suppressed_by, "")
        self.assertEqual(signal.suppressed_reason, "")

    def test_a_status_object_carrying_an_active_state_is_still_firing(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send({"alerts": [{"labels": {"alertname": "A"}, "status": {"state": "active"}}]})
        self.assertEqual(webhook.peek()[0].state, models.STATE_FIRING)

    def test_a_malformed_silenced_by_does_not_crash_the_ingress(self) -> None:
        """Same rule as `_normalize_labels`: guard the type BEFORE indexing.

        A hand-rolled forwarder sending a bare string, or nonsense, must not be able to
        500 the one externally-reachable endpoint this app has.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        accepted, _ = self._send(
            {
                "alerts": [
                    {"labels": {"alertname": "A"}, "status": {"state": "suppressed",
                                                              "silencedBy": "bare-string"}},
                    {"labels": {"alertname": "B"}, "status": {"state": "suppressed",
                                                              "silencedBy": 17}},
                    {"labels": {"alertname": "C"}, "status": "suppressed"},
                ]
            }
        )
        self.assertTrue(accepted)
        by_title = {s.title: s for s in webhook.peek()}
        self.assertEqual(by_title["A"].suppressed_by, "bare-string")
        self.assertEqual(by_title["A"].state, models.STATE_SUPPRESSED)
        self.assertEqual(by_title["B"].suppressed_by, "17")
        self.assertEqual(by_title["C"].state, models.STATE_SUPPRESSED)
        self.assertEqual(by_title["C"].suppressed_by, "")

    def test_the_attribution_is_bounded(self) -> None:
        """It reaches the board and a model prompt, so a sender must not send a novel."""
        self._send(
            {
                "alerts": [
                    {
                        "labels": {"alertname": "A"},
                        "status": {"state": "suppressed", "silencedBy": ["x" * 5000]},
                    }
                ]
            }
        )
        self.assertLessEqual(len(webhook.peek()[0].suppressed_by), webhook.MAX_SUPPRESSION_TEXT)

    def test_the_flat_envelope_can_also_report_a_suppression(self) -> None:
        """Zabbix and Icinga do not speak Alertmanager's shape; a forwarder needs this door."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send(
            {
                "title": "parked for maintenance",
                "state": "suppressed",
                "suppressed_by": "maintenance-window-4",
                "suppressed_reason": "silenced",
            }
        )
        signal = webhook.peek()[0]
        self.assertEqual(signal.state, models.STATE_SUPPRESSED)
        self.assertEqual(signal.suppressed_by, "maintenance-window-4")

    def test_an_envelope_status_object_does_not_leak_into_every_alert(self) -> None:
        """Per-alert status wins, and an alert with none falls back to the envelope TEXT.

        Guards the shape of the fallback: the envelope status is read as a scalar string,
        so an alert carrying no status of its own must not silently inherit a stringified
        dict and become `unknown`.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self._send(
            {
                "status": "firing",
                "alerts": [
                    {"labels": {"alertname": "A"}},
                    {"labels": {"alertname": "B"}, "status": {"state": "suppressed"}},
                ],
            }
        )
        by_title = {s.title: s.state for s in webhook.peek()}
        self.assertEqual(by_title["A"], models.STATE_FIRING)
        self.assertEqual(by_title["B"], models.STATE_SUPPRESSED)


class TestRejectStatusMapping(unittest.TestCase):
    """A payload fault is not an auth failure.

    Everything used to return 401, so a sender debugging a bad body was told
    "Unauthorized" and would re-check credentials that were fine — while a genuine
    signature failure looked identical to a typo.
    """

    def test_trust_failures_are_401(self) -> None:
        for detail in (
            "webhook source is not enabled",
            "no signing secret configured",
            "signature mismatch",
        ):
            self.assertEqual(_webhook_reject_status(detail), 401, detail)

    def test_payload_faults_are_400(self) -> None:
        for detail in (
            "malformed JSON",
            "payload must be a JSON object",
            "payload has no title",
        ):
            self.assertEqual(_webhook_reject_status(detail), 400, detail)

    def test_oversized_body_is_413(self) -> None:
        self.assertEqual(_webhook_reject_status("body too large"), 413)

    def test_unknown_reason_defaults_to_401(self) -> None:
        """A newly-added rejection must not be advertised as 'request was fine'."""
        self.assertEqual(_webhook_reject_status("something new"), 401)

    def test_every_enqueue_rejection_reason_is_mapped(self) -> None:
        """Derived from the source so a new reason cannot silently default.

        Catches the case where someone adds a rejection to ``enqueue`` and forgets
        that its status has to be classified.
        """
        import inspect
        import re

        source = inspect.getsource(webhook.enqueue)
        reasons = set(re.findall(r'return False, "([^"]+)"', source))
        self.assertTrue(reasons, "no literal rejection reasons found")
        known = _WEBHOOK_KNOWN_REASONS
        unmapped = reasons - known
        self.assertFalse(
            unmapped,
            f"unclassified webhook rejection reason(s): {sorted(unmapped)} — add them to "
            "_WEBHOOK_AUTH_REJECTIONS or the payload/size branches in "
            "_webhook_reject_status, and to this test's known set",
        )


#: Every rejection reason ``enqueue`` can return, with its status deliberately
#: chosen. Kept next to the test that enforces completeness.
_WEBHOOK_KNOWN_REASONS = frozenset(
    {
        "webhook source is not enabled",
        "no signing secret configured",
        "body too large",
        "signature mismatch",
        "malformed JSON",
        "payload must be a JSON object",
        "payload has no title",
    }
)


if __name__ == "__main__":
    unittest.main()


class _IngestsOnMembershipTest(set):
    """A set of acked ids that simulates a concurrent `enqueue` landing mid-`ack`.

    Hooks `__contains__`, NOT the deque. Both plausible implementations differ in how they
    TRAVERSE the spool — the racy one iterates and then `clear()`s, the fixed one
    `popleft()`s — so a harness hooked to either traversal fires for one and silently no-ops
    for the other. Hooking `signal.id in signal_ids`, the one operation both perform per
    entry, puts the delivery inside the window either way.

    This matters: the first version of this test hooked `popleft` only, so against the racy
    code it failed with "the interleaving did not happen" rather than with the data loss —
    green for the wrong reason on the very bug it names, which is the vacuous-guard trap.

    Against the racy implementation these tests fail with `RuntimeError: deque mutated during
    iteration`, which is a STRONGER finding than the reported data loss: a delivery landing
    mid-`ack` did not merely disappear, it raised out of `run_cycle` and took the heartbeat
    cycle with it.

    A set subclass rather than `mock.patch.object`: `collections.deque.popleft` and
    `set.__contains__` are read-only C slots and cannot be patched on an instance.
    """

    def __init__(self, ids, on_test, limit=1):
        super().__init__(ids)
        self._on_test = on_test
        self._limit = limit
        self.fired = 0

    def __contains__(self, item):
        if self.fired < self._limit:
            self.fired += 1
            self._on_test(self.fired)
        return super().__contains__(item)


class TestAckDoesNotRaceIngestion(_Env):
    """`ack` must not drop a delivery that arrives while it runs.

    `enqueue` executes in a WORKER THREAD — the webhook route awaits
    `asyncio.to_thread(webhook.enqueue, ...)` — so it genuinely interleaves with the
    heartbeat's `ack`. The first implementation built a `keep` list, called `_queue.clear()`
    and re-extended: anything appended between the snapshot and the clear was destroyed. A
    signature-verified, 200-accepted alert vanishing with no incident and no trace is the same
    failure class the peek/ack split was introduced to fix. Found in review.
    """

    @staticmethod
    def _signal(native_id):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        return Signal.create(
            source="webhook", native_id=native_id, title=f"{native_id} broke"
        )

    def test_a_delivery_during_ack_survives(self):
        for native in ("a", "b"):
            webhook._queue.append(self._signal(native))
        target = webhook.peek()[0].id
        late = self._signal("late")

        acked = _IngestsOnMembershipTest(
            {target}, lambda _n: webhook._queue.append(late)
        )
        removed = webhook.ack(acked)

        self.assertEqual(acked.fired, 1, "the interleaving under test did not happen")
        self.assertEqual(removed, 1)
        remaining = {s.id for s in webhook.peek()}
        self.assertIn(
            late.id,
            remaining,
            "a signature-verified delivery that arrived during ack was destroyed",
        )
        self.assertNotIn(target, remaining)

    def test_ack_terminates_when_ingestion_outruns_it(self):
        """The loop is bounded by the length observed at ENTRY, so a sender appending on every
        iteration cannot spin it forever."""
        for n in range(3):
            webhook._queue.append(self._signal(f"s{n}"))
        target = webhook.peek()[0].id

        acked = _IngestsOnMembershipTest(
            {target},
            lambda n: webhook._queue.append(self._signal(f"x{n}")),
            limit=50,
        )
        removed = webhook.ack(acked)

        self.assertEqual(removed, 1)
        self.assertNotIn(target, {s.id for s in webhook.peek()})

    def test_enqueue_during_ack_at_maxlen_evicts_nothing(self):
        """At `maxlen`, `ack`'s popleft-then-append is a two-step window. A worker-thread
        `enqueue` landing inside it filled the deque, and `ack`'s own append then evicted the
        OLDEST — which could be the alert `enqueue` just accepted. The `_queue_lock` around
        both compound operations closes that; here the lock is exercised directly, and the
        invariant asserted is that no already-spooled unclaimed signal is silently lost.

        Serialised, not raced: with `_queue_lock` held for the whole rotation, an `enqueue`
        cannot interleave mid-`ack`, so the maxlen overflow that dropped a signal cannot form.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook as wh

        # Fill the spool to exactly maxlen with distinct unclaimed signals.
        cap = wh.MAX_QUEUED_SIGNALS
        for n in range(cap):
            wh._queue.append(self._signal(f"full-{n}"))
        self.assertEqual(wh.queue_depth(), cap)
        before = {s.id for s in wh.peek()}

        # Ack a signal that is NOT present: a no-op rotation over a full deque, the exact
        # popleft/append churn that overflowed. Nothing should be dropped.
        removed = wh.ack({"webhook:not-here"})
        self.assertEqual(removed, 0)
        self.assertEqual({s.id for s in wh.peek()}, before, "a full-spool ack dropped a signal")

        # And enqueue holds the same lock, so its extend cannot interleave a partial rotation.
        import inspect

        # Assert the LOCK STATEMENT, not just the identifier: a comment mentioning
        # `_queue_lock` would satisfy a substring check while the guard was gone.
        self.assertIn("with _queue_lock:", inspect.getsource(wh.enqueue))
        self.assertIn("with _queue_lock:", inspect.getsource(wh.ack))


class TestTheBodyIsCappedWhileStreaming(unittest.IsolatedAsyncioTestCase):
    """The size refusal has to be a MEMORY bound, not just a verdict.

    `enqueue`'s `len(raw_body) > MAX_BODY_BYTES` check can only run on a body already in
    memory, and these routes register on the shared gateway application whose
    `client_max_size` is 60 MiB (it carries file uploads) — so `await request.read()`
    buffered up to 60 MiB per concurrent delivery in order to refuse 256 KiB of it. The
    route now reads incrementally and stops one byte past the cap.

    Asserted on BYTES ACTUALLY READ rather than on the status code, because a handler that
    buffers everything and then returns 413 passes a status-only test while the exhaustion
    it is supposed to prevent still happens. Found in review (GPT 5.6).
    """

    class _Stream:
        """Minimal `request.content` that counts what the handler consumed."""

        def __init__(self, total: int, chunk: int) -> None:
            self._remaining = total
            self._chunk = chunk
            self.served = 0

        async def read(self, size: int = -1) -> bytes:
            if self._remaining <= 0:
                return b""
            n = min(self._chunk if size is None or size < 0 else size, self._remaining)
            self._remaining -= n
            self.served += n
            return b"x" * n

    def _request(self, stream, content_length=None):
        from unittest import mock

        from aiohttp import web

        request = mock.MagicMock(spec=web.Request)
        request.content = stream
        # `spec=web.Request` makes `content_length` a Mock, which is truthy and not an int;
        # set it explicitly so the header pre-check sees a real value or None.
        request.content_length = content_length
        return request

    async def test_an_oversized_body_is_not_fully_buffered(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.routes import _read_capped

        cap = webhook.MAX_BODY_BYTES
        oversize = cap * 40  # a 10 MiB delivery against a 256 KiB cap
        stream = self._Stream(oversize, webhook.READ_CHUNK_BYTES)
        self.assertIsNone(await _read_capped(self._request(stream), cap))
        self.assertLessEqual(
            stream.served,
            cap + webhook.READ_CHUNK_BYTES,
            "the whole body was buffered before the cap was enforced",
        )
        self.assertLess(stream.served, oversize, "the handler drained the entire body")

    async def test_a_declared_oversize_length_is_refused_before_any_read(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.routes import _read_capped

        cap = webhook.MAX_BODY_BYTES
        stream = self._Stream(cap * 40, webhook.READ_CHUNK_BYTES)
        result = await _read_capped(self._request(stream, content_length=cap * 40), cap)
        self.assertIsNone(result)
        self.assertEqual(stream.served, 0, "an honest oversized delivery still read bytes")

    async def test_a_body_exactly_at_the_cap_is_accepted_whole(self):
        """Off-by-one in the other direction: the limit itself must still work."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.routes import _read_capped

        cap = webhook.MAX_BODY_BYTES
        stream = self._Stream(cap, webhook.READ_CHUNK_BYTES)
        body = await _read_capped(self._request(stream, content_length=cap), cap)
        self.assertIsNotNone(body)
        assert body is not None
        self.assertEqual(len(body), cap)

    async def test_a_lying_content_length_does_not_defeat_the_cap(self):
        """The streaming count is the authority; the header is only a fast path."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.routes import _read_capped

        cap = webhook.MAX_BODY_BYTES
        stream = self._Stream(cap * 40, webhook.READ_CHUNK_BYTES)
        # Claims to be tiny, actually sends 10 MiB.
        self.assertIsNone(await _read_capped(self._request(stream, content_length=10), cap))
        self.assertLessEqual(stream.served, cap + webhook.READ_CHUNK_BYTES)

    async def test_a_small_body_round_trips(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.routes import _read_capped

        payload = _body()
        stream = self._Stream(len(payload), webhook.READ_CHUNK_BYTES)
        body = await _read_capped(self._request(stream, content_length=len(payload)), 1024)
        self.assertIsNotNone(body)
        assert body is not None
        self.assertEqual(len(body), len(payload))


class TestAFullSpoolIsRefusedNotSilentlyEvicted(_Env):
    """HTTP 200 must mean the alert is on the board, not "accepted and then dropped".

    The spool is a `deque(maxlen=MAX_QUEUED_SIGNALS)`, so `extend` past capacity silently
    evicts the OLDEST entries. Under a burst — 201 signed deliveries before a dispatch cycle
    drains — every sender got a 2xx while the earliest accepted alerts were discarded, so an
    alert was paged, acknowledged as received, and then simply never appeared. Bounding the
    spool is right; lying about the outcome is not. Found in review (GPT 5.6).

    Refusing is strictly better than evicting because webhook senders RETRY: Alertmanager
    re-delivers on a 5xx, so a full spool becomes a delay rather than a lost page.
    """

    def setUp(self) -> None:
        super().setUp()
        set_top_level("providers", {webhook.PROVIDER_ID: {"enabled": True}})
        put_secret(webhook.PROVIDER_ID, "signing_secret", _SECRET)

    def _send(self, n: int) -> tuple[bool, str]:
        raw = json.dumps({"title": f"alert {n}", "id": f"a{n}"}).encode("utf-8")
        return webhook.enqueue(raw, _sign(raw))

    def test_capacity_is_never_exceeded_and_nothing_is_evicted(self) -> None:
        accepted = 0
        rejected: list[str] = []
        for n in range(webhook.MAX_QUEUED_SIGNALS + 5):
            ok, detail = self._send(n)
            if ok:
                accepted += 1
            else:
                rejected.append(detail)

        self.assertEqual(accepted, webhook.MAX_QUEUED_SIGNALS)
        self.assertEqual(webhook.queue_depth(), webhook.MAX_QUEUED_SIGNALS)
        self.assertEqual(len(rejected), 5)
        self.assertEqual(set(rejected), {webhook.REJECT_SPOOL_FULL})
        # The invariant that fails against a silent eviction: EVERY accepted signal is still
        # present. A `maxlen` drop keeps the depth at the cap too, so depth alone proves nothing.
        spooled = {s.id for s in webhook.peek()}
        expected = {
            f"{webhook.PROVIDER_ID}:a{n}" for n in range(webhook.MAX_QUEUED_SIGNALS)
        }
        self.assertEqual(
            spooled,
            expected,
            "an accepted signal is missing from the spool — it was evicted after a 2xx",
        )

    def test_the_refusal_is_a_retriable_503(self) -> None:
        """4xx would tell a sender to stop; the delivery was fine and WE are full."""
        for n in range(webhook.MAX_QUEUED_SIGNALS):
            self._send(n)
        ok, detail = self._send(9999)
        self.assertFalse(ok)
        self.assertEqual(_webhook_reject_status(detail), 503)

    def test_a_multi_alert_delivery_is_all_or_nothing(self) -> None:
        """A partially-accepted Alertmanager fan-out would report success for dropped alerts."""
        # Leave room for exactly two, then send a three-alert delivery.
        for n in range(webhook.MAX_QUEUED_SIGNALS - 2):
            self._send(n)
        depth_before = webhook.queue_depth()
        raw = json.dumps(
            {
                "alerts": [
                    {"status": "firing", "labels": {"alertname": f"multi-{i}"}} for i in range(3)
                ]
            }
        ).encode("utf-8")
        ok, detail = webhook.enqueue(raw, _sign(raw))
        self.assertFalse(ok)
        self.assertEqual(detail, webhook.REJECT_SPOOL_FULL)
        self.assertEqual(
            webhook.queue_depth(), depth_before, "a refused delivery still spooled something"
        )

    def test_capacity_is_checked_under_the_lock(self) -> None:
        """Structural: checking BEFORE the lock is a TOCTOU that reopens the eviction.

        A concurrent delivery can fill the last slot between the test and the `extend`, which
        is the very race the lock exists to close — so the check has to be inside it.
        """
        import inspect

        source = inspect.getsource(webhook.enqueue)
        body = source.split("with _queue_lock:", 1)
        self.assertEqual(len(body), 2, "enqueue no longer takes the queue lock")
        self.assertIn(
            "MAX_QUEUED_SIGNALS",
            body[1],
            "the capacity check is outside the lock — a concurrent delivery can fill the "
            "last slot between the check and the extend",
        )
