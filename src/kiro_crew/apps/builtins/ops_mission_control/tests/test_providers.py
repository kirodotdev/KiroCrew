"""Tests for the provider seam: ADD-only registry, fan-out resilience, adapters.

The ADD-only test is the one that protects the edition boundary: if a companion
could replace a core adapter, auditing what the public core does would require
auditing every companion too.

The fan-out tests cover the property that keeps the heartbeat alive — one broken
or hanging provider must degrade to a per-source error, never take down the poll.
"""

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

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
            title="alarm glpat-IOSFODNN7EXAMPLE tripped",
            body="ok",
        )
        registry.register_evidence_source(_FakeEvidence("x", [leaky]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget())
        self.assertNotIn("glpat-IOSFODNN7EXAMPLE", out[0].title)
        self.assertIn("tripped", out[0].title)  # redaction, not truncation

    async def test_body_is_truncated_to_budget(self):
        registry = OpsProviderRegistry()
        big = Evidence(source="x", kind="logs", title="t", body="a" * 5000)
        registry.register_evidence_source(_FakeEvidence("x", [big]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget(max_bytes=100))
        self.assertLessEqual(len(out[0].body), 100)

    async def test_aws_keys_are_redacted_not_just_bearer_tokens(self):
        """Log lines are where glpat-shaped keys turn up by accident."""
        registry = OpsProviderRegistry()
        leaky = Evidence(
            source="x",
            kind="logs",
            title="t",
            body="AccessDenied for glpat-IOSFODNN7EXAMPLE calling sts:AssumeRole",
        )
        registry.register_evidence_source(_FakeEvidence("x", [leaky]))
        out = await registry.gather_evidence(_signal(), EvidenceBudget())
        self.assertNotIn("glpat-IOSFODNN7EXAMPLE", out[0].body)

    async def test_budget_holds_even_when_redaction_expands_the_body(self):
        """Redaction runs BEFORE the cap, so markers cannot push past the budget.

        A marker is longer than most of what it replaces. Capping first and redacting
        second let the emitted body exceed `max_bytes` (measured ~1.09x on an
        all-credential body) — and the budget exists to bound what reaches the
        model's context, so it has to bound the text actually emitted.
        """
        registry = OpsProviderRegistry()
        body = " ".join(["glpat-IOSFODNN7EXAMPLE"] * 200)
        registry.register_evidence_source(
            _FakeEvidence("x", [Evidence(source="x", kind="logs", title="t", body=body)])
        )
        out = await registry.gather_evidence(_signal(), EvidenceBudget(max_bytes=1000))
        self.assertLessEqual(len(out[0].body), 1000)
        self.assertNotIn("glpat-IOSFODNN7EXAMPLE", out[0].body)

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
            incidentio,
            pagerduty,
            webhook,
        )

        tmp = Path(tempfile.mkdtemp())
        try:
            for adapter in (
                cloudwatch.CloudWatchSignalSource(),
                cloudwatch.CloudWatchEvidenceSource(),
                pagerduty.PagerDutyAdapter(),
                incidentio.IncidentIoAdapter(),
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
            incidentio,
            pagerduty,
        )

        self.assertEqual(await cloudwatch.CloudWatchSignalSource().poll(), [])
        self.assertEqual(await pagerduty.PagerDutyAdapter().poll(), [])
        self.assertEqual(await incidentio.IncidentIoAdapter().poll(), [])

    async def test_public_registry_installs_expected_adapters(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry as reg

        reg.reset_registry()
        catalog = {p.id for p in reg.get_registry().catalog()}
        for expected in ("noop", "always-on", "cloudwatch", "pagerduty", "incidentio", "datadog"):
            self.assertIn(expected, catalog)
        reg.reset_registry()


class TestSuppressionIsAlwaysBounded(unittest.IsolatedAsyncioTestCase):
    """A suppression with no expiry hides a live fault until a human remembers it.

    This is the property that makes ``act`` a bounded bet rather than an
    all-or-nothing one: a WRONG silence expires by itself. POSTing ``/mute`` with
    ``body={}`` leaves Datadog reading the missing ``end`` as "mute forever" — the
    board shows the incident resolved while the metric stays bad, with no way back
    but a human noticing.
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
