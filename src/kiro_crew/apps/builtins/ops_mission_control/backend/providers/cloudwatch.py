"""AWS CloudWatch adapters — alarms as signals, logs and metrics as evidence.

**No credential is ever stored for AWS.** This adapter uses the ambient
credential chain — the user's existing profile, assumed role, or instance role —
a direct application of "IAM roles over long-lived keys". The app
does not accept, persist, or transmit an AWS access key, so there is no AWS
credential in the threat model at all.

Required read-only permissions, which the user attaches to their own principal:

    cloudwatch:DescribeAlarms, cloudwatch:GetMetricStatistics
    logs:StartQuery, logs:GetQueryResults, logs:DescribeLogGroups

No write permission is requested. Resolving a CloudWatch alarm is not something
this app does — it resolves *work items* in trackers, through ``ActionSink``.

``boto3`` is an **optional lazy import** (matching the existing STT precedent): the
module must import cleanly without it and report unconfigured, so a user who never
touches AWS pays nothing and sees no error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from types import MappingProxyType
from typing import Any, Mapping

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATE_FIRING,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_flag,
    config_list,
    config_value,
    provider_enabled,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_POLL_LIMIT,
    Evidence,
    EvidenceBudget,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "cloudwatch"

#: Shape of an AWS region name: ``us-east-1``, ``eu-west-2``, ``ap-southeast-3``,
#: ``us-gov-west-1``, ``cn-north-1``. Letters and digits in hyphen-separated groups,
#: nothing else.
#:
#: This is the same class of defect as the Datadog ``site`` finding, caught by auditing for
#: it rather than waiting for it to be reported. ``region`` is ordinary non-secret config
#: the agent can write through ``PUT /providers/<id>/config``, and it is interpolated into
#: the CloudWatch console HOSTNAME (``https://{region}.console.aws.amazon.com/…``). Measured:
#: ``region="evil#"`` renders ``https://evil#.console.aws.amazon.com/…``, whose real host —
#: confirmed with ``urlsplit`` — is ``evil``, because ``#`` starts the fragment and truncates
#: everything after it. ``region="attacker.example.com"`` yields
#: ``attacker.example.com.console.aws.amazon.com``. Either puts an attacker-controlled link
#: on the incident board labelled "open in provider", which an operator is meant to click.
#:
#: A SHAPE check rather than an allowlist: AWS adds regions regularly, and a stale list would
#: silently break a legitimate install — the failure mode an allowlist is only acceptable for
#: when the set is genuinely closed (Datadog's published sites are; AWS regions are not).
#: Every injection shape that matters (``/``, ``#``, ``?``, ``@``, ``:``, ``.``) is excluded
#: by construction.
_REGION_RE = re.compile(r"^[a-z]{2,}(?:-[a-z0-9]+)+$")


def _validated_region(raw: str | None) -> str:
    """``raw`` if it is region-shaped, else "".

    Returns "" rather than raising, and "" is already the "no region configured" path
    everywhere it is used: ``_console_url`` renders no link and boto3 falls back to its own
    resolution chain. A bogus region must degrade to "no link" and never to "a link
    somewhere else".

    Takes the value rather than reading config itself, because ``region`` is readable from
    TWO namespaces (the signal source's and the evidence adapter's, via ``_evidence_value``)
    and both must pass through the same check.
    """
    # Lower-cased first: AWS region names are canonically lowercase and boto3 is
    # case-sensitive, so `US-EAST-1` is not a valid region — but it IS an obvious operator
    # typo, and silently dropping it (rendering no link, with a warning in a log nobody is
    # reading) is worse than accepting what they plainly meant. Normalising cannot widen the
    # gate: casefolding introduces none of the characters the shape check excludes.
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if _REGION_RE.match(value):
        return value
    logger.warning(
        "ops-mission-control: ignoring malformed cloudwatch region %r — a region name is "
        "letters and digits in hyphen-separated groups (e.g. us-east-1)",
        value,
    )
    return ""


def _region() -> str:
    """The signal source's configured region, shape-checked."""
    return _validated_region(config_value(PROVIDER_ID, "region"))


