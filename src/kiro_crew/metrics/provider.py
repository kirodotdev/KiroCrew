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
  * Opting in does NOT require a restart. The recorder is process-global and
    memoized, but ``get_recorder()`` re-resolves consent on a rate-limited window
    (``_CONSENT_RECHECK_SECS``) and rebuilds when it moved — so a config edit from
    the CLI, an editor, or another process starts (or stops) collection on its own.
    A caller that changed the setting itself can call ``shutdown()`` to apply it on
    the very next metric instead of waiting out the window.
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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

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

# Watchdog idle-at-decision. The watchdog consults the oracle from
# check_after_secs (60s) and the default hard cap is 1h (3600s,
# tool_stall_hard_cap_secs), so the range is 1s .. 4h — headroom above the cap
# because per-agent overrides can raise it; densest around the window
# boundaries (300s stale / 900s model-silent / 3600s cap) that the
# distribution is meant to tune. Sub-minute bounds exist because tests and
# per-agent overrides can legitimately act earlier than the default 60s gate.
_WATCHDOG_IDLE_BUCKETS_MS: list[float] = [
    1000, 5000, 15000, 30000, 60000, 120000, 180000, 300000, 450000,
    600000, 900000, 1800000, 3600000, 5400000, 7200000, 10800000, 14400000,
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
# instrument to this map when you add the metric. All values are ms — the
# dashboard's generic aggregation reports every histogram under *_ms keys, so
# a non-ms instrument would surface 1000x off there.
_HISTOGRAM_BUCKETS_MS: dict[str, list[float]] = {
    "kirocrew.gateway.request.duration": _FAST_BUCKETS_MS,
    "kirocrew.db.query.duration": _FAST_BUCKETS_MS,
    "kirocrew.mcp.backend.acquire.duration": _FAST_BUCKETS_MS,
    "kirocrew.skill.lazy_load.duration": _FAST_BUCKETS_MS,
    # Per-section first-turn context assembly. The spread within ONE build is
    # the widest of any instrument here: trivial string appends land under a
    # millisecond while a section that performs a query embedding reaches
    # several seconds. _FAST_BUCKETS_MS spans 0.5ms..60s, so the sub-ms sections
    # keep resolution and a slow embed stays below the top bound instead of
    # collapsing into +Inf.
    "kirocrew.context.section.duration": _FAST_BUCKETS_MS,
    # Telegram Bot API round-trips: typically 50-500ms, but a 429 retry_after
    # wait or a transport timeout reaches seconds -- _FAST_BUCKETS_MS spans
    # 0.5ms..60s, which covers both without flooring the tail percentiles.
    "kirocrew.telegram.api.duration": _FAST_BUCKETS_MS,
    "kirocrew.session.startup.duration": _STARTUP_BUCKETS_MS,
    # User message → first visible token. Warm turns land at 1-5s (model
    # latency), a cold first message adds the 5-25s spawn/handshake — the same
    # shape and range as startup, whose family is densest exactly there.
    "kirocrew.chat.first_token.duration": _STARTUP_BUCKETS_MS,
    "kirocrew.mcp.lazy_load.duration": _STARTUP_BUCKETS_MS,
    "kirocrew.gateway.boot.duration": _STARTUP_BUCKETS_MS,
    "kirocrew.turn.duration": _TURN_BUCKETS_MS,
    "kirocrew.watchdog.idle.duration": _WATCHDOG_IDLE_BUCKETS_MS,
}

_lock = threading.Lock()
_recorder: Optional[MetricsRecorder] = None
_initialized = False
_provider: Optional["_MeterProviderT"] = None

# Consent is not fixed for the life of the process. `kirocrew config set
# telemetry.enabled true` writes config.json from a SEPARATE process, so a
# recorder built at first use stays a no-op afterwards and the documented CLI
# path silently records nothing until the next restart — while the dashboard,
# which reads config live, reports collection as on.
#
# Re-resolving consent on every metric call would put a config stat behind every
# counter and histogram, so the recheck is rate-limited: at most one re-resolve
# per _CONSENT_RECHECK_SECS. That bounds how long an out-of-band change goes
# unnoticed without charging the hot path, which sees only a monotonic compare.
# A caller that must not wait (the dashboard's own PATCH route) calls shutdown()
# instead and gets the rebuild on the very next metric.
_CONSENT_RECHECK_SECS = 30.0
# Consent the live recorder was built with, and when it was last verified.
# ``None`` means "no live recorder", so the next call builds rather than compares.
_built_consent: Optional[bool] = None
_consent_checked_at = 0.0
# Bumped whenever the live recorder is dropped. An off-thread rebuild captures it
# and refuses to install if it moved, so a disable that lands mid-build cannot be
# undone by the build finishing afterwards.
_build_generation = 0
# True once this process has built a recorder. A later build is therefore a
# REbuild, which must go off-thread even when `shutdown()` cleared the state —
# otherwise the config route's own write would put the SDK import back on the loop.
_ever_built = False
# True while a consent-check worker is running, so a busy window does not spawn one
# thread per request. Cleared in the worker's finally, so a crash costs one window
# rather than stranding the check.
_check_in_flight = False

# Env-var opt-in. ``KIROCREW_TELEMETRY`` lets a host turn
# LOCAL metrics on (or force them off) without editing ~/.kiro/crew/config.json —
# handy for CI, containers, and one-off debugging. Truthy => enable, falsy =>
# disable, unset/blank => defer to the ``telemetry.enabled`` config flag (itself
# default False). This gates LOCAL collection ONLY: external OTLP egress still
# requires ``telemetry.otlp_endpoint`` to be set, so merely flipping this var
# never causes data to leave the host (egress stays off by default).
# Public: a UI control over ``telemetry.enabled`` names this variable when it
# reports the setting as pinned, and naming it from here keeps the message and the
# lookup from drifting apart.
TELEMETRY_ENV_VAR = "KIROCREW_TELEMETRY"
_ENV_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ENV_FALSY = frozenset({"0", "false", "no", "off"})


def env_pin() -> Optional[bool]:
    """Return the ``KIROCREW_TELEMETRY`` pin, or ``None`` when the var is unset.

    Public because a UI control over ``telemetry.enabled`` has to know the config
    flag is not the last word: a pinned host would otherwise offer a switch whose
    write can never change what is collected. Resolving the pin here (rather than
    re-reading the env var at the call site) keeps the control and the collector
    from disagreeing about what "on" means.
    """
    raw = os.environ.get(TELEMETRY_ENV_VAR, "").strip().lower()
    if raw in _ENV_TRUTHY:
        return True
    if raw in _ENV_FALSY:
        return False
    return None


def _consent_enabled(cfg: object) -> bool:
    """Resolve the telemetry consent gate: env var overrides the config flag."""
    pin = env_pin()
    if pin is not None:
        return pin
    return bool(getattr(cfg, "enabled", False))


def _default_metrics_dir() -> Path:
    return config_dir() / "metrics"


class _Build(NamedTuple):
    """What a build produced, for a caller to install under ``_lock``.

    Returned rather than assigned so the expensive part can run OFF the lock and
    off the event loop: only the install is a critical section.
    """

    recorder: MetricsRecorder
    provider: Optional["_MeterProviderT"]
    consent: Optional[bool]


def _build_recorder() -> _Build:
    """Read config once and build a live or no-op recorder accordingly.

    Writes no module state. It resolves consent, and the caller records that
    alongside the recorder so a later recheck can tell whether the setting moved
    underneath a live recorder without re-reading config on every metric call.
    """
    # Consent is resolved BEFORE the availability checks so the recorded consent
    # always reflects the setting, even on a host that can never record. Leaving
    # it unset there would make every recheck window see a difference and rebuild
    # a no-op recorder in a loop.
    try:
        cfg = KiroCrewConfig.load().telemetry
    except Exception as exc:
        logger.warning("telemetry config load failed; metrics disabled: %s", exc)
        # Unknown rather than False: the next successful read re-resolves once.
        return _Build(MetricsRecorder(None), None, None)

    consent = _consent_enabled(cfg)
    if not consent:
        return _Build(MetricsRecorder(None), None, consent)

    if not _OTEL_AVAILABLE:
        # opentelemetry missing from the env closure. Degrade to the
        # no-op recorder instead of ever reaching this point via a crash.
        logger.warning("opentelemetry not installed; telemetry disabled")
        return _Build(MetricsRecorder(None), None, consent)

    # Consent granted — now pay for the SDK import (deferred from module scope
    # so the default-off path never does).
    if not _load_otel():
        logger.warning("opentelemetry not importable; telemetry disabled")
        return _Build(MetricsRecorder(None), None, consent)

    # PeriodicExportingMetricReader starts its daemon ticker thread inside
    # __init__, so if any later step (MeterProvider construction, etc.) raises,
    # the reader is already ticking. This list is bound BEFORE the try — binding
    # it inside would leave it unbound when the first reader's constructor
    # raises, and the except would then fail on the reap instead of degrading —
    # and each reader joins it once constructed, so the except reaps every one.
    # Otherwise an orphaned thread keeps running and spamming export WARNINGs for
    # the life of the process even though metrics are "disabled".
    started_readers: list = []
    try:
        directory = (
            Path(cfg.local_dir).expanduser()
            if cfg.local_dir
            else _default_metrics_dir()
        )
        started_readers.append(
            PeriodicExportingMetricReader(
                JsonlMetricExporter(
                    directory,
                    retention_days=cfg.retention_days,
                    max_total_mb=cfg.max_total_mb,
                ),
                export_interval_millis=float(cfg.export_interval_seconds) * 1000.0,
            )
        )
        # Opt-in OTLP egress: only when telemetry.otlp_endpoint is set.
        # Empty endpoint => local-only, no network egress (the default).
        otlp_reader = _build_otlp_reader(cfg)
        otlp_active = otlp_reader is not None
        if otlp_reader is not None:
            started_readers.append(otlp_reader)
        provider = MeterProvider(
            metric_readers=started_readers,
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
        logger.info(
            "telemetry enabled; local JSONL sink at %s (otlp=%s)",
            directory,
            "on" if otlp_active else "off",
        )
        if otlp_active:
            # Name the egress start on its own line: a file/CLI enable is trusted
            # and ungated, so this log is the only record that metrics began
            # leaving the machine.
            logger.info("telemetry OTLP export active; metrics leave this machine")
        return _Build(MetricsRecorder(provider.get_meter(_SCOPE)), provider, consent)
    except Exception as exc:
        logger.warning("telemetry init failed; metrics disabled: %s", exc)
        # Reap EVERY reader already started, not just the first: with
        # telemetry.otlp_endpoint set there are two, and leaving the second
        # ticking is the same orphan this branch exists to prevent.
        #
        # Off this thread, because a reader shutdown performs a final export and
        # joins its ticker with the SDK's 30s default.
        _reap_readers_detached(started_readers)
        return _Build(MetricsRecorder(None), None, consent)


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


def _install_locked(built: _Build) -> None:
    """Publish a finished build. Call under ``_lock``."""
    global _recorder, _provider, _initialized, _built_consent, _consent_checked_at
    global _ever_built
    _recorder = built.recorder
    _provider = built.provider
    _built_consent = built.consent
    _initialized = True
    _consent_checked_at = time.monotonic()
    _ever_built = True


def _consent_worker(generation: int) -> None:
    """Re-resolve consent off the event loop and rebuild if it moved.

    Everything expensive lives here. Reading config is a fingerprint-cache hit in
    the steady state (~0.3ms) but a full read plus schema validation when the file
    actually changed (~14ms) — and "the file changed" is precisely when this runs,
    so the read cannot happen on the caller's thread. ``get_recorder()`` is called
    on the event loop by the route-latency middleware for every request.
    """
    global _consent_checked_at, _check_in_flight, _recorder, _initialized
    global _built_consent
    try:
        try:
            consent: Optional[bool] = _consent_enabled(KiroCrewConfig.load().telemetry)
        except Exception as exc:
            # Losing the ability to READ the setting is not withdrawal. Keep the
            # live recorder and try again after the next window.
            logger.debug("telemetry consent recheck failed; keeping recorder: %s", exc)
            consent = None

        doomed = None
        rebuild_generation = None
        with _lock:
            if generation != _build_generation:
                # Superseded: another flip or a shutdown happened while we read.
                # Do NOT stamp the clock — this check answered a question about a
                # state that no longer exists, and stamping it would defer the
                # replacement check by a full window while the setting sat
                # unapplied.
                return
            # Stamp even when the read failed, so an unreadable config cannot turn
            # every metric call into a fresh read.
            _consent_checked_at = time.monotonic()
            if consent is None or consent == _built_consent:
                return
            logger.info("telemetry consent changed on disk; rebuilding recorder")
            doomed = _take_provider_locked()
            # Serve a no-op from here on: withdrawal is complete at this point, and
            # an opt-in has nothing to serve until the build lands.
            _recorder = MetricsRecorder(None)
            _initialized = True
            _built_consent = consent
            rebuild_generation = _build_generation

        # Outside the lock: a provider shutdown joins its reader threads.
        if doomed is not None:
            _flush_detached_provider(doomed)
        if not consent:
            return  # withdrawal builds nothing

        built = _build_recorder()
        with _lock:
            superseded = rebuild_generation != _build_generation
            if not superseded:
                _install_locked(built)
        if superseded:
            # Another flip overtook this build; drop it, and flush AFTER releasing
            # the lock so no get_recorder() waits on a reader join.
            stale = built.provider
            if stale is not None:
                _flush_detached_provider(stale)
    finally:
        with _lock:
            _check_in_flight = False


def _schedule_consent_check_locked() -> None:
    """Hand the recheck to a worker. Call under ``_lock``.

    Stamps nothing itself: the worker owns the clock, so a failed or superseded
    check cannot leave the window permanently expired. The in-flight flag is
    cleared in the worker's ``finally``, so a crash delays the next check by one
    window rather than stranding it.
    """
    global _check_in_flight
    if _check_in_flight:
        return
    _check_in_flight = True
    threading.Thread(
        target=_consent_worker,
        args=(_build_generation,),
        name="kirocrew-telemetry-consent",
        daemon=True,
    ).start()


def get_recorder() -> MetricsRecorder:
    """Return the process-global recorder, rebuilding it when consent changes.

    The fast path is a monotonic comparison: once a recorder exists it is returned
    directly until the recheck window elapses. See ``_CONSENT_RECHECK_SECS`` for
    why the window exists at all.

    **This function never reads config and never builds anything** — apart from the
    very first build of the process, which has nothing to serve in the meantime.
    A due recheck only spawns a worker, because both the config read (~14ms when
    the file changed) and the rebuild (~57ms of SDK import) would otherwise land on
    the event loop, which the route-latency middleware drives on every request.

    The recheck is eventual by design — up to ``_CONSENT_RECHECK_SECS`` — so the
    extra thread hop changes nothing an observer can distinguish. A caller that
    changed the setting itself calls ``shutdown()`` to skip the wait.
    """
    global _recorder, _initialized, _built_consent
    # Snapshot into a local: the guard below spans a Python call, so re-reading the
    # global to return it would race a concurrent shutdown() — which the config
    # route runs on an asyncio.to_thread worker — and hand back None from a
    # non-Optional signature.
    rec = _recorder
    if _initialized and rec is not None and not _consent_recheck_due():
        return rec
    with _lock:
        if _initialized and _recorder is not None:
            if _consent_recheck_due():
                _schedule_consent_check_locked()
        elif _ever_built:
            # A shutdown dropped the recorder (the config route applying its own
            # write, or teardown). Serve a no-op and let the worker re-resolve.
            _recorder = MetricsRecorder(None)
            _initialized = True
            _built_consent = None
            _schedule_consent_check_locked()
        else:
            # First build of the process: synchronous, because there is no
            # recorder to serve in the meantime and this is not a steady-state
            # request path.
            _install_locked(_build_recorder())
    assert _recorder is not None  # set above under the lock
    return _recorder


def _consent_recheck_due() -> bool:
    """Whether the recheck window has elapsed. Cheap enough for the hot path."""
    return (time.monotonic() - _consent_checked_at) >= _CONSENT_RECHECK_SECS


def _reap_readers_detached(readers: list) -> None:
    """Shut down partially-constructed metric readers off the calling thread.

    A reader shutdown flushes and joins its ticker thread, so it must not run on
    the event loop — and the degrade path must never raise, since its whole job is
    to hand back a no-op recorder.
    """
    if not readers:
        return

    def _reap() -> None:
        for r in readers:
            try:
                r.shutdown()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("metric reader shutdown after init failure failed", exc_info=True)

    threading.Thread(
        target=_reap, name="kirocrew-telemetry-reap", daemon=True
    ).start()


def _take_provider_locked() -> Optional["_MeterProviderT"]:
    """Clear the live recorder and RETURN its provider for the caller to flush.

    Call under ``_lock``. Deliberately does not flush: a provider shutdown joins
    each reader's export thread (30s deadline) and, with ``telemetry.otlp_endpoint``
    set, ends in a synchronous network POST — and doing that while holding ``_lock``
    would block every other caller on lock ACQUISITION, including ``get_recorder()``
    on the event loop. Clearing the state is cheap; whoever takes the provider
    flushes it after the lock is released.
    """
    global _recorder, _initialized, _provider, _built_consent, _build_generation
    doomed = _provider
    _provider = None
    _recorder = None
    _initialized = False
    _built_consent = None
    # Any rebuild started before this point is now stale.
    _build_generation += 1
    return doomed


def _flush_detached_provider(doomed: "_MeterProviderT") -> None:
    """Flush a provider that is no longer referenced. Never holds ``_lock``.

    Best-effort by construction. The SDK registers its own ``atexit`` flush when a
    provider is built (``MeterProvider(shutdown_on_exit=True)``, its default), which
    covers an interpreter exit before this runs; once it has run, that hook is
    already satisfied and adds nothing, so a flush cut short by exit is simply lost.
    That is the accepted cost of not blocking the loop.

    One consequence to know about: for as long as this flush is inside its reader
    join, a re-enable can put a second exporter on the same per-PID shard, which the
    local exporter's single-writer assumption does not cover. Both sides swallow
    their IO errors, so the worst case is one dropped export cycle rather than a
    corrupt shard.
    """
    try:
        doomed.shutdown()
    except Exception as exc:  # a best-effort flush must never raise
        logger.warning("telemetry provider shutdown failed: %s", exc)


def shutdown() -> None:
    """Flush pending metrics and stop the reader thread for graceful teardown.

    Also the "apply now" seam for a caller that just changed the setting itself:
    dropping the recorder makes the next metric call rebuild from the new value
    without waiting out the recheck window.

    The flush runs on the CALLER's thread (so process teardown and the config
    route — which calls this via ``asyncio.to_thread`` — both get a completed
    flush) but NOT under ``_lock``: holding it across the flush would stall any
    concurrent ``get_recorder()`` on the event loop for the whole 30s deadline.
    """
    global _consent_checked_at
    with _lock:
        doomed = _take_provider_locked()
        # Drop the stamp rather than carry a reading that describes a recorder that
        # no longer exists; whichever rebuild comes next stamps its own. This does
        # not defer the next recheck — a zero stamp reads as immediately due — but
        # nothing consults it while `_recorder` is None.
        _consent_checked_at = 0.0
    if doomed is not None:
        _flush_detached_provider(doomed)


# reset_for_testing() waits at most this long for an in-flight consent-check
# worker to finish before returning. The worker is a daemon thread whose
# ``finally`` unconditionally clears ``_check_in_flight`` (see
# ``_consent_worker``), so under normal load this bound is never approached;
# it exists so a genuinely stuck worker fails the test loudly instead of
# reset_for_testing() handing back a "clean" state while a stale thread can
# still mutate module globals underneath the next test.
_RESET_WAIT_BOUND_SECS = 10.0
_RESET_WAIT_POLL_SECS = 0.01


def _wait_for_in_flight_consent_worker() -> None:
    """Block until ``_check_in_flight`` is False, or raise past the bound.

    Test-only: called from ``reset_for_testing`` so every test starts from a
    state with no consent-check worker still running. Polling a plain bool
    under ``_lock`` matches how ``_check_in_flight`` is read and written
    everywhere else in this module, and keeps this seam simple since it only
    ever runs between tests, never on a request path.
    """
    deadline = time.monotonic() + _RESET_WAIT_BOUND_SECS
    while True:
        with _lock:
            if not _check_in_flight:
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "reset_for_testing: a consent-check worker is still in flight "
                f"after {_RESET_WAIT_BOUND_SECS}s; a test must never proceed "
                "with a live stale worker able to mutate module state"
            )
        time.sleep(_RESET_WAIT_POLL_SECS)


def reset_for_testing() -> None:
    """Drop the cached recorder + provider so the next get_recorder() rebuilds.

    Also clears ``_ever_built``, so the next build is synchronous and a test can
    assert on the result without polling. Production keeps that flag set, which is
    what pushes a post-shutdown rebuild off the calling thread.

    Waits (bounded) for any in-flight consent-check worker to finish before
    returning. ``shutdown()`` alone does not stop that worker — it only bumps
    the generation the worker checks before installing its result — so a
    worker started by an earlier test can still be mid-run here. Owning that
    wait in this one seam, rather than in each test's own helpers, is what
    guarantees every test starts from a state with no worker able to mutate
    module globals underneath it.
    """
    global _ever_built
    shutdown()
    _wait_for_in_flight_consent_worker()
    with _lock:
        _ever_built = False
