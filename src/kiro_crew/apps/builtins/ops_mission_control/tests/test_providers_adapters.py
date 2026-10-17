"""Tests for what each vendor adapter may reach and what it must report.

Datadog keys reach only the configured Datadog host, the CloudWatch console link cannot be
redirected, PagerDuty abstains without an identity instead of borrowing a teammate's shift, a
CloudWatch fault or a capped poll is never read as a quiet estate, and a timed-out ``gh`` is
reaped. The provider seam itself (registry, fan-out, redaction) is in ``test_providers.py``.
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.registry import (
    OpsProviderRegistry,
)


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
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        raw_reads = [
            line.strip()
            for line in code.splitlines()
            if "region = " in line and "_validated_region" not in line and "_region()" not in line
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
                    {
                        "number": i,
                        "title": f"#{i}",
                        "labels": [],
                        "createdAt": "",
                        "url": "",
                        "assignees": [],
                    }
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


class TestRunGhTimeoutReapsChild(unittest.IsolatedAsyncioTestCase):
    """A timed-out ``gh`` must be tree-killed and reaped by draining pipes.

    After ``wait_for`` cancels ``communicate()``, a killed child blocked
    writing into a full stderr pipe makes a bare ``wait()`` hang the polling
    task forever — the reap must be a SECOND ``communicate()``.
    """

    async def test_timeout_reaps_child_via_communicate_not_wait(self):
        from kiro_crew import platform_compat
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            github_issues,
        )

        class HangProc:
            # No supported OS can allocate this PID, so the host process table cannot
            # make the fake child look like it shares the test runner's process group.
            pid = 99_999_999_999
            returncode: int | None = None
            kill_calls = 0
            wait_calls = 0
            communicate_calls = 0

            async def communicate(self):
                HangProc.communicate_calls += 1
                if HangProc.communicate_calls == 1:
                    raise asyncio.TimeoutError
                HangProc.returncode = -9
                return b"", b""

            def kill(self):
                HangProc.kill_calls += 1

            async def wait(self):
                HangProc.wait_calls += 1
                return -9

        async def fake_spawn(*argv, **kwargs):
            return HangProc()

        killed: list[tuple[int, int]] = []

        async def fake_tree_kill(pid, sig):
            killed.append((pid, sig))
            return True

        with (
            mock.patch.object(
                github_issues,
                "sandboxed_spawn_argv",
                lambda argv, **kw: (argv, {}, None),
            ),
            mock.patch.object(github_issues, "create_subprocess_limited", fake_spawn),
            mock.patch.object(platform_compat, "kill_process_tree_async", fake_tree_kill),
        ):
            rc, out, err = await github_issues._run_gh(["issue", "list"])

        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("timed out", err)
        self.assertEqual(killed, [(HangProc.pid, platform_compat.SIGKILL)])
        self.assertEqual(HangProc.kill_calls, 1)
        self.assertEqual(HangProc.communicate_calls, 2)
        self.assertEqual(HangProc.wait_calls, 0)