#: Alarm state that constitutes work. ``INSUFFICIENT_DATA`` is deliberately NOT
#: included: it usually means a metric stopped reporting, which is real but
#: produces enormous noise on any account with idle resources. Users who want it
#: opt in via ``include_insufficient_data``.
_ALARM_STATE_ALARM = "ALARM"
_ALARM_STATE_INSUFFICIENT = "INSUFFICIENT_DATA"

#: Page ceiling for `describe_alarms`. Paging exists so an estate larger than one page is not
#: reported as a complete snapshot; this bounds it so a provider that keeps returning a token
#: cannot spin. 20 pages x 100 records is 2000 alarms — far past `DEFAULT_POLL_LIMIT`, so a real
#: install always breaks out on the cap first and only a misbehaving API reaches this.
_MAX_ALARM_PAGES = 20

#: How far back log evidence looks. Long enough to cover the alarm's evaluation
#: window, short enough that a Logs Insights query stays cheap.
_LOG_LOOKBACK_MINUTES = 30

#: Log lines kept per query. The evidence budget also caps total bytes; this caps
#: rows so one chatty log group cannot crowd out every other source.
_LOG_LINE_LIMIT = 40

#: Poll interval for a Logs Insights query to finish, and its ceiling.
_LOG_POLL_INTERVAL_SECS = 1.0
_LOG_MAX_WAIT_SECS = 25.0


def _boto3_client(service: str, region: str) -> Any | None:
    """Build a boto3 client, or ``None`` when boto3/credentials are unavailable.

    Lazy import by design: ``boto3`` is an optional dependency, and a user with no
    AWS setup must not see an import error — the adapter simply reports
    unconfigured.
    """
    try:
        import boto3  # noqa: PLC0415 — optional lazy import
    except ImportError:
        logger.debug("ops-mission-control: boto3 not installed; cloudwatch unavailable")
        return None
    try:
        kwargs: dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        # Prefer the evidence namespace when it is set, so the ``profile`` field the
        # evidence adapter advertises is actually honored; otherwise fall back to the
        # signal source's, which is where a single-account install configures it.
        profile = config_value(EVIDENCE_PROVIDER_ID, "profile") or config_value(
            PROVIDER_ID, "profile"
        )
        if profile:
            session = boto3.Session(profile_name=profile, **kwargs)
            return session.client(service)
        return boto3.client(service, **kwargs)
    except Exception:  # noqa: BLE001 — missing/expired credentials, bad profile, …
        logger.exception("ops-mission-control: failed to build %s client", service)
        return None


def _severity_for(alarm: dict[str, Any]) -> str:
    """Derive severity from the alarm itself.

    CloudWatch has no severity concept, so we read one from an ``omc:severity``
    tag-style dimension if present and otherwise infer from the alarm name. This
    is a heuristic and is documented as such in the skill — a user who cares sets
    the dimension.
    """
    name = str(alarm.get("AlarmName", "")).lower()
    for dimension in alarm.get("Dimensions") or []:
        if str(dimension.get("Name", "")).lower() in {"omc:severity", "severity"}:
            return str(dimension.get("Value", ""))
    if any(token in name for token in ("critical", "sev1", "p1", "pager", "urgent")):
        return SEVERITY_CRITICAL
    return SEVERITY_WARNING


