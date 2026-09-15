"""Datadog adapter paths the provider-seam suite never reaches.

``test_providers.py`` covers the mute/comment bodies and the ``site`` allowlist.
What is left uncovered is everything that decides whether a call happens at all:
the unconfigured short-circuits on ``poll`` / ``execute`` / ``gather``, the
monitor-filter params, the three ``_poll_sync`` skip guards, and the whole
evidence source. Those are the guards that keep an unconfigured provider from
issuing credential-less requests, so they are worth pinning.

Everything is patched at the module attribute (``datadog.request_json``,
``datadog.get_secret``, …) — no network, no secrets store, no config file.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_COMMENT,
    ACTION_SILENCE,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    EvidenceBudget,
    TruncatedSignals,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import HttpError


def _signal(monitor_id: str = "123", source: str = datadog.PROVIDER_ID) -> Signal:
    labels = {"dd_monitor_id": monitor_id} if monitor_id else {}
    return Signal.create(
        source=source, native_id=f"monitor/{monitor_id}", title="cpu high", labels=labels
    )


def _monitor(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "name": "cpu high",
        "overall_state": "Alert",
        "overall_state_modified": "2026-01-01T00:00:00Z",
        "query": "avg(last_5m):avg:system.cpu.user{*} > 90",
        "tags": ["team:core", "env:prod"],
    }
    base.update(over)
    return base


def _configured(flag: bool):
    """Patch both halves of ``configured()`` plus the secret reads behind ``_headers``."""
    return (
        mock.patch.object(datadog, "provider_enabled", return_value=flag),
        mock.patch.object(datadog, "has_secrets", return_value=flag),
        mock.patch.object(datadog, "get_secret", return_value="k"),
    )


class TestConfiguredGate(unittest.IsolatedAsyncioTestCase):
    """An unconfigured provider must not issue a request at all."""

    async def test_poll_short_circuits_without_touching_the_api(self):
        enabled, secrets, secret = _configured(False)
        with enabled, secrets, secret, mock.patch.object(datadog, "request_json") as req:
            self.assertEqual(await datadog.DatadogAdapter().poll(), [])
        req.assert_not_called()

    async def test_execute_refuses_with_a_reason_the_board_can_show(self):
        enabled, secrets, secret = _configured(False)
        with enabled, secrets, secret, mock.patch.object(datadog, "request_json") as req:
            result = await datadog.DatadogAdapter().execute(_signal(), ACTION_SILENCE, {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "datadog is not configured")
        req.assert_not_called()

    async def test_a_signal_without_a_monitor_id_is_refused_not_guessed(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(datadog, "request_json") as req:
            result = await datadog.DatadogAdapter().execute(_signal(""), ACTION_SILENCE, {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "signal carries no Datadog id")
        req.assert_not_called()

    async def test_execute_reaches_the_sync_path_when_configured(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "request_json", return_value={}
        ) as req:
            result = await datadog.DatadogAdapter().execute(
                _signal("42"), ACTION_COMMENT, {"note": "looked at it"}
            )
        self.assertTrue(result.ok)
        # A comment is posted to the events endpoint, tagged with the monitor id.
        self.assertTrue(req.call_args.args[0].endswith("/api/v1/events"), req.call_args.args[0])
        body = req.call_args.kwargs["body"]
        self.assertIn("42", body["title"])
        self.assertIn("monitor_id:42", body["tags"])
        self.assertEqual(body["text"], "looked at it")


class TestPollFilteringAndSkips(unittest.IsolatedAsyncioTestCase):
    async def test_tag_and_id_filters_are_forwarded_as_comma_joined_params(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "config_list", side_effect=[["team:core", "env:prod"], ["7", "8"]]
        ), mock.patch.object(datadog, "request_json", return_value=[]) as req:
            self.assertEqual(await datadog.DatadogAdapter().poll(), [])
        params = req.call_args.kwargs["params"]
        self.assertEqual(params["monitor_tags"], "team:core,env:prod")
        self.assertEqual(params["id"], "7,8")
        self.assertEqual(params["page_size"], datadog.POLL_FETCH_LIMIT)

    def test_non_dict_closed_and_id_less_monitors_are_all_skipped(self):
        monitors = [
            "not-a-dict",
            _monitor(overall_state="OK"),
            _monitor(overall_state="No Data"),
            _monitor(id=""),
            _monitor(id=9, overall_state="Warn", tags="not-a-list"),
        ]
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "config_list", return_value=[]
        ), mock.patch.object(datadog, "request_json", return_value=monitors):
            signals = datadog.DatadogAdapter()._poll_sync()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].labels["dd_monitor_id"], "9")
        self.assertEqual(signals[0].severity, SEVERITY_WARNING)
        # A non-list ``tags`` degrades to an empty label, not a crash.
        self.assertEqual(signals[0].labels["tags"], "")

    def test_an_alert_monitor_becomes_a_critical_signal_with_a_gated_url(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "config_list", return_value=[]
        ), mock.patch.object(datadog, "request_json", return_value=[_monitor()]):
            signals = datadog.DatadogAdapter()._poll_sync()
        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig.severity, SEVERITY_CRITICAL)
        # ``Signal.create`` namespaces the provider's own key with its source.
        self.assertEqual(sig.provider_key, f"{datadog.PROVIDER_ID}:monitor/7")
        self.assertEqual(sig.resource, "avg(last_5m):avg:system.cpu.user{*} > 90")
        self.assertEqual(sig.fired_at, "2026-01-01T00:00:00Z")
        self.assertEqual(sig.labels["tags"], "team:core,env:prod")
        self.assertTrue(sig.url.endswith("/monitors/7"), sig.url)
        self.assertNotIsInstance(signals, TruncatedSignals)

    def test_a_non_list_payload_is_read_as_an_empty_estate(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "config_list", return_value=[]
        ), mock.patch.object(datadog, "request_json", return_value={"errors": ["nope"]}):
            self.assertEqual(datadog.DatadogAdapter()._poll_sync(), [])


class TestExecuteErrorMapping(unittest.TestCase):
    def test_an_http_error_becomes_a_failed_action_result_not_an_exception(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "request_json", side_effect=HttpError(403, "403 forbidden")
        ):
            result = datadog.DatadogAdapter()._execute_sync("123", ACTION_SILENCE, {})
        self.assertFalse(result.ok)
        self.assertEqual(result.action, ACTION_SILENCE)
        self.assertIn("403", str(result.error))


class TestEvidenceSource(unittest.IsolatedAsyncioTestCase):
    async def test_gather_skips_an_unconfigured_provider(self):
        enabled, secrets, secret = _configured(False)
        with enabled, secrets, secret, mock.patch.object(datadog, "request_json") as req:
            out = await datadog.DatadogEvidenceSource().gather(_signal(), EvidenceBudget())
        self.assertEqual(out, [])
        req.assert_not_called()

    async def test_gather_skips_a_signal_from_another_source(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(datadog, "request_json") as req:
            out = await datadog.DatadogEvidenceSource().gather(
                _signal(source="cloudwatch"), EvidenceBudget()
            )
        self.assertEqual(out, [])
        req.assert_not_called()

    async def test_gather_returns_monitor_context_bounded_by_the_byte_budget(self):
        enabled, secrets, secret = _configured(True)
        payload = [{"downtime": "x" * 500}]
        with enabled, secrets, secret, mock.patch.object(
            datadog, "request_json", return_value=payload
        ) as req:
            out = await datadog.DatadogEvidenceSource().gather(
                _signal("55"), EvidenceBudget(max_bytes=40)
            )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source, "datadog-evidence")
        self.assertEqual(out[0].kind, "monitor_context")
        self.assertIn("55", out[0].title)
        self.assertEqual(len(out[0].body), 40)
        self.assertTrue(out[0].url.endswith("/monitors/55"), out[0].url)
        # The lookback window is sent as an explicit from/to pair.
        params = req.call_args.kwargs["params"]
        self.assertEqual(params["to_ts"] - params["from_ts"], datadog._METRIC_LOOKBACK_SECS)

    def test_a_signal_without_a_monitor_id_gathers_nothing(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(datadog, "request_json") as req:
            out = datadog.DatadogEvidenceSource()._gather_sync(_signal(""), EvidenceBudget())
        self.assertEqual(out, [])
        req.assert_not_called()

    def test_an_http_error_is_swallowed_into_no_evidence(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "request_json", side_effect=HttpError(500, "boom")
        ):
            out = datadog.DatadogEvidenceSource()._gather_sync(_signal("9"), EvidenceBudget())
        self.assertEqual(out, [])

    def test_an_empty_payload_yields_no_evidence_rather_than_a_blank_card(self):
        enabled, secrets, secret = _configured(True)
        with enabled, secrets, secret, mock.patch.object(
            datadog, "request_json", return_value=[]
        ):
            out = datadog.DatadogEvidenceSource()._gather_sync(_signal("9"), EvidenceBudget())
        self.assertEqual(out, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
