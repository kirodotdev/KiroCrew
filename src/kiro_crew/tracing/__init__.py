"""Native OpenTelemetry traces, off by default.

``provider`` owns the tracer's lifecycle and the ``telemetry.traces_enabled``
switch; ``turns`` builds each ACP turn's span tree; ``ids`` is the deterministic
id formula; ``correlation`` is the attribute allowlist and the correlation ids;
``exporter`` is the local JSONL sink. Importing this package imports none of
the OpenTelemetry SDK: that happens only once the switch is on. Spec:
docs/system-specs/modules/metrics.md, "Traces".
"""