class CloudWatchSignalSource:
    """CloudWatch alarms in ALARM state, as signals."""

    id = PROVIDER_ID
    display_name = "AWS CloudWatch"
    detail = (
        "Alarms in ALARM state. Uses your ambient AWS credentials — no key is stored. "
        "Set include_insufficient_data to also catch alarms whose metric STOPPED "
        "reporting (a pipeline that silently stopped running looks healthy otherwise) "
        "— off by default because it is noisy on accounts with idle resources."
    )
    config_fields: tuple[str, ...] = (
        "enabled",
        "region",
        "profile",
        "alarm_name_prefix",
        "alarm_names",
        "include_insufficient_data",
    )
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID)

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[Signal]:
        region = _region()
        client = _boto3_client("cloudwatch", region)
        if client is None:
            # RAISE, do not return []. `registry.poll_all` marks a source unhealthy only
            # when `poll()` raises; a swallowed failure returning an empty list is recorded
            # as a SUCCESSFUL poll that saw nothing — indistinguishable from "nothing is
            # firing". With expired credentials that reads as an all-clear over a live
            # estate, and `all_sources_healthy` then promises absence-means-recovery, which
            # is the one claim this app must never make falsely. Found in review; it is the
            # same "machinery that looks deliberate while doing nothing" defect the
            # SignalsPanel `ready / ok` row was.
            #
            # `configured()` is the honest place to say "no AWS here" — boto3 missing or no
            # credential chain surfaces as a per-source error the Signals tab renders, not
            # as a clean poll.
            raise RuntimeError(
                "cloudwatch client unavailable — boto3 missing, or no usable credentials "
                "(check the profile/role and whether the session has expired)"
            )

        states = [_ALARM_STATE_ALARM]
        if config_flag(PROVIDER_ID, "include_insufficient_data"):
            states.append(_ALARM_STATE_INSUFFICIENT)

        signals: list[Signal] = []
        for state in states:
            kwargs: dict[str, Any] = {"StateValue": state, "MaxRecords": DEFAULT_POLL_LIMIT}
            prefix = config_value(PROVIDER_ID, "alarm_name_prefix")
            if prefix:
                kwargs["AlarmNamePrefix"] = prefix
            names = config_list(PROVIDER_ID, "alarm_names")
            if names:
                kwargs["AlarmNames"] = names[:100]
                kwargs.pop("AlarmNamePrefix", None)
            # FOLLOW `NextToken`. `describe_alarms` paginates, and reading only the first page
            # stopped at `MaxRecords` with nothing indicating more existed — so an estate with
            # more firing alarms than the cap under-returned while `poll_all` still recorded
            # `snapshot=True`, and `reconcile` terminally resolved the omitted live incidents.
            # That is the same "a partial snapshot must not look complete" rule the `except`
            # below already enforces for a FAILED call, applied to the case where every call
            # succeeds and the estate is simply larger than one page.
            #
            # Paged to `DEFAULT_POLL_LIMIT + 1`, NOT to exhaustion: one item past the cap is all
            # `poll_all` needs to see the result is over-limit and mark the poll
            # non-authoritative, while draining an unbounded estate would trade this bug for the
            # memory/rate-limit one the cap exists to prevent. `_MAX_ALARM_PAGES` additionally
            # bounds a provider that keeps handing back a token. Found in review (GPT 5.6).
            for page in range(_MAX_ALARM_PAGES):
                try:
                    response = client.describe_alarms(**kwargs)
                except Exception as exc:
                    # PROPAGATE. Returning the signals gathered so far reported a partial
                    # snapshot as a complete, healthy one: with `include_insufficient_data`
                    # on, this loop runs twice, so a failure on the second state silently
                    # truncated the estate — and the caller cannot tell a short list from a
                    # quiet one. `poll_all` needs the raise to record the source as unhealthy
                    # and arm its backoff window; the operator then sees "cloudwatch did not
                    # answer" rather than a board that looks calm.
                    logger.exception("ops-mission-control: describe_alarms failed")
                    raise RuntimeError(f"cloudwatch describe_alarms failed: {exc}") from exc

                signals.extend(self._alarms_to_signals(response, region, state))

                token = str(response.get("NextToken") or "")
                if not token:
                    break
                if len(signals) > DEFAULT_POLL_LIMIT:
                    # Over the cap already: `poll_all` will slice and flag this poll, so more
                    # pages would cost API calls for signals that get dropped anyway.
                    break
                if page + 1 >= _MAX_ALARM_PAGES:
                    # Bounded out with pages still pending and NOT over the cap, so nothing
                    # downstream would notice the shortfall. Raise instead: an under-reported
                    # estate that looks complete is the bug this whole block exists to close,
                    # and `poll_all` turns the raise into "cloudwatch did not answer".
                    raise RuntimeError(
                        f"cloudwatch returned more than {_MAX_ALARM_PAGES} pages of alarms "
                        "without reaching the poll cap; refusing to report a partial estate "
                        "as a complete snapshot"
                    )
                kwargs["NextToken"] = token
            kwargs.pop("NextToken", None)
        return signals

    def _alarms_to_signals(
        self, response: Mapping[str, Any], region: str, state: str
    ) -> list[Signal]:
        """One page of ``describe_alarms`` output as signals.

        Extracted so the paging loop above reads as paging. Skips an alarm with no name: it
        cannot be addressed, deduped, or linked, so a nameless row is not actionable work.
        """
        out: list[Signal] = []
        for alarm in response.get("MetricAlarms", []):
            name = str(alarm.get("AlarmName", ""))
            if not name:
                continue
            namespace = str(alarm.get("Namespace", ""))
            metric = str(alarm.get("MetricName", ""))
            resource = f"{namespace}/{metric}" if namespace or metric else name
            updated = alarm.get("StateUpdatedTimestamp")
            out.append(
                Signal.create(
                    source=PROVIDER_ID,
                    native_id=f"alarm/{name}",
                    title=str(alarm.get("AlarmDescription") or name),
                    severity=_severity_for(alarm),
                    state=STATE_FIRING,
                    fired_at=updated.strftime("%Y-%m-%dT%H:%M:%SZ") if updated else "",
                    resource=resource,
                    url=self._console_url(region, name),
                    labels={
                        "alarm_name": name,
                        "namespace": namespace,
                        "metric": metric,
                        "region": region,
                        "state": state,
                    },
                    # The alarm ARN's stable part: region + name identifies this
                    # alarm exactly, where the fingerprint only captures the shape
                    # of its description (and strips the threshold digits, so two
                    # alarms on the same metric at different thresholds collide).
                    provider_key=f"{region}/{name}" if name else "",
                )
            )
        return out

    @staticmethod
    def _console_url(region: str, alarm_name: str) -> str:
        if not region:
            return ""
        from urllib.parse import quote

        return (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#alarmsV2:alarm/{quote(alarm_name)}"
        )


