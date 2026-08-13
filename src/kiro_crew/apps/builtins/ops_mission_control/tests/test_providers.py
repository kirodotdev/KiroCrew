"""Tests for the provider seam: ADD-only registry, fan-out resilience, adapters.

The ADD-only test is the one that protects the edition boundary: if a companion
could replace a core adapter, auditing what the public core does would require
auditing every companion too.

The fan-out tests cover the property that keeps the heartbeat alive — one broken
or hanging provider must degrade to a per-source error, never take down the poll.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_COMMENT,
    ACTION_RESOLVE,
    ACTION_SILENCE,
    DEFAULT_SILENCE_SECS,
    VALID_ACTIONS,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    ActionResult,
    Evidence,
    EvidenceBudget,
    ShiftStatus,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import (
    OpsProviderRegistry,
)


def _signal(source="test", native_id="x", title="broke") -> Signal:
    return Signal.create(source=source, native_id=native_id, title=title)


class _FakeSource:
    def __init__(self, source_id, signals=None, *, fail=False, hang=False, ready=True):
        self.id = source_id
        self.display_name = source_id
        self._signals = signals or []
        self._fail = fail
        self._hang = hang
        self._ready = ready

    def configured(self):
        return self._ready

    async def poll(self):
        if self._fail:
            raise RuntimeError("provider exploded")
        if self._hang:
            await asyncio.sleep(60)
        return self._signals


class TestAddOnlyRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_first_registration_wins(self):
        registry = OpsProviderRegistry()
        core = _FakeSource("cloudwatch")
        companion = _FakeSource("cloudwatch")
        self.assertTrue(registry.register_signal_source(core))
        self.assertFalse(registry.register_signal_source(companion))
        self.assertIs(registry.signal_sources()[0], core)

    async def test_adapter_without_id_refused(self):
        registry = OpsProviderRegistry()
        self.assertFalse(registry.register_signal_source(_FakeSource("")))

    async def test_companion_can_add_a_new_id(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("cloudwatch"))
        self.assertTrue(registry.register_signal_source(_FakeSource("internal-tracker")))
        self.assertEqual(len(registry.signal_sources()), 2)


class TestPollFanOut(unittest.IsolatedAsyncioTestCase):
    async def test_failing_source_does_not_suppress_others(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("good", [_signal()]))
        registry.register_signal_source(_FakeSource("bad", fail=True))
        signals, errors = await registry.poll_all()
        self.assertEqual(len(signals), 1)
        self.assertIn("bad", errors)

    async def test_hanging_source_times_out_and_is_reported(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("good", [_signal()]))
        registry.register_signal_source(_FakeSource("slow", hang=True))
        signals, errors = await registry.poll_all(timeout_secs=0.05)
        self.assertEqual(len(signals), 1)
        self.assertIn("timed out", errors["slow"])

    async def test_unconfigured_source_is_skipped_silently(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("off", [_signal()], ready=False))
        signals, errors = await registry.poll_all()
        self.assertEqual(signals, [])
        self.assertEqual(errors, {})

    async def test_per_source_limit_is_enforced(self):
        registry = OpsProviderRegistry()
        many = [_signal(native_id=str(n)) for n in range(50)]
        registry.register_signal_source(_FakeSource("noisy", many))
        signals, _ = await registry.poll_all(limit=5)
        self.assertEqual(len(signals), 5)

    async def test_no_sources_is_not_an_error(self):
        signals, errors = await OpsProviderRegistry().poll_all()
        self.assertEqual((signals, errors), ([], {}))


class TestPollHealthAndBackoff(unittest.IsolatedAsyncioTestCase):
    """Absence of a signal is only evidence when the poll actually succeeded.

    Without this distinction a 429 or a provider outage reads exactly like "everything
    cleared", and the reconcile SOP closes live incidents with a resolution string that
    asserts something false. ``resolved`` is terminal, so that work does not come back.
    """

    async def test_a_successful_poll_is_recorded_as_healthy(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("good", [_signal()]))
        await registry.poll_all()
        health = registry.poll_health()
        self.assertTrue(health["good"]["ok"])
        self.assertEqual(health["good"]["signals"], 1)

    async def test_a_truncated_poll_is_not_reported_as_a_complete_snapshot(self):
        """The cap is fine; calling a capped poll authoritative is not.

        `poll_all` slices each source's result to `limit`, and the omitted signals are simply
        ABSENT from that poll. For a snapshot source, absence is how `reconcile` and
        `verify_pending_actions` infer recovery — so a provider returning limit+1 firing alarms
        had the surplus verify as CLEARED while they were still firing, and `resolved` is
        terminal. Reported through the existing `snapshot=False` channel (built for the webhook
        spool's drain) because it is the same fact: this poll did not see everything.
        Found in review (GPT 5.6).

        Fails against a `poll_all` that slices and still reports `snapshot: True`.
        """
        registry = OpsProviderRegistry()
        registry.register_signal_source(
            _FakeSource("flood", [_signal(native_id=f"a{i}") for i in range(7)])
        )
        signals, _ = await registry.poll_all(limit=5)
        health = registry.poll_health()["flood"]

        self.assertEqual(len(signals), 5, "the cap must still bound the result")
        self.assertTrue(health["ok"], "a truncated poll still SUCCEEDED")
        self.assertFalse(
            health["snapshot"],
            "a truncated poll was reported as a complete snapshot, so the omitted signals "
            "would be read as recovered",
        )
        self.assertIn("5", health["detail"], "the operator is told the poll was capped")

    async def test_an_uncapped_poll_is_still_a_snapshot(self):
        """The fix must not mark every poll non-authoritative — absence has to stay usable."""
        registry = OpsProviderRegistry()
        registry.register_signal_source(
            _FakeSource("normal", [_signal(native_id=f"a{i}") for i in range(3)])
        )
        await registry.poll_all(limit=5)
        health = registry.poll_health()["normal"]
        self.assertTrue(health["snapshot"])
        self.assertEqual(health["detail"], "")

    async def test_a_failed_poll_is_recorded_as_unhealthy_with_the_reason(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("bad", fail=True))
        await registry.poll_all()
        health = registry.poll_health()
        self.assertFalse(health["bad"]["ok"])
        self.assertIn("exploded", health["bad"]["detail"])

    async def test_a_quiet_source_is_distinguishable_from_a_broken_one(self):
        """The whole point: both contribute zero signals, only one means 'cleared'."""
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("quiet", []))
        registry.register_signal_source(_FakeSource("broken", fail=True))
        signals, _ = await registry.poll_all()
        self.assertEqual(signals, [])
        health = registry.poll_health()
        self.assertTrue(health["quiet"]["ok"])
        self.assertFalse(health["broken"]["ok"])

    async def test_a_failed_source_is_skipped_on_the_next_cycle(self):
        """A provider shedding load must not be re-polled at full rate every 120s."""
        registry = OpsProviderRegistry()
        source = _FakeSource("flaky", fail=True)
        registry.register_signal_source(source)
        await registry.poll_all()
        # It would now succeed, but we are in backoff and must not even call it.
        source._fail = False
        source._signals = [_signal()]
        signals, errors = await registry.poll_all()
        self.assertEqual(signals, [])
        self.assertIn("backing off", errors["flaky"])

    async def test_a_timing_out_source_backs_off_too(self):
        """The one failure mode that never armed a window, and the most expensive one.

        `asyncio.TimeoutError` IS an `Exception`, so its own `except` clause shadowed the
        generic one that calls `_note_backoff` — a hung provider was therefore re-polled on
        every heartbeat, burning the FULL per-source timeout out of each one for as long as
        it stayed hung. A failure that returns fast costs a socket; this one costs 15s of
        every cycle.
        """
        registry = OpsProviderRegistry()

        class _Hangs:
            id = "hangs"
            display_name = "hangs"

            def configured(self):
                return True

            async def poll(self):
                await asyncio.sleep(30)
                return []

        registry.register_signal_source(_Hangs())
        _signals, errors = await registry.poll_all(timeout_secs=0.05)
        self.assertIn("timed out", errors["hangs"])
        self.assertIn("hangs", registry._backoff_until)
        # And the next cycle skips it instead of paying the timeout again.
        _signals, errors = await registry.poll_all(timeout_secs=0.05)
        self.assertIn("backing off", errors["hangs"])

    async def test_a_push_spool_is_not_a_snapshot_so_absence_proves_nothing(self):
        """`ok` answers "did we look", not "did we see everything" — and those differ.

        The webhook source drains its queue on poll, so a still-firing pushed signal is
        absent from every cycle after the one that delivered it. Recording that as a plain
        success let `verify_pending_actions` read absence as recovery through a SUCCESSFUL
        poll, which is why the existing `ok` guard could not catch it.
        """
        registry = OpsProviderRegistry()

        class _Spool:
            id = "spool"
            display_name = "spool"
            is_snapshot = False

            def configured(self):
                return True

            async def poll(self):
                return []

        registry.register_signal_source(_Spool())
        registry.register_signal_source(_FakeSource("polled", [_signal()]))
        await registry.poll_all()
        health = registry.poll_health()
        # Both answered; only one of them licenses an inference from absence.
        self.assertTrue(health["spool"]["ok"])
        self.assertFalse(health["spool"]["snapshot"])
        self.assertTrue(health["polled"]["snapshot"])

    async def test_the_real_webhook_source_declares_itself_non_snapshot(self):
        """The flag has to be on the adapter that actually drains, not just supported."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self.assertFalse(webhook.WebhookSignalSource.is_snapshot)

    async def test_a_429_honours_the_providers_retry_after(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import HttpError

        class _RateLimited:
            id = "limited"
            display_name = "limited"

            def configured(self):
                return True

            async def poll(self):
                raise HttpError(429, "HTTP 429: slow down", 42)

        registry = OpsProviderRegistry()
        registry.register_signal_source(_RateLimited())
        await registry.poll_all()
        # 42s requested, so the deadline is ~42s out rather than the 5-minute default.
        remaining = registry._backoff_until["limited"] - time.monotonic()
        self.assertGreater(remaining, 30)
        self.assertLess(remaining, 50)

    async def test_a_success_clears_the_backoff(self):
        registry = OpsProviderRegistry()
        source = _FakeSource("recovers", fail=True)
        registry.register_signal_source(source)
        await registry.poll_all()
        self.assertIn("recovers", registry._backoff_until)
        # Simulate the window elapsing, then a good poll.
        registry._backoff_until["recovers"] = time.monotonic() - 1
        source._fail = False
        source._signals = [_signal()]
        await registry.poll_all()
        self.assertNotIn("recovers", registry._backoff_until)
        self.assertTrue(registry.poll_health()["recovers"]["ok"])

    async def test_an_unpolled_source_is_absent_rather_than_assumed_healthy(self):
        registry = OpsProviderRegistry()
        registry.register_signal_source(_FakeSource("off", [_signal()], ready=False))
        await registry.poll_all()
        self.assertNotIn("off", registry.poll_health())


class TestRetryAfterParsing(unittest.TestCase):
    def test_delta_seconds_is_honoured_and_clamped(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import http

        self.assertEqual(http._parse_retry_after("30"), 30)
        self.assertEqual(http._parse_retry_after(" 30 "), 30)
        self.assertEqual(
            http._parse_retry_after("999999"),
            http.MAX_RETRY_AFTER_SECS,
        )

    def test_unusable_values_yield_zero_rather_than_a_guess(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import http

        for raw in (None, "", "Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-5"):
            self.assertEqual(http._parse_retry_after(raw), 0, raw)

    def test_retryable_statuses_are_distinguished_from_client_errors(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import HttpError

        self.assertTrue(HttpError(429, "x").is_retryable)
        self.assertTrue(HttpError(503, "x").is_retryable)
        # A 404 or 401 is a configuration fault; waiting will not fix it.
        self.assertFalse(HttpError(404, "x").is_retryable)
        self.assertFalse(HttpError(401, "x").is_retryable)


class _FakeRotation:
    def __init__(self, rotation_id, status, *, fail=False, ready=True):
        self.id = rotation_id
        self.display_name = rotation_id
        self._status = status
        self._fail = fail
        self._ready = ready

    def configured(self):
        return self._ready

    async def on_shift(self):
        if self._fail:
            raise RuntimeError("rotation API down")
        return self._status


class TestRotationResolution(unittest.IsolatedAsyncioTestCase):
    async def test_no_source_is_unknown_and_armed(self):
        status = await OpsProviderRegistry().resolve_shift()
        self.assertTrue(status.unknown)
        self.assertTrue(status.on_shift)

    async def test_any_on_shift_source_wins(self):
        registry = OpsProviderRegistry()
        registry.register_rotation_source(_FakeRotation("a", ShiftStatus(on_shift=False)))
        registry.register_rotation_source(
            _FakeRotation("b", ShiftStatus(on_shift=True, who="dana"))
        )
        status = await registry.resolve_shift()
        self.assertTrue(status.on_shift)
        self.assertEqual(status.who, "dana")

    async def test_all_sources_failing_is_unknown_and_armed(self):
        """Fail-open — an unreachable rotation API must not disable response."""
        registry = OpsProviderRegistry()
        registry.register_rotation_source(
            _FakeRotation("a", ShiftStatus(on_shift=False), fail=True)
        )
        status = await registry.resolve_shift()
        self.assertTrue(status.unknown)
        self.assertTrue(status.on_shift)

    async def test_off_shift_is_reported_when_sources_answer(self):
        registry = OpsProviderRegistry()
        registry.register_rotation_source(_FakeRotation("a", ShiftStatus(on_shift=False)))
        status = await registry.resolve_shift()
        self.assertFalse(status.on_shift)
        self.assertFalse(status.unknown)

    async def test_the_always_on_default_does_not_mask_a_real_rotation(self):
        """Regression: the always-on fallback made every real rotation unhearable.

        ``AlwaysOnRotationSource`` is always configured and always on-shift, and
        ``resolve_shift`` returns the first on-shift answer — so a real source reporting
        "someone else is on call" was discarded and the on-shift tier armed permanently
        for everyone. That is precisely the failure a rotation exists to prevent, and it
        would have silently swallowed the whole schedule-file feature.

        Verified against the pre-fix code: this resolved to ``on_shift=True``.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import noop

        registry = OpsProviderRegistry()
        registry.register_rotation_source(noop.AlwaysOnRotationSource())
        registry.register_rotation_source(
            _FakeRotation("real", ShiftStatus(on_shift=False, who="someone-else"))
        )
        status = await registry.resolve_shift()
        self.assertFalse(status.on_shift, "the real rotation must be heard")
        self.assertEqual(status.who, "someone-else")

    async def test_the_default_still_arms_when_it_is_the_only_source(self):
        """A solo operator with no rotation must stay armed — that is the floor's job."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import noop

        registry = OpsProviderRegistry()
        registry.register_rotation_source(noop.AlwaysOnRotationSource())
        status = await registry.resolve_shift()
        self.assertTrue(status.on_shift)
        self.assertTrue(status.unknown, "a default is not a real answer")


class _FakeEvidence:
    def __init__(self, evidence_id, items):
        self.id = evidence_id
        self.display_name = evidence_id
        self._items = items

    def configured(self):
        return True

    async def gather(self, signal, budget):
        return self._items


class TestEvidenceRedaction(unittest.IsolatedAsyncioTestCase):
    """Redaction chokepoint plus evidence config resolution.

    Isolates ``KIROCREW_HOME`` because the config-resolution tests WRITE provider
    config: without it one test's evidence-namespace value leaks into the next and
    the fallback test reads the wrong namespace. (Tests under ``src/`` get no
    ``test/conftest.py`` fixture — the sibling classes here isolate the same way.)
    """

    def setUp(self):
        import os

        self._tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self._tmp)

    def tearDown(self):
        import os

        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_tokens_are_redacted_centrally(self):
        """An adapter author cannot leak a credential by forgetting to redact."""
        registry = OpsProviderRegistry()
        leaky = Evidence(
            source="x",
            kind="logs",
            title="t",
            body="auth failed for token=u+AbCdEfGhIjKlMnOpQrStUv",
        )
        registry.register_evidence_source(_FakeEvidence("x", [leaky]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget())
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUv", out[0].body)

    async def test_the_title_is_redacted_too_not_just_the_body(self):
        """An evidence TITLE is provider-controlled (a CloudWatch alarm name, a Datadog
        monitor title) and flows into `ClaimedIncident.to_dict()` — the dashboard JSON and
        the model brief. The body was redacted at this chokepoint while the title beside it
        was copied raw. Found in review."""
        registry = OpsProviderRegistry()
        leaky = Evidence(
            source="x",
            kind="logs",
            title="alarm AKIAIOSFODNN7EXAMPLE tripped",
            body="ok",
        )
        registry.register_evidence_source(_FakeEvidence("x", [leaky]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget())
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out[0].title)
        self.assertIn("tripped", out[0].title)  # redaction, not truncation

    async def test_body_is_truncated_to_budget(self):
        registry = OpsProviderRegistry()
        big = Evidence(source="x", kind="logs", title="t", body="a" * 5000)
        registry.register_evidence_source(_FakeEvidence("x", [big]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget(max_bytes=100))
        self.assertLessEqual(len(out[0].body), 100)

    async def test_aws_keys_are_redacted_not_just_bearer_tokens(self):
        """Log lines are where AKIA-shaped keys turn up by accident."""
        registry = OpsProviderRegistry()
        leaky = Evidence(
            source="x",
            kind="logs",
            title="t",
            body="AccessDenied for AKIAIOSFODNN7EXAMPLE calling sts:AssumeRole",
        )
        registry.register_evidence_source(_FakeEvidence("x", [leaky]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget())
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out[0].body)

    async def test_budget_holds_even_when_redaction_expands_the_body(self):
        """Redaction runs BEFORE the cap, so markers cannot push past the budget.

        A marker is longer than most of what it replaces. Capping first and redacting
        second let the emitted body exceed `max_bytes` (measured ~1.09x on an
        all-credential body) — and the budget exists to bound what reaches the
        model's context, so it has to bound the text actually emitted.
        """
        registry = OpsProviderRegistry()
        body = " ".join(["AKIAIOSFODNN7EXAMPLE"] * 200)
        registry.register_evidence_source(
            _FakeEvidence("x", [Evidence(source="x", kind="logs", title="t", body=body)])
        )
        out = await registry.gather_evidence(_signal(), EvidenceBudget(max_bytes=1000))
        self.assertLessEqual(len(out[0].body), 1000)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out[0].body)

    async def test_config_flag_accepts_what_an_operator_would_type(self):
        """`include_insufficient_data` compared against the literal string "true".

        So `yes`, `1`, `True ` (trailing space), and a real JSON boolean all read as
        FALSE — silently. The setting looked applied and did nothing, which is the
        worst outcome for a detection opt-in: the operator believes stale-metric
        alarms are being caught and they are not.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            config_flag,
            merge_provider_config,
        )

        for raw in (True, "true", "True", " TRUE ", "yes", "1", "on", "y"):
            merge_provider_config("cloudwatch", {"flag": raw})
            self.assertTrue(config_flag("cloudwatch", "flag"), f"{raw!r} must read true")

        for raw in (False, "false", "no", "0", "off"):
            merge_provider_config("cloudwatch", {"flag": raw})
            self.assertFalse(config_flag("cloudwatch", "flag"), f"{raw!r} must read false")

    async def test_config_flag_falls_back_to_default_when_unset_or_unknown(self):
        """An unrecognized value must not be guessed at in either direction."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            config_flag,
            merge_provider_config,
        )

        self.assertFalse(config_flag("cloudwatch", "absent"))
        self.assertTrue(config_flag("cloudwatch", "absent", default=True))
        merge_provider_config("cloudwatch", {"flag": "maybe"})
        self.assertFalse(config_flag("cloudwatch", "flag"))
        self.assertTrue(config_flag("cloudwatch", "flag", default=True))

    async def test_string_false_does_not_enable_a_provider(self):
        """`bool("false")` is True, which would enable a provider that says it is off.

        Reachable from a hand-edited config or any form that stringifies its values —
        and the failure direction is the dangerous one: a provider the operator
        believes is disabled starts polling.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
            provider_enabled,
        )

        for raw in ("false", "no", "0", "off", False):
            merge_provider_config("pagerduty", {"enabled": raw})
            self.assertFalse(provider_enabled("pagerduty"), f"{raw!r} must stay disabled")

        merge_provider_config("pagerduty", {"enabled": "true"})
        self.assertTrue(provider_enabled("pagerduty"))

    async def test_unknown_enabled_value_stays_disabled(self):
        """Default-quiet: garbage must not switch a provider on."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
            provider_enabled,
        )

        merge_provider_config("pagerduty", {"enabled": "sometimes"})
        self.assertFalse(provider_enabled("pagerduty"))

    async def test_cloudwatch_detail_mentions_the_stale_metric_opt_in(self):
        """An opt-in nobody is told about is an opt-in nobody uses.

        `INSUFFICIENT_DATA` is the CloudWatch equivalent of the
        "table freshness" checks — a pipeline that silently stopped running. The
        provider row is the only place an operator would learn it exists.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        detail = cloudwatch.CloudWatchSignalSource.detail
        self.assertIn("include_insufficient_data", detail)
        self.assertIn("STOPPED", detail)

    async def test_evidence_config_resolves_where_the_ui_writes_it(self):
        """`log_groups` must be readable from the evidence adapter's own namespace.

        The adapter advertises `config_fields`, so Settings writes to
        `providers["cloudwatch-evidence"]` — but the gather code read
        `providers["cloudwatch"]`. `log_groups` exists ONLY on this adapter, so
        whatever the operator typed landed where nothing looked for it and log
        evidence was silently always empty. Found by trying to configure it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
            merge_provider_config,
        )

        merge_provider_config(cloudwatch.EVIDENCE_PROVIDER_ID, {"log_groups": "/aws/lambda/mine"})
        self.assertEqual(cloudwatch._evidence_list("log_groups"), ["/aws/lambda/mine"])

    async def test_evidence_config_falls_back_to_the_signal_namespace(self):
        """A single-account install configures `region` on `cloudwatch` only."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
            merge_provider_config,
        )

        merge_provider_config(cloudwatch.PROVIDER_ID, {"region": "eu-west-1"})
        self.assertEqual(cloudwatch._evidence_value("region"), "eu-west-1")

    async def test_evidence_namespace_wins_over_the_fallback(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
            merge_provider_config,
        )

        merge_provider_config(cloudwatch.PROVIDER_ID, {"region": "us-east-1"})
        merge_provider_config(cloudwatch.EVIDENCE_PROVIDER_ID, {"region": "ap-south-1"})
        self.assertEqual(cloudwatch._evidence_value("region"), "ap-south-1")

    async def test_every_advertised_evidence_field_is_actually_read(self):
        """A `config_fields` entry the code never reads is a lie to the operator.

        This is the class of bug above, generalized: the UI renders an input for each
        advertised field, so one that resolves nowhere silently does nothing.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        source = inspect.getsource(cloudwatch)
        for field in cloudwatch.CloudWatchEvidenceSource.config_fields:
            if field == "enabled":
                continue  # read via provider_enabled(), not by name
            self.assertIn(
                f'"{field}"',
                source,
                f"evidence advertises config field {field!r} but never reads it",
            )

    async def test_budget_hint_can_narrow_but_never_widen(self):
        """An adapter must not be able to raise its own spend ceiling.

        The hint says "this is what I need"; the operator's configured budget stays the
        authority. Same reason the autonomy gate is resolved outside the adapter — a
        provider that could grant itself more time/calls/bytes is a provider that sets
        its own cost.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            EvidenceBudget,
        )

        ceiling = EvidenceBudget(timeout_secs=20.0, max_calls=6, max_bytes=1000)

        class _Greedy:
            evidence_budget_hint = {
                "timeout_secs": 600.0,
                "max_calls": 999,
                "max_bytes": 10_000_000,
            }

        widened = ceiling.for_source(_Greedy())
        self.assertEqual(widened.timeout_secs, 20.0)
        self.assertEqual(widened.max_calls, 6)
        self.assertEqual(widened.max_bytes, 1000)

        class _Modest:
            evidence_budget_hint = {"timeout_secs": 5.0, "max_bytes": 100}

        narrowed = ceiling.for_source(_Modest())
        self.assertEqual(narrowed.timeout_secs, 5.0)
        self.assertEqual(narrowed.max_bytes, 100)
        self.assertEqual(narrowed.max_calls, 6, "unhinted fields keep the ceiling")

    async def test_no_hint_leaves_the_budget_untouched(self):
        """Opt-in: every existing adapter must behave exactly as before."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            EvidenceBudget,
        )

        budget = EvidenceBudget()

        class _Plain:
            pass

        self.assertEqual(budget.for_source(_Plain()), budget)
        # And a malformed hint is ignored rather than crashing the fan-out.
        for bad in ({}, "nonsense", None, {"timeout_secs": "soon"}, {"max_calls": -1}):

            class _Bad:
                evidence_budget_hint = bad

            self.assertEqual(budget.for_source(_Bad()), budget, f"hint={bad!r}")

    async def test_cloudwatch_evidence_hint_makes_its_ceiling_reachable(self):
        """The concrete bug this feature fixes.

        `_LOG_MAX_WAIT_SECS = 25.0` with a 20s global meant `min(25, 20)` always chose
        20 — the adapter's own ceiling was unreachable dead code. With the hint the
        Logs Insights poll can use what it declared, still clamped by the operator.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            EvidenceBudget,
        )

        source = cloudwatch.CloudWatchEvidenceSource()
        generous = EvidenceBudget(timeout_secs=60.0).for_source(source)
        self.assertEqual(generous.timeout_secs, cloudwatch._LOG_MAX_WAIT_SECS)

        # But an operator who tightened the budget still wins.
        tight = EvidenceBudget(timeout_secs=8.0).for_source(source)
        self.assertEqual(tight.timeout_secs, 8.0)

    async def test_each_source_is_waited_on_its_own_resolved_budget(self):
        """The wait_for timeout must be the value the adapter was handed.

        Passing one timeout into `gather` and enforcing a different one outside it kills
        the adapter mid-call while it believes it still has budget.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry as reg_mod

        src = inspect.getsource(reg_mod.OpsProviderRegistry.gather_evidence)
        self.assertIn("per_source = budget.for_source(src)", src)
        self.assertIn("src.gather(signal, per_source)", src)
        self.assertIn("timeout=per_source.timeout_secs", src)

    async def test_redaction_is_the_only_path_out_of_an_adapter(self):
        """`gather_evidence` must be the sole caller of any adapter's `gather()`.

        The `Evidence` docstring promises adapters "cannot forget" to redact. That
        holds only while this method is the single funnel — a second call site would
        silently bypass the chokepoint, so pin it by source inspection.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry as reg_mod

        source = inspect.getsource(reg_mod)
        # One `.gather(` call — inside gather_evidence. `asyncio.gather(` is distinct.
        adapter_calls = source.count("src.gather(")
        self.assertEqual(
            adapter_calls,
            1,
            "an adapter's gather() must be invoked only from gather_evidence, "
            "which is where redaction happens",
        )


class _FakeSink:
    def __init__(self, sink_id):
        self.id = sink_id
        self.display_name = sink_id

    def configured(self):
        return True

    def supported_actions(self):
        return frozenset({ACTION_RESOLVE})

    async def execute(self, signal, action, payload):
        return ActionResult(ok=True, action=action, detail="done")


class TestCatalog(unittest.IsolatedAsyncioTestCase):
    async def test_multi_role_adapter_appears_once_with_all_roles(self):
        """PagerDuty is signal+rotation+action; it must not appear three times."""
        registry = OpsProviderRegistry()
        multi = _FakeSource("pagerduty")
        registry.register_signal_source(multi)
        registry.register_rotation_source(_FakeRotation("pagerduty", ShiftStatus(on_shift=True)))
        registry.register_action_sink(_FakeSink("pagerduty"))
        catalog = registry.catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(set(catalog[0].roles), {"signal", "rotation", "action"})

    async def test_broken_configured_does_not_break_the_catalog(self):
        """A misbehaving adapter must not take the settings page down with it."""

        class _Exploding:
            id = "boom"
            display_name = "boom"

            def configured(self) -> bool:
                raise RuntimeError("nope")

            async def poll(self) -> list[Signal]:
                return []

        registry = OpsProviderRegistry()
        registry.register_signal_source(_Exploding())
        catalog = registry.catalog()
        self.assertEqual(len(catalog), 1)
        self.assertFalse(catalog[0].configured)


class TestPublicAdapterDefaults(unittest.IsolatedAsyncioTestCase):
    """The shipped defaults are what make a fresh install safe and useful.

    ``KIROCREW_HOME`` is redirected to an empty temp dir. Without it these tests
    read the OPERATOR'S live ``data/config.json``, so the moment someone enables
    CloudWatch in the real dashboard the "unconfigured" assertions start polling
    real AWS and fail — which is exactly what happened once the app was enabled
    for browser testing. A test that asserts fresh-install behavior has to supply
    a fresh install.
    """

    def setUp(self):
        import os
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        self._clear_config_cache()

    def tearDown(self):
        import os
        import shutil as _shutil

        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        self._clear_config_cache()
        _shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_config_cache():
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    async def test_noop_sink_is_always_available_and_writes_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import noop

        sink = noop.NoopActionSink()
        self.assertTrue(sink.configured())
        self.assertEqual(sink.supported_actions(), VALID_ACTIONS)
        result = await sink.execute(_signal(), ACTION_RESOLVE, {})
        self.assertTrue(result.ok)
        self.assertIn("observe-only", result.detail)

    async def test_always_on_rotation_keeps_a_solo_operator_covered(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import noop

        status = await noop.AlwaysOnRotationSource().on_shift()
        self.assertTrue(status.on_shift)

    async def test_adapters_report_unconfigured_rather_than_raising(self):
        """A user with no AWS/PagerDuty/Datadog setup must see no errors."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
            datadog,
            github_issues,
            pagerduty,
            webhook,
        )

        tmp = Path(tempfile.mkdtemp())
        try:
            for adapter in (
                cloudwatch.CloudWatchSignalSource(),
                cloudwatch.CloudWatchEvidenceSource(),
                pagerduty.PagerDutyAdapter(),
                datadog.DatadogAdapter(),
                datadog.DatadogEvidenceSource(),
                github_issues.GitHubIssuesAdapter(),
                webhook.WebhookSignalSource(),
            ):
                with self.subTest(adapter=adapter.id):
                    self.assertFalse(adapter.configured())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_unconfigured_poll_returns_empty(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
            pagerduty,
        )

        self.assertEqual(await cloudwatch.CloudWatchSignalSource().poll(), [])
        self.assertEqual(await pagerduty.PagerDutyAdapter().poll(), [])

    async def test_public_registry_installs_expected_adapters(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry as reg

        reg.reset_registry()
        catalog = {p.id for p in reg.get_registry().catalog()}
        for expected in ("noop", "always-on", "cloudwatch", "pagerduty", "datadog"):
            self.assertIn(expected, catalog)
        reg.reset_registry()


class TestSuppressionIsAlwaysBounded(unittest.IsolatedAsyncioTestCase):
    """A suppression with no expiry hides a live fault until a human remembers it.

    This is the property that makes ``act`` a bounded bet rather than an
    all-or-nothing one: a WRONG silence expires by itself. The shipped Datadog sink
    used to POST ``/mute`` with ``body={}``, and Datadog reads a missing ``end`` as
    "mute forever" — so the board showed the incident resolved while the metric stayed
    bad, with no way back but a human noticing.
    """

    def test_a_requested_window_is_clamped_not_honoured_blindly(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self.assertEqual(models.resolve_silence_secs(600), 600)
        self.assertEqual(models.resolve_silence_secs(10**9), models.MAX_SILENCE_SECS)

    def test_unusable_input_yields_the_default_never_no_expiry(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        unusable: tuple[object, ...] = (None, "", "forever", 0, -1, [], {})
        for raw in unusable:
            self.assertEqual(
                models.resolve_silence_secs(raw), models.DEFAULT_SILENCE_SECS, repr(raw)
            )

    async def test_datadog_mute_always_carries_an_end(self):
        """The regression guard for the indefinite mute."""
        import time as _time

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog

        sent: dict[str, Any] = {}

        def _fake_request(url, *, method="GET", headers=None, params=None, body=None, **kw):
            sent["url"] = url
            sent["body"] = body
            return {}

        original = datadog.request_json
        datadog.request_json = _fake_request
        try:
            adapter = datadog.DatadogAdapter()
            result = adapter._execute_sync("123", ACTION_SILENCE, {})
        finally:
            datadog.request_json = original

        self.assertTrue(result.ok)
        self.assertIn("/mute", sent["url"])
        body = sent["body"]
        assert isinstance(body, dict)
        # The whole point: an `end` is present and in the future.
        self.assertIn("end", body)
        self.assertGreater(body["end"], int(_time.time()))

    async def test_datadog_resolve_alias_is_bounded_too(self):
        """`resolve` still maps onto the mute for already-granted rules — bounded."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog

        sent: dict[str, Any] = {}

        def _fake_request(url, *, method="GET", headers=None, params=None, body=None, **kw):
            sent["body"] = body
            return {}

        original = datadog.request_json
        datadog.request_json = _fake_request
        try:
            datadog.DatadogAdapter()._execute_sync("123", ACTION_RESOLVE, {})
        finally:
            datadog.request_json = original

        body = sent["body"]
        assert isinstance(body, dict)
        self.assertIn("end", body)

    async def test_the_mute_window_is_reported_so_verification_waits_for_it(self):
        """A `resolve` that is really a 4-hour mute must not be rechecked in 5 minutes.

        Datadog aliases `resolve` onto the same bounded mute as `silence`, but only
        `EXPIRING_ACTIONS` (i.e. `silence`) gets a `duration_secs` from the route — so a
        resolve established a four-hour suppression while the verifier used its five-minute
        default, rechecked INSIDE the mute, read the monitor as still Alert, and charged a
        false `miss` to every ledger entry the investigation cited. The sink now reports the
        window it actually established, for BOTH verbs, and the route schedules from that.

        Fails against the pre-fix adapter, which returned `suppressed_secs=0`. Found in
        review (GPT 5.6).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog

        original = datadog.request_json
        datadog.request_json = lambda *a, **kw: {}
        try:
            for action in (ACTION_RESOLVE, ACTION_SILENCE):
                with self.subTest(action=action):
                    result = datadog.DatadogAdapter()._execute_sync("123", action, {})
                    self.assertTrue(result.ok)
                    self.assertGreater(
                        result.suppressed_secs,
                        0,
                        f"{action} muted the monitor but reported no window, so verification "
                        "would recheck inside the mute and record a false miss",
                    )
                    # And it is the window actually sent to Datadog, not a guess.
                    self.assertEqual(result.suppressed_secs, DEFAULT_SILENCE_SECS)
        finally:
            datadog.request_json = original

    async def test_a_comment_reports_no_suppression(self):
        """The field means "this call suppressed the signal" — a note does not."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog

        original = datadog.request_json
        datadog.request_json = lambda *a, **kw: {}
        try:
            result = datadog.DatadogAdapter()._execute_sync("123", ACTION_COMMENT, {"note": "x"})
        finally:
            datadog.request_json = original
        self.assertTrue(result.ok)
        self.assertEqual(result.suppressed_secs, 0)

    async def test_datadog_advertises_the_honest_verb(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog

        actions = datadog.DatadogAdapter().supported_actions()
        self.assertIn(ACTION_SILENCE, actions)
        # Kept as an alias so an existing act-rule granting `resolve` is not revoked.
        self.assertIn(ACTION_RESOLVE, actions)

    async def test_github_does_not_claim_a_suppression_it_cannot_perform(self):
        """An issue tracker has no snooze; advertising one would be a lie."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import github_issues

        self.assertNotIn(ACTION_SILENCE, github_issues.GitHubIssuesAdapter().supported_actions())


class TestHttpHelper(unittest.IsolatedAsyncioTestCase):
    async def test_non_https_is_refused(self):
        """Provider tokens ride in these headers — cleartext is not an option."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import (
            HttpError,
            request_json,
        )

        with self.assertRaises(HttpError) as ctx:
            request_json("http://example.com/api")
        self.assertIn("non-https", str(ctx.exception))

    async def test_error_messages_are_token_scrubbed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import (
            HttpError,
        )

        err = HttpError(401, "rejected token=u+AbCdEfGhIjKlMnOpQrStUv")
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUv", str(err))


class TestWebhookIngress(unittest.IsolatedAsyncioTestCase):
    async def test_unsigned_delivery_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        accepted, detail = webhook.enqueue(b'{"title":"x"}', "")
        self.assertFalse(accepted)
        self.assertTrue(detail)

    async def test_signature_verification_fails_without_secret(self):
        """Fail-closed: no configured secret means reject everything."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self.assertFalse(webhook.verify_signature(b"body", "deadbeef"))

    async def test_payload_without_title_is_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        self.assertIsNone(webhook.signal_from_payload({"severity": "critical"}))

    async def test_payload_with_title_normalizes(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        signal = webhook.signal_from_payload(
            {"title": "disk full", "severity": "p1", "resource": "/dev/sda1"}
        )
        assert signal is not None
        self.assertEqual(signal.severity, "critical")
        self.assertEqual(signal.source, "webhook")
        self.assertTrue(signal.fingerprint)


if __name__ == "__main__":
    unittest.main()


class TestDatadogCredentialsOnlyReachDatadog(unittest.IsolatedAsyncioTestCase):
    """The configured ``site`` decides which HOST receives both stored Datadog keys.

    ``_headers()`` attaches ``DD-API-KEY`` and ``DD-APPLICATION-KEY`` to every request, and
    ``site`` was interpolated into the request host verbatim from provider config that
    ``PUT /providers/<id>/config`` lets the agent write. A prompt-injected agent setting
    ``site`` to a host it controls therefore had both credentials posted to it on the next
    poll — a complete credential handover with no user-visible step. Found in review.

    The gate is an allowlist of Datadog's published site domains. Not a suffix test
    (`evil-datadoghq.com` ends in `datadoghq.com`) and not a shape test (that admits every
    domain there is).
    """

    def setUp(self):
        import os

        self._tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self._tmp)

    def tearDown(self):
        import os

        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_documented_site_is_honoured(self):
        """The gate must not break the multi-region support it protects."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            datadog,
            merge_provider_config,
        )

        for site in ("datadoghq.eu", "us3.datadoghq.com", "ddog-gov.com", "ap1.datadoghq.com"):
            merge_provider_config("datadog", {"site": site})
            self.assertEqual(datadog._api_base(), f"https://api.{site}")

    def test_an_attacker_controlled_host_never_receives_the_keys(self):
        """Each of these was a working exfiltration path before the allowlist."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            datadog,
            merge_provider_config,
        )

        for hostile in (
            "attacker.example.com",
            # Defeats a naive endswith() check.
            "evil-datadoghq.com",
            # Defeats a naive "contains" check.
            "datadoghq.com.attacker.example",
            # Userinfo trick: the real host is the part after the @.
            "datadoghq.com@attacker.example",
            # Path/port injection into the interpolated URL.
            "attacker.example/x",
            "attacker.example:8443",
        ):
            merge_provider_config("datadog", {"site": hostile})
            base = datadog._api_base()
            self.assertEqual(
                base,
                f"https://api.{datadog._DEFAULT_SITE}",
                f"{hostile!r} must not reach the request host",
            )
            self.assertNotIn("attacker", base)
            self.assertNotIn("evil", base)

    def test_the_monitor_link_is_gated_too(self):
        """No credential rides it, but it is a link an operator clicks from the board."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            datadog,
            merge_provider_config,
        )

        merge_provider_config("datadog", {"site": "attacker.example.com"})
        url = datadog.DatadogAdapter._monitor_url("123")
        self.assertTrue(url.startswith(f"https://app.{datadog._DEFAULT_SITE}/"), url)

    def test_an_unset_site_still_defaults_to_us(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import datadog

        self.assertEqual(datadog._api_base(), f"https://api.{datadog._DEFAULT_SITE}")

    def test_whitespace_and_case_are_tolerated_not_a_bypass(self):
        """An operator pasting ` DataDogHQ.eu ` should work; it must not widen the gate."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            datadog,
            merge_provider_config,
        )

        merge_provider_config("datadog", {"site": "  DataDogHQ.eu  "})
        self.assertEqual(datadog._api_base(), "https://api.datadoghq.eu")


class TestACloudWatchFaultIsNotAQuietEstate(unittest.IsolatedAsyncioTestCase):
    """A poll that could not read AWS must not be recorded as a poll that saw nothing.

    ``registry.poll_all`` marks a source unhealthy ONLY when ``poll()`` raises. ``_poll_sync``
    swallowed both of its failure paths — ``client is None`` (boto3 missing, bad profile,
    **expired credentials**) and a ``describe_alarms`` exception — and returned ``[]`` or the
    partial list gathered so far. Either way the registry recorded a SUCCESSFUL poll, so with
    expired credentials the board read as an all-clear over a live estate and
    ``all_sources_healthy`` promised absence-means-recovery. Found in review; it is the same
    defect class as the SignalsPanel row that printed ``ready / ok`` for a source in backoff.

    A default install is unaffected: ``provider_enabled`` defaults to False, so nothing polls
    CloudWatch until the operator explicitly turns it on — at which point a credential fault
    is exactly what they need told.
    """

    def setUp(self):
        import os

        self._tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self._tmp)
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
        )

        merge_provider_config("cloudwatch", {"enabled": True})

    def tearDown(self):
        import os

        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_an_unavailable_client_raises_rather_than_polling_empty(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        adapter = cloudwatch.CloudWatchSignalSource()
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=None):
            with self.assertRaises(Exception) as caught:
                await adapter.poll()
        # The message has to name the likely cause; "poll failed" sends nobody anywhere.
        self.assertIn("credential", str(caught.exception).lower())

    async def test_a_describe_alarms_failure_propagates(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        class _Boom:
            @staticmethod
            def describe_alarms(**_kw):
                raise RuntimeError("ExpiredToken")

        adapter = cloudwatch.CloudWatchSignalSource()
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=_Boom()):
            with self.assertRaises(Exception) as caught:
                await adapter.poll()
        self.assertIn("describe_alarms", str(caught.exception))

    async def test_the_registry_records_the_fault_as_unhealthy(self):
        """The property that actually matters: the operator is told, and absence is not
        readable as recovery."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        registry.reset_registry()
        try:
            with mock.patch.object(cloudwatch, "_boto3_client", return_value=None):
                reg = registry.get_registry()
                signals, errors = await reg.poll_all()
            self.assertEqual(signals, [])
            self.assertIn("cloudwatch", errors, "the fault must surface as a per-source error")
            health = reg.poll_health()
            self.assertFalse(
                health.get("cloudwatch", {}).get("ok", True),
                "a source that could not be read must not report a healthy poll",
            )
        finally:
            registry.reset_registry()


class TestTheCloudWatchConsoleLinkCannotBeRedirected(unittest.IsolatedAsyncioTestCase):
    """`region` is interpolated into the console HOSTNAME, and the agent can write it.

    Same class as the Datadog `site` finding, found by auditing for that class rather than
    waiting for it to be reported. `region` is ordinary non-secret config writable through
    `PUT /providers/<id>/config`, and `_console_url` renders
    `https://{region}.console.aws.amazon.com/...` into a link on the incident board labelled
    "open in provider" — one an operator is meant to click.

    Measured before fixing, with `urllib.parse.urlsplit`:

      region="evil#"               -> https://evil#.console.aws.amazon.com/...  host=evil
      region="attacker.example.com" -> host=attacker.example.com.console.aws.amazon.com

    The `#` case is the sharp one: it starts the URL fragment, so everything after it is
    discarded and the real host is whatever precedes it.

    No credential rides this URL — that is why it is a phishing vector rather than an
    exfiltration one, and it is exactly the reason review gave for gating the equivalent
    Datadog monitor link.
    """

    def setUp(self):
        import os

        self._tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self._tmp)

    def tearDown(self):
        import os

        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_real_region_still_builds_a_real_console_link(self):
        """Including the gov and cn partitions — a shape check must not break them."""
        from urllib.parse import urlsplit

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        for region in ("us-east-1", "eu-west-2", "ap-southeast-3", "us-gov-west-1", "cn-north-1"):
            self.assertEqual(cloudwatch._validated_region(region), region)
            url = cloudwatch.CloudWatchSignalSource._console_url(region, "my-alarm")
            self.assertEqual(urlsplit(url).netloc, f"{region}.console.aws.amazon.com")

    def test_no_injected_region_can_move_the_host(self):
        """Asserted on the PARSED host, not on the string: the point is where a click goes."""
        from urllib.parse import urlsplit

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
            merge_provider_config,
        )

        for hostile in (
            "evil#",  # `#` truncates: real host becomes `evil`
            "attacker.example.com",
            "x/y",  # path injection
            "a@b",  # userinfo trick
            "us-east-1:8443",
            "us-east-1?x=1",
            "../../evil",
        ):
            # Driven through CONFIG and the real poll-path reader, not by calling the
            # validator with the value. An earlier version of this test asserted on
            # `_validated_region(hostile)` directly, which meant it still passed when the
            # validator was removed from the call path entirely — it was checking that the
            # function works, not that anything uses it.
            merge_provider_config("cloudwatch", {"region": hostile})
            cleaned = cloudwatch._region()
            self.assertEqual(cleaned, "", f"{hostile!r} must not survive the config read")
            url = cloudwatch.CloudWatchSignalSource._console_url(cleaned, "my-alarm")
            # No region means NO link, which is the honest degradation.
            self.assertEqual(url, "", f"{hostile!r} must render no link at all")
            if url:  # pragma: no cover — defensive
                self.assertNotIn("evil", urlsplit(url).netloc)
                self.assertNotIn("attacker", urlsplit(url).netloc)

    def test_an_uppercase_typo_is_normalised_rather_than_dropped(self):
        """AWS regions are canonically lowercase, but silently rendering no link for an
        obvious typo is worse than accepting what the operator plainly meant."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        self.assertEqual(cloudwatch._validated_region("US-EAST-1"), "us-east-1")
        self.assertEqual(cloudwatch._validated_region("  us-east-1  "), "us-east-1")
        # ...and normalising must not widen the gate.
        self.assertEqual(cloudwatch._validated_region("attacker.EXAMPLE.com"), "")

    def test_both_config_namespaces_are_validated(self):
        """`region` is readable from the evidence namespace too, via `_evidence_value`.

        Validating only the signal source's read would leave the same field unguarded one
        namespace over — it reaches boto3 endpoint resolution from there.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        source = inspect.getsource(cloudwatch)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        raw_reads = [
            line.strip()
            for line in code.splitlines()
            if 'region = ' in line and "_validated_region" not in line and "_region()" not in line
        ]
        self.assertEqual(
            raw_reads,
            [],
            f"every region read must pass through _validated_region(): {raw_reads}",
        )


class TestPagerDutyCannotBorrowATeammatesShift(unittest.IsolatedAsyncioTestCase):
    """Without an identity this source must ABSTAIN, not claim whoever is on call.

    Both the API query filter and the per-entry check in `_on_shift_sync` were conditional on
    `user_id`, so a blank one made the source report `on_shift=True` for ANY teammate's shift.
    `_definitely_off_shift` then read a colleague's rotation as this instance's own and
    permitted a production write off shift — the exact refusal the whole rotation layer exists
    to make. Found in review.

    The fix reports `unknown` rather than `on_shift=False`. The review proposed False; that is
    the wrong direction, because the vote treats False as a real off-shift ballot, so an
    operator who set `schedule_ids` and forgot the user id would have every manual action
    refused with nothing explaining why. `unknown` is a non-vote: this source steps aside and
    any other configured rotation still decides.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _adapter(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import pagerduty

        return pagerduty, pagerduty.PagerDutyAdapter()

    def test_a_blank_identity_abstains_instead_of_claiming_the_shift(self) -> None:
        pagerduty, adapter = self._adapter()

        # A schedule IS configured, and PagerDuty says a teammate holds the pager.
        called: list[dict] = []

        def _fake_request(url, headers=None, params=None):
            called.append(dict(params or {}))
            return {"oncalls": [{"user": {"id": "PTEAMMATE", "summary": "alice"}, "end": ""}]}

        with mock.patch.object(pagerduty, "config_list", return_value=["PSCHED1"]):
            with mock.patch.object(pagerduty, "request_json", side_effect=_fake_request):
                # No identity on the keystone — the operator never set one.
                status = adapter._on_shift_sync()

        self.assertTrue(status.unknown, "a source with no identity must abstain")
        self.assertEqual(called, [], "it must not even ask: there is nothing to ask about")

    def test_clearing_schedule_ids_cannot_manufacture_an_abstention(self) -> None:
        """An agent-writable field must not be able to switch this source off.

        `schedule_ids` is in `config_fields`, i.e. in agent-writable `config.json`, while
        `user_id` is operator-only on the keystone. Reporting `unknown` for an empty schedule
        list regardless let the constrained party MANUFACTURE the abstention that
        `_definitely_off_shift` treats as a non-vote — so one config write made this source
        stop answering and the off-shift refusal stopped firing, with nothing about who is on
        call having changed.

        Reproduced end to end before fixing: with schedules set and PagerDuty reporting off
        shift, `authorize_action` refused; after clearing `schedule_ids` the same signal
        returned `(True, 'granted by rule on cloudwatch')`.

        A stored `user_id` is the operator's own statement that they use this rotation and is
        the one input here the agent cannot forge, so it decides which kind of absence an
        empty schedule list is. Found in review (GPT 5.6).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        pagerduty, adapter = self._adapter()
        policy_store.put(policy_store.PAGERDUTY_USER_KEY, "PME")

        with mock.patch.object(pagerduty, "config_list", return_value=[]):
            status = adapter._on_shift_sync()

        self.assertFalse(status.unknown, "an operator-configured rotation must not abstain")
        self.assertFalse(
            status.on_shift,
            "with the operator's identity set, an empty (agent-writable) schedule list is a "
            "source that should be able to answer and cannot — an off-shift vote, not a shrug",
        )

    def test_a_genuinely_unconfigured_pagerduty_still_abstains(self) -> None:
        """The other half, and the reason this is not just "return False": no identity AND no
        schedules is the normal state of a solo install, and denying there would make a
        missing config silently refuse every manual action."""
        pagerduty, adapter = self._adapter()

        with mock.patch.object(pagerduty, "config_list", return_value=[]):
            status = adapter._on_shift_sync()

        self.assertTrue(status.unknown, "an unconfigured source must abstain, not deny")
        self.assertTrue(status.on_shift, "the tier gate must still fail OPEN")

    def test_a_configured_identity_still_matches_only_itself(self) -> None:
        """The guard must not break the legitimate case, in either direction."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        pagerduty, adapter = self._adapter()
        policy_store.put(policy_store.PAGERDUTY_USER_KEY, "PME")

        def _oncalls(user_id):
            def _fake(url, headers=None, params=None):
                return {"oncalls": [{"user": {"id": user_id, "summary": "x"}, "end": ""}]}

            return _fake

        with mock.patch.object(pagerduty, "config_list", return_value=["PSCHED1"]):
            with mock.patch.object(pagerduty, "request_json", side_effect=_oncalls("PME")):
                mine = adapter._on_shift_sync()
            with mock.patch.object(pagerduty, "request_json", side_effect=_oncalls("POTHER")):
                theirs = adapter._on_shift_sync()

        self.assertTrue(mine.on_shift, "my own shift must still register")
        self.assertFalse(mine.unknown)
        self.assertFalse(theirs.on_shift, "a teammate's shift is not mine")
        self.assertFalse(theirs.unknown, "and that is a real vote, not an abstention")


class TestConcurrentStoreWritesDoNotLoseData(unittest.TestCase):
    """The config store and the keystone secret store are both read-modify-writes over a file
    that `atomic_write` REPLACES, so two concurrent saves each write onto a stale snapshot and
    the later replace silently drops the other. Measured before the locks: the config store
    lost updates about half the time; the SECRET store lost ~118/120 — nearly every concurrent
    save of two different providers dropped one credential. Found in review, the same class as
    `store._IndexLock` / `ledger._LedgerLock` / `policy_store._PolicyLock`.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_config_writes_keep_both_keys(self):
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
            read_config,
            set_top_level,
        )

        losses = 0
        for _ in range(30):
            set_top_level("primary_instance", True)
            merge_provider_config("cloudwatch", {"region": "old"})
            threads = [
                threading.Thread(target=lambda: set_top_level("primary_instance", False)),
                threading.Thread(
                    target=lambda: merge_provider_config("cloudwatch", {"region": "new"})
                ),
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            cfg = read_config()
            if cfg.get("primary_instance") is not False or (
                cfg.get("providers", {}).get("cloudwatch", {}).get("region") != "new"
            ):
                losses += 1
        self.assertEqual(losses, 0, f"{losses}/30 config rounds lost a concurrent write")

    def test_concurrent_secret_saves_keep_both_credentials(self):
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import KeystoneFileBackend

        backend = KeystoneFileBackend()
        losses = 0
        for _ in range(30):
            threads = [
                threading.Thread(target=lambda: backend.put("pagerduty", "api_token", "pd")),
                threading.Thread(target=lambda: backend.put("datadog", "api_key", "dd")),
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            if backend.get("pagerduty", "api_token") != "pd" or (
                backend.get("datadog", "api_key") != "dd"
            ):
                losses += 1
            backend.delete("pagerduty")
            backend.delete("datadog")
        self.assertEqual(
            losses, 0, f"{losses}/30 secret rounds lost a credential — a lost update here is a lost secret"
        )

    def test_no_module_open_codes_a_config_read_modify_write(self):
        """`write_config` must only be reached through the locked setters.

        `_ConfigLock` guards `merge_provider_config` and `set_top_level`, but a caller that
        open-codes `cfg = read_config(); cfg[k] = v; write_config(cfg)` bypasses the lock and
        drops concurrent updates. Two did — `ledger_sync.set_settings` and
        `notify_out.set_settings` — and were repointed at `set_top_level`. This walks the whole
        backend so a third cannot reappear: outside `providers/__init__.py` (which DEFINES the
        store), no module may call `write_config` at all.
        """
        import ast
        import pathlib as _pl

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import __file__ as pf

        backend = _pl.Path(pf).parent.parent
        offenders = []
        for path in sorted(backend.rglob("*.py")):
            if path.name == "__init__.py" and path.parent.name == "providers":
                continue  # the store's own definition
            if "/tests/" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "write_config"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "write_config() is an unlocked whole-file replace; go through set_top_level / "
            f"merge_provider_config (which hold _ConfigLock). Open-coded at: {offenders}",
        )

    def test_both_stores_lock_their_read_modify_write(self):
        """Structural, because the races above are timing-dependent and pass by luck on a
        fast machine. A new writer added without the lock fails here instead of in production."""
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import secrets
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
            set_top_level,
        )

        for fn in (merge_provider_config, set_top_level):
            with self.subTest(fn=fn.__name__):
                self.assertIn("_ConfigLock()", inspect.getsource(fn))
        for meth in (secrets.KeystoneFileBackend.put, secrets.KeystoneFileBackend.delete):
            with self.subTest(meth=meth.__name__):
                self.assertIn("self._lock()", inspect.getsource(meth))


class TestCloudWatchAlarmsArePaged(unittest.IsolatedAsyncioTestCase):
    """`describe_alarms` paginates, and one page is not the estate.

    Reading only the first page stopped at `MaxRecords` with nothing indicating more existed —
    so an account with more firing alarms than the cap under-returned while `poll_all` recorded
    `snapshot=True`, and `reconcile` terminally resolved the omitted live incidents. Same
    "a partial snapshot must not look complete" rule the adapter already enforced for a FAILED
    call, applied to the case where every call succeeds and the estate is just larger than a
    page. Found in review (GPT 5.6).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    class _FakeCW:
        """Returns ``pages`` pages of ``per`` alarms, with a NextToken until the last."""

        def __init__(self, pages: int, per: int) -> None:
            self.calls = 0
            self._pages = pages
            self._per = per

        def describe_alarms(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            alarms = [
                {"AlarmName": f"p{self.calls}-{i}", "Namespace": "AWS/SQS", "MetricName": "Depth"}
                for i in range(self._per)
            ]
            more = {"NextToken": f"tok{self.calls}"} if self.calls < self._pages else {}
            return {"MetricAlarms": alarms, **more}

    def _enable(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import set_top_level

        set_top_level("providers", {"cloudwatch": {"enabled": True, "region": "us-east-1"}})

    async def test_paging_continues_past_the_first_page(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        self._enable()
        fake = self._FakeCW(pages=4, per=60)
        with mock.patch.object(cloudwatch, "_boto3_client", lambda *a, **k: fake):
            signals = await cloudwatch.CloudWatchSignalSource().poll()

        self.assertGreater(fake.calls, 1, "only the first page was read")
        self.assertGreater(
            len(signals),
            cloudwatch.DEFAULT_POLL_LIMIT,
            "the adapter must return at least one signal past the cap so poll_all can tell "
            "the estate is larger than the poll",
        )

    async def test_paging_stops_once_over_the_cap(self):
        """Not exhaustive: draining an unbounded estate is the bug the cap exists to prevent."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        self._enable()
        fake = self._FakeCW(pages=50, per=60)
        with mock.patch.object(cloudwatch, "_boto3_client", lambda *a, **k: fake):
            await cloudwatch.CloudWatchSignalSource().poll()
        self.assertEqual(fake.calls, 2, "paged further than needed to detect the overflow")

    async def test_a_small_estate_reads_exactly_one_page(self):
        """No extra API call when the provider says there is nothing more."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        self._enable()
        fake = self._FakeCW(pages=1, per=5)
        with mock.patch.object(cloudwatch, "_boto3_client", lambda *a, **k: fake):
            signals = await cloudwatch.CloudWatchSignalSource().poll()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(signals), 5)

    async def test_an_endless_token_is_an_error_not_a_quiet_partial(self):
        """Bounded out with pages pending and UNDER the cap: nothing downstream would notice
        the shortfall, so the honest answer is "cloudwatch did not answer"."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        self._enable()
        # 1 alarm per page, so the page ceiling is hit long before the poll cap.
        fake = self._FakeCW(pages=10_000, per=1)
        with mock.patch.object(cloudwatch, "_boto3_client", lambda *a, **k: fake):
            with self.assertRaises(RuntimeError) as ctx:
                await cloudwatch.CloudWatchSignalSource().poll()
        self.assertIn("partial estate", str(ctx.exception))
        self.assertEqual(fake.calls, cloudwatch._MAX_ALARM_PAGES)

    async def test_a_paged_estate_is_reported_as_a_non_snapshot(self):
        """The consequence that matters, asserted through the real registry."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import cloudwatch

        self._enable()
        registry = OpsProviderRegistry()
        registry.register_signal_source(cloudwatch.CloudWatchSignalSource())
        with mock.patch.object(
            cloudwatch, "_boto3_client", lambda *a, **k: self._FakeCW(pages=4, per=60)
        ):
            signals, _ = await registry.poll_all()

        health = registry.poll_health()["cloudwatch"]
        self.assertEqual(len(signals), cloudwatch.DEFAULT_POLL_LIMIT)
        self.assertTrue(health["ok"])
        self.assertFalse(
            health["snapshot"],
            "an estate larger than the poll was reported as a complete snapshot, so the "
            "omitted firing alarms would be terminally resolved as recovered",
        )


class TestPollersMarkTruncationSoAbsenceIsNotRecovery(unittest.IsolatedAsyncioTestCase):
    """Every polled source must distinguish "full" from "capped".

    Each adapter requested exactly `DEFAULT_POLL_LIMIT`, so `len(result) == limit` was
    ambiguous — and `poll_all` recorded `snapshot=True` regardless, which is how `reconcile`
    and `verify_pending_actions` infer recovery. On an estate larger than the cap, the omitted
    still-firing signals were terminally resolved as cleared. The CloudWatch adapter paged; the
    other three fetched `DEFAULT_POLL_LIMIT + 1` (or read a `more` flag) and return
    `TruncatedSignals` so the registry marks the poll non-authoritative. Found in review
    (GPT 5.6).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_datadog_marks_a_full_page_as_truncated(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import secrets
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            datadog,
            set_top_level,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            DEFAULT_POLL_LIMIT,
            TruncatedSignals,
        )

        set_top_level("providers", {"datadog": {"enabled": True}})
        secrets.put_secret("datadog", "api_key", "k" * 32)
        secrets.put_secret("datadog", "app_key", "a" * 40)

        over = [
            {"id": str(i), "overall_state": "Alert", "name": f"m{i}"}
            for i in range(DEFAULT_POLL_LIMIT + 1)
        ]
        with mock.patch.object(datadog, "request_json", return_value=over):
            result = datadog.DatadogAdapter()._poll_sync()
        self.assertIsInstance(result, TruncatedSignals)

        under = [{"id": str(i), "overall_state": "Alert", "name": f"m{i}"} for i in range(5)]
        with mock.patch.object(datadog, "request_json", return_value=under):
            result = datadog.DatadogAdapter()._poll_sync()
        self.assertNotIsInstance(result, TruncatedSignals)

    def test_datadog_requests_one_past_the_cap(self):
        """The raw page, not the filtered count, is the detector — so it must OVER-fetch."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import secrets
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            datadog,
            set_top_level,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            POLL_FETCH_LIMIT,
        )

        set_top_level("providers", {"datadog": {"enabled": True}})
        secrets.put_secret("datadog", "api_key", "k" * 32)
        secrets.put_secret("datadog", "app_key", "a" * 40)
        captured: dict = {}

        def _fake(url, *, headers=None, params=None, **kw):
            captured["params"] = params
            return []

        with mock.patch.object(datadog, "request_json", side_effect=_fake):
            datadog.DatadogAdapter()._poll_sync()
        self.assertEqual(captured["params"]["page_size"], POLL_FETCH_LIMIT)

    def test_pagerduty_marks_more_true_as_truncated(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import secrets
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            pagerduty,
            set_top_level,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            TruncatedSignals,
        )

        set_top_level("providers", {"pagerduty": {"enabled": True}})
        secrets.put_secret("pagerduty", "api_token", "t" * 20)

        body = {"incidents": [{"id": str(i), "title": f"i{i}"} for i in range(50)], "more": True}
        with mock.patch.object(pagerduty, "request_json", return_value=body):
            result = pagerduty.PagerDutyAdapter()._poll_sync()
        self.assertIsInstance(
            result,
            TruncatedSignals,
            "PagerDuty's `more` flag means additional open incidents exist that this poll "
            "cannot see, so the poll must not read as a complete snapshot",
        )

        body["more"] = False
        with mock.patch.object(pagerduty, "request_json", return_value=body):
            result = pagerduty.PagerDutyAdapter()._poll_sync()
        self.assertNotIsInstance(result, TruncatedSignals)

    async def test_github_marks_a_full_page_as_truncated(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            github_issues,
            set_top_level,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            DEFAULT_POLL_LIMIT,
            TruncatedSignals,
        )

        set_top_level("providers", {"github-issues": {"enabled": True, "repo": "o/r"}})

        def _issues(n: int) -> str:
            return json.dumps(
                [
                    {"number": i, "title": f"#{i}", "labels": [], "createdAt": "", "url": "",
                     "assignees": []}
                    for i in range(n)
                ]
            )

        async def _fake_over(args):
            return 0, _issues(DEFAULT_POLL_LIMIT + 1), ""

        with mock.patch.object(github_issues, "_run_gh", side_effect=_fake_over):
            result = await github_issues.GitHubIssuesAdapter().poll()
        self.assertIsInstance(result, TruncatedSignals)

        async def _fake_under(args):
            return 0, _issues(3), ""

        with mock.patch.object(github_issues, "_run_gh", side_effect=_fake_under):
            result = await github_issues.GitHubIssuesAdapter().poll()
        self.assertNotIsInstance(result, TruncatedSignals)
