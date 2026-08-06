"""MetricsRecorder — thin facade over the OpenTelemetry SDK metrics API.

Every core or app metric flows through this facade so KiroCrew's namespace and
privacy guardrails (``validate_name`` / ``validate_attrs`` / ``redact`` from
``kiro_crew.metrics.schema``) run *before* the value reaches an OTEL instrument.
Namespace validation is exhaustive: ``validate_name`` rejects anything outside
the caller's namespace, so an out-of-namespace metric cannot be emitted.

Attribute redaction is defence in depth, not a guarantee. ``redact`` matches
known credential shapes and falls back to a Shannon-entropy heuristic whose
reach is bounded (see ``schema.py``), so a call site remains responsible for
passing only the declared low-cardinality constants the CARDINALITY note in
``schema.py`` requires. Do not treat this facade as a licence to hand it raw
values (security event logging: never log secrets or user PII).

Design:
  * OTEL instrument objects are created once per metric name and cached under a
    lock. The SDK warns on duplicate instrument creation, so the lock makes the
    check-then-create atomic: concurrent first-emit on the SAME name (dashboard +
    Slack + cron sessions all calling ensure_ready() at boot) creates exactly one
    instrument, never a duplicate. The caches are keyed by metric NAME and are
    not evicted, so callers MUST use low-cardinality constant names (see the
    cardinality note in ``schema.py``); bounded eviction is deferred to a later
    wave.
  * Every public method is best-effort: a telemetry failure NEVER propagates to
    the caller -- it is logged at WARNING and swallowed.
  * A recorder built with ``meter=None`` is a no-op recorder, used when telemetry
    is disabled so metric call sites cost nothing.

OSS-CLEAN: depends only on ``opentelemetry`` (Apache-2.0, CNCF) + the stdlib +
the first-party ``kiro_crew.metrics.schema`` guardrails. No Amazon-internal
imports.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Dict, Optional, Tuple, Union

if TYPE_CHECKING:
    # Annotation-only (``from __future__ import annotations`` defers evaluation).
    # Kept out of runtime imports so this module — and the no-op
    # ``MetricsRecorder(None)`` path provider.py falls back to — stays loadable
    # when opentelemetry is absent (env-closure drift).
    from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter

from kiro_crew.metrics.schema import validate_attrs, validate_name

logger = logging.getLogger(__name__)

AttrValue = Union[str, int, bool, float]
Attributes = Dict[str, AttrValue]


class MetricsRecorder:
    """Namespace- and privacy-guarded facade over an OTEL ``Meter``.

    Pass ``meter=None`` for a no-op recorder (telemetry disabled): every method
    returns immediately without touching the SDK.
    """

    def __init__(self, meter: Optional[Meter]) -> None:
        self._meter = meter
        self._counters: Dict[str, Counter] = {}
        self._updown: Dict[str, UpDownCounter] = {}
        self._histograms: Dict[str, Histogram] = {}
        # Guards the check-then-create in the instrument caches so concurrent
        # first-emit on the same metric name creates exactly one instrument.
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._meter is not None

    def _guard(
        self, name: str, attrs: Optional[Attributes], app_id: Optional[str]
    ) -> Tuple[str, Attributes]:
        """Validate the metric name + sanitize/redact attributes (C4 + privacy)."""
        validated = validate_name(name, app_id=app_id)
        safe = validate_attrs(attrs) if attrs else {}
        return validated, safe

    def counter(
        self,
        name: str,
        value: Union[int, float] = 1,
        *,
        unit: str = "1",
        attrs: Optional[Attributes] = None,
        app_id: Optional[str] = None,
        description: str = "",
    ) -> None:
        """Add *value* to the monotonic counter *name*."""
        if self._meter is None:
            return
        try:
            vname, safe = self._guard(name, attrs, app_id)
            inst = self._counters.get(vname)
            if inst is None:
                with self._lock:
                    inst = self._counters.get(vname)
                    if inst is None:
                        inst = self._meter.create_counter(
                            vname, unit=unit, description=description
                        )
                        self._counters[vname] = inst
            inst.add(value, attributes=safe)
        except Exception as exc:  # telemetry must never break the caller
            logger.warning("metrics counter %r failed: %s", name, exc)

    def up_down_counter(
        self,
        name: str,
        value: Union[int, float],
        *,
        unit: str = "1",
        attrs: Optional[Attributes] = None,
        app_id: Optional[str] = None,
        description: str = "",
    ) -> None:
        """Add *value* (may be negative) to the up/down counter *name*."""
        if self._meter is None:
            return
        try:
            vname, safe = self._guard(name, attrs, app_id)
            inst = self._updown.get(vname)
            if inst is None:
                with self._lock:
                    inst = self._updown.get(vname)
                    if inst is None:
                        inst = self._meter.create_up_down_counter(
                            vname, unit=unit, description=description
                        )
                        self._updown[vname] = inst
            inst.add(value, attributes=safe)
        except Exception as exc:
            logger.warning("metrics up_down_counter %r failed: %s", name, exc)

    def histogram(
        self,
        name: str,
        value: Union[int, float],
        *,
        unit: str = "ms",
        attrs: Optional[Attributes] = None,
        app_id: Optional[str] = None,
        description: str = "",
    ) -> None:
        """Record a *value* observation for the histogram *name*."""
        if self._meter is None:
            return
        try:
            vname, safe = self._guard(name, attrs, app_id)
            inst = self._histograms.get(vname)
            if inst is None:
                with self._lock:
                    inst = self._histograms.get(vname)
                    if inst is None:
                        inst = self._meter.create_histogram(
                            vname, unit=unit, description=description
                        )
                        self._histograms[vname] = inst
            inst.record(value, attributes=safe)
        except Exception as exc:
            logger.warning("metrics histogram %r failed: %s", name, exc)
