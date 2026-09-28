"""Which temporality each instrument kind exports — one owner, both sinks.

Temporality is the difference between "the running total" (CUMULATIVE) and
"what changed since the last export" (DELTA). It is a per-DESTINATION encoding
choice, not a property of the instrument, which is exactly why it needs an
owner: Kiro Crew attaches TWO readers to one ``MeterProvider`` (the local JSONL
sink, always; an opt-in OTLP reader per destination an edition names), each with
its own exporter, and each exporter resolves temporality independently. Before
this module the map lived inline in the local exporter's ``__init__`` and the
OTLP leg passed nothing at all, so the two destinations reported the SAME
instruments differently — DELTA on disk, CUMULATIVE on the wire — with nothing
failing.

The rule, and why it is DELTA:

  * ``Counter`` / ``UpDownCounter`` / ``Histogram`` — DELTA. Aggregation over
    per-day, per-PID shards then reduces to an element-wise sum across cycles
    and processes, and stays correct across restarts and day boundaries. Under
    CUMULATIVE an idle series re-ships every bucket of every series on every
    cycle: a fixed per-host cost independent of traffic, and one that scales
    linearly with fleet size for data that did not change. Under DELTA an idle
    series sends nothing.
  * ``ObservableCounter`` — deliberately ABSENT, so it keeps the SDK default
    (CUMULATIVE). DELTA was tried and reverted: the delta baseline lives in the
    provider, and the recorder is rebuilt in-process whenever telemetry consent
    changes (``provider._consent_worker``), so the first post-rebuild collection
    would re-emit the entire process-lifetime total as ONE giant delta and
    inflate daily sums. The split is along sync vs async and it is the correct
    line — a sync ``Counter`` accumulates its own ``.add()`` calls and restarts
    at zero, so a fake delta is structurally impossible, while an observable
    callback reads an EXTERNAL process-lifetime total that a fresh baseline
    cannot explain. No instrument registers an observable counter today: the
    process CPU/GC readings and the inventory probe-failure count are observable
    GAUGES declared as lifetime totals, which is what removed the last cumulative
    series. So this entry is a guard for the next one rather than a live case.
  * Gauges carry no temporality at all and never belong in the map.

``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`` stays in control of the
OTLP leg. The env var IS read — by ``opentelemetry.exporter.otlp.proto.common``,
a sibling package of the http exporter (``OTLPMetricExporterMixin._get_temporality``),
which is why grepping the SDK tree and the http exporter tree alone finds only
the DECLARATION in ``opentelemetry.sdk.environment_variables`` and reads as
"nothing consumes it". Measured at exporter 1.44.0: unset resolves every kind
CUMULATIVE, ``DELTA`` resolves Counter/Histogram/ObservableCounter to DELTA, and
an explicitly passed dict is applied ON TOP of whichever base the env var chose
(``instrument_class_temporality.update(preferred_temporality or {})``).

That last detail is why :func:`otlp_preference` returns ``None`` rather than the
map when the operator has set the variable: passing the map unconditionally
would silently override an operator who asked for CUMULATIVE, turning a
documented escape hatch (match Datadog, CloudWatch, a Prometheus-style backend
without a code change) into a setting that works in one direction only. So the
default agrees with the local sink, and an operator who names a preference wins.

OSS-CLEAN: opentelemetry (Apache-2.0) + stdlib only.
"""

from __future__ import annotations

import os
from typing import Optional

from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
from opentelemetry.sdk.metrics.export import AggregationTemporality

#: The OpenTelemetry standard variable an operator uses to match a backend that
#: disagrees with our default. Named here (rather than spelled at each call
#: site) so the code that STANDS ASIDE for it and the guide that documents it
#: cannot drift apart.
TEMPORALITY_ENV_VAR = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"


def delta_preference() -> "dict[type, AggregationTemporality]":
    """The instrument-kind -> temporality map every Kiro Crew sink prefers.

    A FRESH dict per call: an exporter stores the mapping it is handed, so a
    shared module-level dict would let one exporter's (or one test's) mutation
    reach the other destination.
    """
    return {
        Counter: AggregationTemporality.DELTA,
        UpDownCounter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
    }


def otlp_preference() -> "Optional[dict[type, AggregationTemporality]]":
    """What to pass ``OTLPMetricExporter``, or None to leave it to the exporter.

    ``None`` means "pass nothing", which is not the same as passing an empty
    map: it is what hands the decision to the exporter's own env-var handling
    for every instrument kind at once.

    Any non-blank value of :data:`TEMPORALITY_ENV_VAR` counts as "the operator
    decided", including one the exporter does not recognise: it warns and falls
    back to CUMULATIVE, and a host that mistyped the variable must see that
    fallback rather than our default quietly taking over and hiding the typo.
    """
    if os.environ.get(TEMPORALITY_ENV_VAR, "").strip():
        return None
    return delta_preference()
