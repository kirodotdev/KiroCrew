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

import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.metrics.recorder import MetricsRecorder

if TYPE_CHECKING:  # real types for annotations; never imported at runtime
    from opentelemetry.sdk.metrics import MeterProvider as _MeterProviderT
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader as _ReaderT

# KiroCrew declares opentelemetry-sdk as a required dependency, so
# the availability probe below is defense-in-depth — not for a genuinely
# optional dep, but for a partial / --no-deps / broken env-closure install where
# the SDK is absent. This module is on the eager boot chain (cli.py -> dashboard
# -> ... -> history.py -> skills.py -> get_recorder), so an unconditional
# top-level import here would brick the ENTIRE gateway (and `kirocrew
# --version`) even though telemetry defaults off. Degrade to the existing no-op
# MetricsRecorder(None) path instead of crashing at import time.
#
# The SDK is NOT imported at module scope: executing
# ``opentelemetry.sdk.metrics`` + ``.export`` + ``.view`` + ``.resources`` costs
# ~57ms and ~120 extra modules on EVERY entry point (CLI invocations, MCP stdio
# subprocesses, `kirocrew --version`) while telemetry is off by default and the
# SDK is never used. ``importlib.util.find_spec`` answers the availability
# question for ~0.9ms without executing the package, so the eager cost is paid
# only by hosts that actually opt in. The real imports happen in ``_load_otel``,
# called from ``_build_recorder`` after the consent gate.
#
# local_exporter is loaded in the same lazy step: its JsonlMetricExporter
# subclasses the OTel SDK's MetricExporter base class, so the module itself
# cannot load without opentelemetry. It is only used on the enabled path, so
# deferring it preserves the degrade contract. recorder.py is annotation-only on
# OTel symbols (TYPE_CHECKING import) and stays loadable either way.


def _otel_importable() -> bool:
    """Whether the OTel metrics SDK can be imported, without importing it.

    ``find_spec`` on a dotted name imports the PARENT packages
    (``opentelemetry``, ``opentelemetry.sdk``) but not the metrics SDK itself,
    which is where the cost and the module-count blow-up live. It raises
    (rather than returning ``None``) when a parent is unimportable, so both
    outcomes are folded into ``False`` here.
    """
    try:
        return importlib.util.find_spec("opentelemetry.sdk.metrics") is not None
    except (ImportError, AttributeError, ValueError):
        return False


_OTEL_AVAILABLE = _otel_importable()

# Resolved on first use by ``_load_otel``. Kept as module globals (rather than
# locals in ``_build_recorder``) because tests substitute them by name —
# ``_load_otel`` only fills the ones that are still None, so a monkeypatched
# stand-in is never overwritten by the real class. Typed ``Any`` because they
# are lazily bound; the TYPE_CHECKING aliases above carry the real types for
# the annotations that need them.
MeterProvider: Any = None
PeriodicExportingMetricReader: Any = None
ExplicitBucketHistogramAggregation: Any = None
View: Any = None
Resource: Any = None
JsonlMetricExporter: Any = None


def _load_otel() -> bool:
    """Import the OTel metrics SDK into the module globals. True on success."""
    global MeterProvider, PeriodicExportingMetricReader
    global ExplicitBucketHistogramAggregation, View, Resource, JsonlMetricExporter
    try:
        from opentelemetry.sdk.metrics import MeterProvider as _MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader as _Reader
        from opentelemetry.sdk.metrics.view import (
            ExplicitBucketHistogramAggregation as _BucketAggregation,
        )
        from opentelemetry.sdk.metrics.view import View as _View
        from opentelemetry.sdk.resources import Resource as _Resource

        from kiro_crew.metrics.local_exporter import JsonlMetricExporter as _JsonlMetricExporter
    except ImportError:
        return False
    # Fill only what is still unset, so a test's substitute survives.
    if MeterProvider is None:
        MeterProvider = _MeterProvider
    if PeriodicExportingMetricReader is None:
        PeriodicExportingMetricReader = _Reader
    if ExplicitBucketHistogramAggregation is None:
        ExplicitBucketHistogramAggregation = _BucketAggregation
    if View is None:
        View = _View
    if Resource is None:
        Resource = _Resource
    if JsonlMetricExporter is None:
        JsonlMetricExporter = _JsonlMetricExporter
    return True


logger = logging.getLogger(__name__)

_SERVICE_NAME = "kirocrew"
_SCOPE = "kiro_crew"

# Explicit histogram bucket boundaries (milliseconds), applied PER INSTRUMENT via
# MeterProvider Views. OTEL's default boundaries top out at 10s, so anything
# slower lands entirely in the +Inf overflow bucket — and because
# `_pct_from_buckets` can only report an overflow bucket's LOWER bound, the
# derived p50/p90 then silently pin to the top boundary instead of reporting the
# real value. A single shared array cannot serve every instrument: sub-ms MCP
# acquires and multi-minute agent turns differ by six orders of magnitude, and
# sizing one array for both costs either resolution at the fast end or truth at
# the slow end.
#
# Three families, each sized to its instrument's MEASURED range:

# Sub-ms through a minute — pooled acquires, skill loads, HTTP requests.
# These are dominated by ~1ms values, so the fine end matters. The 60s ceiling
# exists because request duration excludes WebSocket upgrades and SSE streams,
# but ordinary slow endpoints (installers, long provisioning calls) do run past
# 30s, and any sample above the top bound has its percentile floored at that
# bound.
_FAST_BUCKETS_MS: list[float] = [
    0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500,
    1000, 2500, 5000, 10000, 30000, 60000,
]

