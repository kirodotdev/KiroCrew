"""Telemetry provider wiring — builds the process-global ``MetricsRecorder``.

Consent + local-first:
  * ``telemetry.enabled`` defaults **False**. When off, ``get_recorder()`` returns
    a no-op recorder, so adding metric call sites is a zero-runtime-effect change
    until a host opts in (mirrors the ``mcp_gateway.enabled`` /
    ``skills.lazy_load`` default-off convention).
  * When on, a ``PeriodicExportingMetricReader`` drains aggregated metrics to the
    local JSONL exporter under ``~/.kirocrew/metrics``. Nothing egresses the host.
  * Remote / OTLP egress is a separate opt-in exporter (deferred; not wired here).

OSS-CLEAN: depends only on ``opentelemetry`` (Apache-2.0 / CNCF) + the stdlib +
the first-party config loader and metrics helpers. No Amazon-internal imports.

This module is imported lazily (on the first ``get_recorder()`` call), never
during ``config.loader``'s import chain, so its top-level ``config.loader``
import cannot form a cycle. Callers that reach it from inside that chain (e.g.
``acp.client``) MUST import ``get_recorder`` lazily — see the ``# circular
import`` note there.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.metrics.local_exporter import JsonlMetricExporter
from kiro_crew.metrics.recorder import MetricsRecorder

logger = logging.getLogger(__name__)

_SERVICE_NAME = "kirocrew"
_SCOPE = "kiro_crew"

# Explicit histogram bucket boundaries (milliseconds), applied to EVERY kirocrew
# duration histogram via a MeterProvider View. OTEL's default boundaries top out
# at 10s, so a cold session startup (15-25s) or a cold MCP lazy-load would fall
# entirely into the +Inf overflow bucket and make bucket-derived p50/p90
# meaningless. These boundaries span sub-ms acquire latencies through ~60s cold
# starts so startup / backend.acquire / mcp.lazy_load / skill.lazy_load all get
# usable percentiles from the exported bucket counts.
_LATENCY_BUCKETS_MS = [
    1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 3000,
    5000, 7500, 10000, 15000, 20000, 30000, 45000, 60000,
]

_lock = threading.Lock()
_recorder: Optional[MetricsRecorder] = None
_initialized = False
_provider: Optional[MeterProvider] = None


def _default_metrics_dir() -> Path:
    return config_dir() / "metrics"


def _build_recorder() -> MetricsRecorder:
    """Read config once and build a live or no-op recorder accordingly."""
    global _provider
    try:
        cfg = KiroCrewConfig.load().telemetry
    except Exception as exc:
        logger.warning("telemetry config load failed; metrics disabled: %s", exc)
        return MetricsRecorder(None)

    if not cfg.enabled:
        return MetricsRecorder(None)

    try:
        directory = (
            Path(cfg.local_dir).expanduser()
            if cfg.local_dir
            else _default_metrics_dir()
        )
        reader = PeriodicExportingMetricReader(
            JsonlMetricExporter(directory),
            export_interval_millis=float(cfg.export_interval_seconds) * 1000.0,
        )
        _provider = MeterProvider(
            metric_readers=[reader],
            resource=Resource.create({"service.name": _SERVICE_NAME}),
            # Apply the latency bucket set to every histogram so bucket-derived
            # p50/p90 stay meaningful across the full startup / acquire /
            # lazy-load range (OTEL's default histogram tops out at 10s).
            views=[
                View(
                    instrument_type=Histogram,
                    aggregation=ExplicitBucketHistogramAggregation(
                        _LATENCY_BUCKETS_MS
                    ),
                ),
            ],
        )
        logger.info("telemetry enabled; local JSONL sink at %s", directory)
        return MetricsRecorder(_provider.get_meter(_SCOPE))
    except Exception as exc:
        logger.warning("telemetry init failed; metrics disabled: %s", exc)
        return MetricsRecorder(None)


def get_recorder() -> MetricsRecorder:
    """Return the process-global recorder, building it once on first use."""
    global _recorder, _initialized
    if _initialized and _recorder is not None:
        return _recorder
    with _lock:
        if not _initialized:
            _recorder = _build_recorder()
            _initialized = True
    assert _recorder is not None  # set above under the lock
    return _recorder


def shutdown() -> None:
    """Flush pending metrics and stop the reader thread for graceful teardown."""
    global _recorder, _initialized, _provider
    with _lock:
        if _provider is not None:
            try:
                _provider.shutdown()
            except Exception as exc:  # teardown must never raise
                logger.warning("telemetry provider shutdown failed: %s", exc)
            _provider = None
        _recorder = None
        _initialized = False


def reset_for_testing() -> None:
    """Drop the cached recorder + provider so the next get_recorder() rebuilds."""
    shutdown()