#: The evidence adapter's own config namespace. It advertises ``config_fields``, so
#: the Settings UI writes to ``providers["cloudwatch-evidence"]`` — but the gather
#: code read ``providers["cloudwatch"]``, so ``log_groups`` (which exists ONLY on this
#: adapter) could never be set through the UI at all: whatever the operator typed
#: landed where nothing looked for it, and log evidence was silently always empty.
EVIDENCE_PROVIDER_ID = "cloudwatch-evidence"


def _evidence_value(field: str) -> str:
    """Read a field from the evidence namespace, falling back to the signal one.

    The fallback keeps ``region`` / ``profile`` working for an install that already
    configured them on ``cloudwatch`` (the common case — one AWS account serves both
    adapters), while ``log_groups`` now resolves where the UI actually writes it.
    """
    value = config_value(EVIDENCE_PROVIDER_ID, field)
    return value if value else config_value(PROVIDER_ID, field)


def _evidence_list(field: str) -> list[str]:
    values = config_list(EVIDENCE_PROVIDER_ID, field)
    return values if values else config_list(PROVIDER_ID, field)


class CloudWatchEvidenceSource:
    """Alarm history and recent log lines, as investigation evidence."""

    id = EVIDENCE_PROVIDER_ID
    display_name = "AWS CloudWatch evidence"
    detail = "Alarm history plus recent matching log lines."
    config_fields: tuple[str, ...] = ("enabled", "region", "profile", "log_groups")

    #: What this adapter needs, clamped by the operator's ceiling in
    #: ``EvidenceBudget.for_source``. Logs Insights is submit-then-poll rather than a
    #: single request, so it wants longer than a plain REST adapter — the reason
    #: ``_LOG_MAX_WAIT_SECS`` existed at all. It can still only ever NARROW the
    #: operator's value, never raise it.
    #: A MappingProxy, not a dict: a mutable class attribute shared across every
    #: instance is one accidental ``hint["timeout_secs"] = 300`` away from an adapter
    #: rewriting its own ceiling at runtime.
    evidence_budget_hint: Mapping[str, float] = MappingProxyType(
        {"timeout_secs": _LOG_MAX_WAIT_SECS}
    )
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        # Either namespace enabling it is enough: the signal source and this adapter
        # share one AWS account, and requiring a second toggle for the same account
        # would make evidence silently absent for anyone who enabled only CloudWatch.
        return provider_enabled(EVIDENCE_PROVIDER_ID) or provider_enabled(PROVIDER_ID)

    async def gather(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        if not self.configured() or signal.source != PROVIDER_ID:
            return []
        return await asyncio.to_thread(self._gather_sync, signal, budget)

    def _gather_sync(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        # Shape-checked like the signal source's, even though this one only reaches boto3
        # and never a rendered URL: a malformed region here misdirects endpoint resolution,
        # and "" is the honest value for "we could not read a region" on both paths. The
        # evidence namespace is a SECOND place the agent can write it, so validating only
        # the signal source's read would leave the same field unguarded one namespace over.
        region = _validated_region(_evidence_value("region"))
        out: list[Evidence] = []
        calls = 0

        alarm_name = signal.labels.get("alarm_name", "")
        if alarm_name and calls < budget.max_calls:
            client = _boto3_client("cloudwatch", region)
            calls += 1
            if client is not None:
                try:
                    history = client.describe_alarm_history(AlarmName=alarm_name, MaxRecords=10)
                    lines = [
                        f"{item.get('Timestamp')} {item.get('HistorySummary', '')}"
                        for item in history.get("AlarmHistoryItems", [])
                    ]
                    if lines:
                        out.append(
                            Evidence(
                                source=self.id,
                                kind="alarm_history",
                                title=f"Alarm history — {alarm_name}",
                                body="\n".join(lines),
                            )
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("ops-mission-control: alarm history failed")

        log_groups = _evidence_list("log_groups")
        for group in log_groups:
            if calls >= budget.max_calls:
                break
            calls += 1
            body = self._query_logs(region, group, budget)
            if body:
                out.append(
                    Evidence(
                        source=self.id,
                        kind="logs",
                        title=f"Recent errors — {group}",
                        body=body,
                    )
                )
        return out

    def _query_logs(self, region: str, log_group: str, budget: EvidenceBudget) -> str:
        """Run a bounded Logs Insights query for recent error-ish lines."""
        client = _boto3_client("logs", region)
        if client is None:
            return ""
        try:
            now = int(time.time())
            start = client.start_query(
                logGroupName=log_group,
                startTime=now - _LOG_LOOKBACK_MINUTES * 60,
                endTime=now,
                queryString=(
                    "fields @timestamp, @message "
                    "| filter @message like /(?i)(error|exception|timeout|fail)/ "
                    "| sort @timestamp desc "
                    f"| limit {_LOG_LINE_LIMIT}"
                ),
            )
            query_id = start.get("queryId")
            if not query_id:
                return ""
            waited = 0.0
            budget_wait = min(_LOG_MAX_WAIT_SECS, budget.timeout_secs)
            while waited < budget_wait:
                result = client.get_query_results(queryId=query_id)
                status = str(result.get("status", ""))
                if status == "Complete":
                    rows = result.get("results", [])
                    lines = [
                        " ".join(str(f.get("value", "")) for f in row if f.get("field") != "@ptr")
                        for row in rows
                    ]
                    return "\n".join(lines)[: budget.max_bytes]
                if status in {"Failed", "Cancelled", "Timeout"}:
                    return ""
                time.sleep(_LOG_POLL_INTERVAL_SECS)
                waited += _LOG_POLL_INTERVAL_SECS
            # Out of budget: stop the query rather than leaving it running and
            # billable after we have stopped caring about the answer.
            try:
                client.stop_query(queryId=query_id)
            except Exception:  # noqa: BLE001
                logger.debug("ops-mission-control: stop_query failed", exc_info=True)
            return ""
        except Exception:  # noqa: BLE001
            logger.exception("ops-mission-control: logs query failed for %r", log_group)
            return ""