# Milliseconds through ~1 minute — session startup and other cold-start work.
# Sized for startup, which spans a 0.5ms set_model phase through 15-25s cold
# spawns.
_STARTUP_BUCKETS_MS: list[float] = [
    1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 3000,
    5000, 7500, 10000, 15000, 20000, 30000, 45000, 60000,
]

# One second through one hour — agent turns. A turn is an entire agent loop
# (model calls plus every tool round-trip, and any wait on an interactive
# approval prompt), so minutes are ordinary and buckets extend to an hour so a
# multi-minute turn is not floored into an overflow bucket. Resolution is
# deliberately densest between 1 and 10 minutes, where turns actually land.
_TURN_BUCKETS_MS: list[float] = [
    1000, 2500, 5000, 10000, 20000, 30000, 45000, 60000, 90000,
    120000, 180000, 300000, 450000, 600000, 900000, 1200000,
    1800000, 2700000, 3600000,
]

# Instrument name -> boundaries. This map is the COMPLETE set of kirocrew
# duration histograms: the Views below are built from it and there is no
# catch-all, because the OTEL SDK applies EVERY matching View rather than the
# first, so a per-instrument View plus a catch-all would emit two conflicting
# streams under one metric name.
#
# Consequence: a new histogram missing from this map falls back to OTEL's
# default 10s-ceiling boundaries. `test/metrics/test_provider_bucket_views.py`
# fails when a histogram metric name in the source has no entry here — add the
# instrument to this map when you add the metric.
_HISTOGRAM_BUCKETS_MS: dict[str, list[float]] = {
    "kirocrew.gateway.request.duration": _FAST_BUCKETS_MS,
    "kirocrew.db.query.duration": _FAST_BUCKETS_MS,
    "kirocrew.mcp.backend.acquire.duration": _FAST_BUCKETS_MS,
    "kirocrew.skill.lazy_load.duration": _FAST_BUCKETS_MS,
    # Telegram Bot API round-trips: typically 50-500ms, but a 429 retry_after
    # wait or a transport timeout reaches seconds -- _FAST_BUCKETS_MS spans
    # 0.5ms..60s, which covers both without flooring the tail percentiles.
    "kirocrew.telegram.api.duration": _FAST_BUCKETS_MS,
    "kirocrew.session.startup.duration": _STARTUP_BUCKETS_MS,
    "kirocrew.mcp.lazy_load.duration": _STARTUP_BUCKETS_MS,
    "kirocrew.gateway.boot.duration": _STARTUP_BUCKETS_MS,
    "kirocrew.turn.duration": _TURN_BUCKETS_MS,
}

_lock = threading.Lock()
_recorder: Optional[MetricsRecorder] = None
_initialized = False
_provider: Optional["_MeterProviderT"] = None

# Env-var opt-in. ``KIROCREW_TELEMETRY`` lets a host turn
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

    # Consent granted — now pay for the SDK import (deferred from module scope
    # so the default-off path never does).
    if not _load_otel():
        logger.warning("opentelemetry not importable; telemetry disabled")
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
        # Opt-in OTLP egress: only when telemetry.otlp_endpoint is set.
        # Empty endpoint => local-only, no network egress (the default).
        otlp_reader = _build_otlp_reader(cfg)
        if otlp_reader is not None:
            readers.append(otlp_reader)
        provider = MeterProvider(
            metric_readers=readers,
            resource=Resource.create({"service.name": _SERVICE_NAME}),
            # One View per instrument, from _HISTOGRAM_BUCKETS_MS. Deliberately
            # NOT a catch-all `instrument_type=Histogram` View: the OTEL SDK
            # applies every matching View, so a catch-all alongside these would
            # publish each named instrument twice under one metric name with
            # different bounds, and the telemetry aggregator merges same-length
            # bucket arrays without comparing bounds — it would silently double
            # the counts. See the completeness guard test.
            views=[
                View(
                    instrument_name=name,
                    aggregation=ExplicitBucketHistogramAggregation(bounds),
                )
                for name, bounds in _HISTOGRAM_BUCKETS_MS.items()
            ],
        )
        _provider = provider
        logger.info(
            "telemetry enabled; local JSONL sink at %s (otlp=%s)",
            directory,
            "on" if len(readers) > 1 else "off",
        )
        return MetricsRecorder(provider.get_meter(_SCOPE))
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


def _build_otlp_reader(cfg: object) -> Optional["_ReaderT"]:
    """Build the opt-in OTLP/HTTP metric reader, or None when not configured.

    Egress is OFF by default: this returns None unless
    ``telemetry.otlp_endpoint`` is a non-empty string. The OTLP exporter lives
    in the separate ``kirocrew[otlp]`` package extra (install with
    ``pip install "kirocrew[otlp]"``), not the hard dependency set. If a host
    opts in without installing it, we log a warning and degrade to local-only
    rather than crashing telemetry init. The exporter sees only what the
    MetricsRecorder facade lets through: attributes are sanitised before they
    reach any reader, and call sites are required to pass low-cardinality
    constants rather than prompts, content, tokens, paths or user ids. That
    sanitisation is defence in depth over that requirement, not a substitute
    for it, so egress is only as safe as the call sites feeding it.
    """
    endpoint = str(getattr(cfg, "otlp_endpoint", "") or "").strip()
    if not endpoint:
        return None
    # Callable directly (not only via _build_recorder), so make sure the lazily
    # imported SDK symbols this needs are bound.
    if not _load_otel():
        logger.warning("opentelemetry not importable; OTLP egress disabled")
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
