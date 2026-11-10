"""Which OTLP collectors metrics may reach: ``capabilities.telemetry`` ``otlp_hosts``.

A policy names collector hosts in the row's inner ``otlp_hosts`` ruleset (RFC
crew-lts-edition, appendix row "Diagnostic telemetry target"). Two places
hold metrics to it:

* :func:`governed_destinations` -- before a reader is built, a destination whose
  host the policy does not permit gets no reader;
* :func:`gate_exporter` -- before every export of a reader already running, so in
  a process with a platform context a policy installed at runtime
  (``policy_distribution.apply_ceiling``) stops the next export, the shutdown
  flush included, without waiting for a rebuild. The MCP gateway daemon has no
  context and gets no runtime install; it keeps its composed companion's ceiling
  until it restarts.

Policy layer only, like the beacon probe: both run process-wide with no session,
so a per-surface profile is not asked. No ceiling is still asked through
``resolve``, so the answer it gives with no policy holds; a ceiling without the row
or the ruleset permits every host. An evaluation error denies (egress fails closed;
local collection carries on). Enforcement decisions are SEL-audited: each build
decision, and each change of a running reader's answer -- not every export, which
would write a row per export interval.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: Governed scope whose inner ``otlp_hosts`` ruleset names the permitted collectors.
TELEMETRY_SCOPE = "capabilities.telemetry"

#: A credential redaction function, as ``CredentialPolicy.redact``.
Redactor = Callable[[str], str]

#: Tool name on the SEL ``governance_decision`` row for these decisions.
AUDIT_TOOL = "telemetry_otlp_export"

#: Stands for a ceiling that could not be established. Every host is refused under
#: it: a failure to load the policy must not read as "no policy".
UNEVALUABLE = object()

#: Hosts already reported as dropped, so a pinned list warns once, not per read.
_dropped_hosts_warned: set[str] = set()


def hosts_of(endpoint: object) -> "tuple[str, ...]":
    """Every host a client could read an OTLP endpoint as (``url_policy_hosts``)."""
    from kiro_crew.platform.governance_parts.matching import url_policy_hosts

    return url_policy_hosts(endpoint)


#: The destination the policy's ``capabilities.telemetry.otlp_endpoint`` supplies,
#: as the Privacy panel lists it.
POLICY_DESTINATION_NAME = "policy"


def policy_endpoint(ceiling: Any) -> str:
    """``capabilities.telemetry.otlp_endpoint`` under *ceiling*; empty outside enterprise."""
    from kiro_crew.platform.governance import telemetry_otlp_endpoint

    return telemetry_otlp_endpoint(ceiling)


def policy_collector(ceiling: Any) -> "tuple[str, bool]":
    """The policy's collector under *ceiling*, and whether ``otlp_hosts`` permits it.

    For a rebuild fingerprint: a policy that only starts permitting the host must
    move it too, since no reader exists yet to notice. Not audited.
    """
    endpoint = policy_endpoint(ceiling)
    return endpoint, bool(endpoint) and _decide(ceiling, hosts_of(endpoint))[0]


def policy_destination(endpoint: str) -> Any:
    """The ``OtlpDestination`` for the policy's collector at *endpoint*.

    Metrics only, and no process-wide OTel header or credential variable: local
    environment must not change what reaches the admin's collector.
    """
    from kiro_crew.platform.interfaces import OtlpDestination

    return OtlpDestination(
        name=POLICY_DESTINATION_NAME,
        endpoint=endpoint,
        signals=frozenset({"metrics"}),
        process_env_credentials=False,
    )


def _decide(ceiling: Any, hosts: "tuple[str, ...]") -> "tuple[bool, str, str, str]":
    """``(permitted, host, rule, reason)``: permitted only if EVERY reading is.

    *host* is the reading the answer rests on: the first refused one, else the
    first. Fails closed.
    """
    from kiro_crew.platform.governance import resolve

    first = hosts[0] if hosts else ""
    if ceiling is UNEVALUABLE:
        return False, first, "default", "governance unavailable (fail-closed)"
    try:
        for host in hosts or ("",):
            decision = resolve(ceiling, None, TELEMETRY_SCOPE, f"otlp_hosts:{host}")
            if not decision.permitted:
                return False, host, str(decision.rule), str(decision.reason)
    except Exception:
        logger.debug("OTLP host governance evaluation failed; denying")
        return False, first, "default", "governance unavailable (fail-closed)"
    return True, first, str(decision.rule), str(decision.reason)


def _audit(
    host: str, permitted: bool, rule: str, reason: str, redact: "Redactor | None" = None
) -> None:
    """Best-effort SEL row for one enforcement decision. Never raises.

    *redact* is the companion's redaction in a process with no platform context,
    where the SEL's default shim would raise; ``None`` everywhere else.
    """
    try:
        # Looked up at call time: the suite patches ``kiro_crew.sel.sel``.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key="",
            tool_name=AUDIT_TOOL,
            scope=TELEMETRY_SCOPE,
            item=f"otlp_hosts:{host}",
            outcome="allowed" if permitted else "denied",
            rule=rule,
            layer="policy",
            reason=reason,
            redact=redact,
        )
    except Exception:
        pass


def governed_destinations(
    supplied: "Iterable[Any]",
    ceiling: Any,
    *,
    audit: bool = False,
    redact: "Redactor | None" = None,
) -> tuple:
    """Keep the destinations whose host *ceiling* permits.

    *audit* is for the build that acts on the answer.
    """
    allowed = []
    for dest in supplied:
        # No ceiling is still asked through ``resolve``, so whatever the evaluator
        # answers with no policy holds here too, and is audited like any other.
        permitted, host, rule, reason = _decide(ceiling, hosts_of(getattr(dest, "endpoint", "")))
        if audit:
            _audit(host, permitted, rule, reason, redact)
        if permitted:
            allowed.append(dest)
        elif host not in _dropped_hosts_warned:
            # The host only, never the endpoint: it can carry userinfo or a signed query.
            _dropped_hosts_warned.add(host)
            logger.warning(
                "OTLP destination %r (host %r) dropped: the security policy does not "
                "permit it (capabilities.telemetry otlp_hosts)",
                getattr(dest, "name", ""),
                host,
            )
    return tuple(allowed)


def gate_exporter(
    exporter: Any,
    endpoint: object,
    ceiling: "Callable[[], Any]",
    *,
    redact: "Redactor | None" = None,
    named_by: "Callable[[Any], str] | None" = None,
) -> None:
    """Hold every ``export`` of *exporter* to the policy *ceiling()* returns now.

    The reader calls ``exporter.export`` on each interval and once more on
    shutdown; a refused call drops that batch and returns ``FAILURE`` without
    contacting the collector. The reader was built under a permit, so the first
    refusal (and a later return to permitted) is audited and logged once.

    *named_by* is for a destination the policy itself names: given the ceiling,
    it returns the endpoint the policy names now, and an export is refused while
    that is not *endpoint*, so a policy that moves or drops its collector stops
    the old reader before the next rebuild.
    """
    hosts = hosts_of(endpoint)
    inner = exporter.export
    last = {"permitted": True}

    def export(metrics_data: Any, *args: Any, **kwargs: Any) -> Any:
        current = ceiling()
        permitted, host, rule, reason = _decide(current, hosts)
        if permitted and named_by is not None:
            try:
                still_named = named_by(current) == endpoint
            except Exception:
                still_named = False
            if not still_named:
                permitted, rule = False, "policy"
                reason = "the policy no longer names this collector"
        if permitted != last["permitted"]:
            last["permitted"] = permitted
            _audit(host, permitted, rule, reason, redact)
            logger.warning(
                "OTLP export to host %r %s by the security policy (capabilities.telemetry)",
                host,
                "resumed" if permitted else "stopped",
            )
        if not permitted:
            from opentelemetry.sdk.metrics.export import MetricExportResult

            return MetricExportResult.FAILURE
        return inner(metrics_data, *args, **kwargs)

    exporter.export = export
