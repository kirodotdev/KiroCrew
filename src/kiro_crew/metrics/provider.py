"""Telemetry provider wiring — builds the process-global ``MetricsRecorder``.

Consent + local-first:
  * ``telemetry.enabled`` defaults **False**. When off, ``get_recorder()`` returns
    a no-op recorder, so adding metric call sites is a zero-runtime-effect change
    until a host opts in (mirrors the ``mcp_gateway.enabled`` /
    ``skills.lazy_load`` default-off convention).
  * Easy opt-in: set ``telemetry.enabled: true`` in ``~/.kiro/crew/config.json``
    OR export the ``KIROCREW_TELEMETRY`` env var (``1``/``true``/``on`` to enable,
    ``0``/``false``/``off`` to force-disable). The env var overrides the config
    flag and gates LOCAL collection only — it never enables network egress.
  * When on, a ``PeriodicExportingMetricReader`` drains aggregated metrics to the
    local JSONL exporter under ``~/.kiro/crew/metrics``. Nothing egresses the host.
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
import os
import threading
from pathlib import Path
from typing import Optional

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.metrics.recorder import MetricsRecorder

# KiroCrew declares opentelemetry-sdk as a required dependency, so
# this guard is defense-in-depth — not for a genuinely optional dep, but for a
# partial / --no-deps / broken env-closure install where the SDK is absent. This
# module is on the eager boot chain (cli.py -> dashboard -> ... -> history.py ->
# skills.py -> get_recorder), so an unconditional top-level import here would
# brick the ENTIRE gateway (and `kirocrew --version`) even though telemetry
# defaults off. Degrade to the existing no-op MetricsRecorder(None) path instead
# of crashing at import time.
#
# local_exporter is imported INSIDE the guard: its JsonlMetricExporter
# subclasses the OTel SDK's MetricExporter base class, so the module itself
# cannot load without opentelemetry. It is only used on the enabled path
# (after the _OTEL_AVAILABLE check in _build_recorder), so guarding the
# import preserves the degrade contract. recorder.py is annotation-only on
# OTel symbols (TYPE_CHECKING import) and stays loadable either way.
try:
    from opentelemetry.sdk.metrics import Histogram, MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import (
        ExplicitBucketHistogramAggregation,
        View,
    )
    from opentelemetry.sdk.resources import Resource

    from kiro_crew.metrics.local_exporter import JsonlMetricExporter

    _OTEL_AVAILABLE = True
except ImportError:
    Histogram = MeterProvider = PeriodicExportingMetricReader = None  # type: ignore[assignment,misc]
    ExplicitBucketHistogramAggregation = View = Resource = None  # type: ignore[assignment,misc]
    JsonlMetricExporter = None  # type: ignore[assignment,misc]
    _OTEL_AVAILABLE = False

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
_provider: Optional["MeterProvider"] = None

# Env-var opt-in (rec #14: easy opt-in). ``KIROCREW_TELEMETRY`` lets a host turn
# LOCAL metrics on (or force them off) without editing ~/.kiro/crew/config.json —
# handy for CI, containers, and one-off debugging. Truthy => enable, falsy =>
# disable, unset/blank => defer to the ``telemetry.enabled`` config flag (itself
# default False). This gates LOCAL collection ONLY: external OTLP egress still
# requires ``telemetry.otlp_endpoint`` to be set, so merely flipping this var
# never causes data to leave the host (egress stays off by default).
_TELEMETRY_ENV = "KIROCREW_TELEMETRY"
_ENV_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ENV_FALSY = frozenset({"0", "false", "no", "off"})


def _consent_enabled(cfg: object) -> bool:
    """Resolve the telemetry consent gate: env var overrides the config flag."""
    raw = os.environ.get(_TELEMETRY_ENV, "").strip().lower()
    if raw in _ENV_TRUTHY:
        return True
    if raw in _ENV_FALSY:
        return False
    return bool(getattr(cfg, "enabled", False))


def _default_metrics_dir() -> Path:
    return config_dir() / "metrics"


def _build_recorder() -> MetricsRecorder:
    """Read config once and build a live or no-op recorder accordingly."""
    global _provider
    if not _OTEL_AVAILABLE:
        # opentelemetry missing from the env closure. Degrade to the
        # no-op recorder instead of ever reaching this point via a crash.
        logger.warning("opentelemetry not installed; telemetry disabled")
        return MetricsRecorder(None)

    try:
        cfg = KiroCrewConfig.load().telemetry
    except Exception as exc:
        logger.warning("telemetry config load failed; metrics disabled: %s", exc)
        return MetricsRecorder(None)

    if not _consent_enabled(cfg):
        return MetricsRecorder(None)

    # PeriodicExportingMetricReader starts its daemon ticker thread inside
    # __init__, so if any later step (MeterProvider construction, etc.) raises,
    # the reader is already ticking. Hoist it here so the except can reap it —
    # otherwise the orphaned thread keeps running and spamming export WARNINGs
    # for the life of the process even though metrics are "disabled".
    reader = None
    try:
        directory = (
            Path(cfg.local_dir).expanduser()
            if cfg.local_dir
            else _default_metrics_dir()
        )
        reader = PeriodicExportingMetricReader(
            JsonlMetricExporter(
                directory,
                retention_days=cfg.retention_days,
                max_total_mb=cfg.max_total_mb,
            ),
            export_interval_millis=float(cfg.export_interval_seconds) * 1000.0,
        )
        readers = [reader]
        # Opt-in OTLP egress (rec #1): only when telemetry.otlp_endpoint is set.
        # Empty endpoint => local-only, no network egress (the default).
        otlp_reader = _build_otlp_reader(cfg)
        if otlp_reader is not None:
            readers.append(otlp_reader)
        _provider = MeterProvider(
            metric_readers=readers,
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
        logger.info(
            "telemetry enabled; local JSONL sink at %s (otlp=%s)",
            directory,
            "on" if len(readers) > 1 else "off",
        )
        return MetricsRecorder(_provider.get_meter(_SCOPE))
    except Exception as exc:
        logger.warning("telemetry init failed; metrics disabled: %s", exc)
        # Reap the reader's already-started daemon thread if it outlived a
        # failure in a later init step, so a disabled recorder leaves nothing
        # ticking behind. shutdown() must never turn the degrade path into a
        # raise, so swallow anything it throws.
        if reader is not None:
            try:
                reader.shutdown()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("metric reader shutdown after init failure failed", exc_info=True)
        return MetricsRecorder(None)


def _build_otlp_reader(cfg: object) -> Optional["PeriodicExportingMetricReader"]:
    """Build the opt-in OTLP/HTTP metric reader, or None when not configured.

    Egress is OFF by default (rec #1): this returns None unless
    ``telemetry.otlp_endpoint`` is a non-empty string. The OTLP exporter lives
    in the separate ``kirocrew[otlp]`` package extra (install with
    ``pip install "kirocrew[otlp]"``), not the hard dependency set. If a host
    opts in without installing it, we log a warning and degrade to local-only
    rather than crashing telemetry init. The exporter only ever sees redacted, low-cardinality data points (the MetricsRecorder
    facade sanitises attributes before they reach any reader), so opting in
    cannot leak prompts, content, tokens, paths, user ids, or secrets.
    """
    endpoint = str(getattr(cfg, "otlp_endpoint", "") or "").strip()
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
    except ImportError:
        # Never log the configured endpoint: URLs may contain credentials in
        # userinfo or query parameters. The setting's presence is sufficient
        # for diagnosis without exposing its value.
        logger.warning(
            "telemetry.otlp_endpoint is set but opentelemetry-exporter-otlp-"
            "proto-http is not installed; OTLP egress disabled (local-only)"
        )
        return None
    try:
        exporter = OTLPMetricExporter(endpoint=endpoint)
        return PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=float(
                getattr(cfg, "export_interval_seconds", 60)
            )
            * 1000.0,
        )
    except Exception:
        # Constructor errors may echo the credential-bearing endpoint in their
        # message. Keep this warning fixed-text just like the missing-extra path.
        logger.warning("OTLP exporter init failed; OTLP egress disabled (local-only)")
        return None


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
