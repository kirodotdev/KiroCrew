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
import platform
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from kiro_crew import __version__, beacon
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.metrics.recorder import MetricsRecorder
from kiro_crew.platform.context import (
    PlatformCompositionError,
    current_context,
    safe_context_call,
)

if TYPE_CHECKING:  # real types for annotations; never imported at runtime
    from collections.abc import Iterable

    from opentelemetry.sdk.metrics import MeterProvider as _MeterProviderT
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader as _ReaderT

    from kiro_crew.config.loader import TelemetryConfig
    from kiro_crew.platform.interfaces import OtlpDestination

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

# ``platform.machine()`` spellings -> OTel semantic-convention ``host.arch``
# values. Every environment attribute below is a CLOSED set: a spelling this
# map does not know folds to :data:`_ATTR_OTHER` rather than passing through,
# so an exotic platform cannot mint a label of its own.
_ARCH_BY_MACHINE = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "x86",
    "i686": "x86",
}
_KNOWN_OS_TYPES = frozenset({"linux", "darwin", "windows"})
#: ``platform.python_implementation()`` spellings, lowercased -- the four the
#: stdlib documents it can return. An interpreter outside this set folds to
#: :data:`_ATTR_OTHER` like every other environment reading, so a patched or
#: exotic runtime cannot mint a label of its own.
_KNOWN_RUNTIME_NAMES = frozenset({"cpython", "pypy", "jython", "ironpython"})
#: Fold target for any environment reading outside its known set. One shared
#: bucket, so "unknown" stays a single bounded label per attribute.
_ATTR_OTHER = "other"

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
# Each family below is sized to its instrument's MEASURED range:

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

# Sub-ms through one hour — a single tool round-trip. The widest span of any
# family here, and both ends are real: a cached file read returns in well under
# a millisecond while a build or test run invoked through the execute tool runs
# for minutes. The fine end is copied from _FAST_BUCKETS_MS because reads
# dominate the population by count, and the ceiling matches _TURN_BUCKETS_MS
# because a tool call cannot outlive the turn that contains it.
_TOOL_CALL_BUCKETS_MS: list[float] = [
    0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500,
    1000, 2500, 5000, 10000, 30000, 60000,
    120000, 300000, 600000, 1800000, 3600000,
]

# One second through one week — session lifetime. Sessions are the only
# instrument here measured in hours and days: a speculative session removed
# before its first turn lives seconds, while a dashboard tab left open across a
# working week is ordinary. Resolution is densest from a minute to a few hours,
# where interactive sessions land, and the 7-day ceiling exists so a long-lived
# tab is not floored into an overflow bucket. Sub-minute bounds are kept because
# the short-lived teardown paths (unclaimed, destroyed) are a real population
# whose distribution would otherwise collapse onto one boundary.
_SESSION_BUCKETS_MS: list[float] = [
    1000, 5000, 15000, 30000, 60000, 300000, 900000, 1800000,
    3600000, 7200000, 14400000, 28800000, 43200000,
    86400000, 172800000, 259200000, 604800000,
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
# a non-ms instrument would surface 1000x off there. A histogram that is NOT a
# duration therefore belongs in `_HISTOGRAM_BUCKETS_BY_UNIT` below, whose
# instruments the dashboard reads under unit-neutral keys instead.
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
    "kirocrew.tool.call.duration": _TOOL_CALL_BUCKETS_MS,
    "kirocrew.session.duration": _SESSION_BUCKETS_MS,
    "kirocrew.watchdog.idle.duration": _WATCHDOG_IDLE_BUCKETS_MS,
    # Embedding queue wait + inference time. Both are ms and both predate this
    # map; neither name ends in `.duration`, which is precisely why the guard
    # test's old name-suffix scan could not see them and they have been running
    # on OTEL's default 0..10000 boundaries. The guard now finds histograms by
    # reading their emit calls, so they are registered here. _FAST_BUCKETS_MS
    # because they behave like the other sub-ms-to-seconds work: a warm local
    # embed is single-digit ms, a cold model load or a long batch reaches
    # seconds.
    "kirocrew.embed.queue_wait": _FAST_BUCKETS_MS,
    "kirocrew.embed.inference": _FAST_BUCKETS_MS,
}

# Per-turn billed amount. Calibrated against 17,240 real per-turn credit rows
# from a kiro-backend install: min 0.03, p10 0.076, p50 6.8, p90 53, p99 155,
# max 658 — four and a half decades, so the array is one-bound-per-half-decade
# rather than dense anywhere. The bottom bound sits BELOW the observed minimum
# and the top two decades above the observed maximum, because a sample outside
# the explicit range has its percentile floored at the nearest bound (the
# overflow artifact `_HISTOGRAM_BUCKETS_MS` documents), and credit pricing is not
# ours to hold still.
_CREDIT_BUCKETS: list[float] = [
    0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5,
    10, 25, 50, 100, 250, 500, 1000, 2500,
]

# The same shape in dollars, one decade lower: a claude_code turn bills
# fractions of a cent at the low end and tens of dollars for a long tool-heavy
# turn. This is the one array here with NO local calibration — the host these
# bounds were sized on runs the kiro backend, so every `cost_usd` row it has is
# zero (the two are mutually exclusive by the "whichever is non-zero" contract).
# Sized from published per-token pricing against the observed token range
# instead, and worth re-checking against real rows once a claude_code host
# reports.
_USD_BUCKETS: list[float] = [
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
    0.5, 1, 2.5, 5, 10, 25, 50, 100,
]

# Non-duration histograms: instrument name -> boundaries, in the instrument's
# OWN unit. A SEPARATE map from _HISTOGRAM_BUCKETS_MS on purpose — that map's
# contract is "all values are ms", which the dashboard's generic aggregation
# relies on when it reports every histogram under `*_ms` keys, and adding a
# credit or a dollar amount to it would make both the contract and the reported
# key a lie. `test_provider_bucket_views.py` guards the split in both
# directions off the emitted `unit=`, NOT off the name suffix: nothing here is a
# millisecond instrument, nothing there is anything else, and every histogram
# instrument in the source must appear in exactly one of them. The suffix would
# be the wrong test — `kirocrew.embed.queue_wait` and `.inference` above are ms
# and do not carry it, which is the whole reason the guard reads units.
#
# The dashboard reads these two under unit-neutral keys (see
# `handlers/telemetry.py`'s turn block), which is what keeps them out of the
# `*_ms` surface.
_HISTOGRAM_BUCKETS_BY_UNIT: dict[str, list[float]] = {
    "kirocrew.turn.credits": _CREDIT_BUCKETS,
    "kirocrew.turn.cost_usd": _USD_BUCKETS,
}


def histogram_bounds() -> dict[str, list[float]]:
    """Every kirocrew histogram's explicit boundaries, ms and non-ms alike.

    One mapping so the View list below has a single source, and so the guard test
    can assert completeness over the whole instrument set rather than over the
    duration family alone. The two maps are disjoint (asserted), so merge order
    cannot hide an entry.
    """
    return {**_HISTOGRAM_BUCKETS_MS, **_HISTOGRAM_BUCKETS_BY_UNIT}


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
# default False). This gates LOCAL collection ONLY: external OTLP egress needs the
# active telemetry provider to name a destination (the default provider does so
# only for a non-empty ``telemetry.otlp_endpoint``), so merely flipping this var
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


def _resource_attributes() -> "dict[str, str | int]":
    """Resource attributes for the MeterProvider.

    A resource attribute becomes a LABEL on every series this process exports,
    which cuts both ways: it is the only place fleet-level GROUP BYs can come
    from, and any unbounded value here multiplies every instrument's series
    count. So every attribute is a closed set or explicitly clamped, and every
    probe fails soft -- a failed read omits the attribute rather than losing
    telemetry or inventing a value.

    ``service.instance.id`` is set EXPLICITLY to the persisted install id
    rather than left to the SDK, whose default identifies a PROCESS: a fresh
    UUID per restart starts a brand-new series set every time -- fast,
    pure-cost growth on desktops, which restart constantly -- and it severs
    "this install over time" across restarts. Machines must still be separable
    (a fleet percentile is computed ACROSS series; identical labels would
    collide every host into one series), which is why the id exists at all.
    It is a random UUID persisted on disk, deliberately NOT derived from
    hostname or username, which on a corporate desktop routinely embed the
    employee's alias.

    ``process.pid`` (OTel semconv) carries the PROCESS identity SEPARATELY:
    one install runs several telemetry-enabled processes at once (the gateway
    plus spawned agents/apps each build their own recorder -- the reason the
    local exporter shards per PID), and with an install-scoped resource alone
    their per-process gauges and cumulative counters would interleave into one
    corrupted series at any OTLP backend. The pid makes each process its own
    resource; the install id groups them back together for fleet questions
    (distinct devices, per-install rollups). PID reuse across restarts reads
    as an ordinary counter reset downstream.

    This function only ever READS the install id (``create=False``: one stat
    + a 32-byte read, the same class as the config fingerprint check). The
    single WRITE that can create it lives in :func:`_build_recorder`'s live
    branch, immediately before this call, so a resource is labelled from the
    very first export rather than acquiring its identity mid-life -- a
    mid-life resource swap reads downstream as a brand-new series set, which
    is a worse outcome than the ~1ms it would save. Keeping the mint in the
    build path rather than in a rebuild worker is also what lets this module
    hold no backfill state at all. Residual, accepted: omitting the key does
    NOT yield an unlabelled resource -- the SDK substitutes its own
    per-process UUID (measured: a dashed 36-char uuid4, versus our 32-char
    hex) -- so a host whose mint FAILS (unwritable data dir) falls back to
    exactly the per-restart series churn this attribute exists to prevent.
    That is the cost of a broken data dir, not of a fresh install: minting
    before the first build means there is no startup window in which the
    substitute can reach an export at all. Local shards stay unambiguous
    either way, because the exporter stamps per-process identity on every
    line.

    ``service.version`` is the release-clamped build version
    (:func:`beacon.release`, the same clamp the beacon ships). Without it
    nothing in a payload identifies the build that produced it, so
    release-over-release comparison degenerates to comparing time ranges --
    which a gradual rollout muddies, since both versions report into the same
    buckets. With the label it is a GROUP BY. The clamp matters as much as the
    field: a raw dev/nightly ``__version__`` carries a per-build stamp, so an
    unclamped value would mint a new series set per build.

    The environment attributes (``os.type``, ``host.arch``,
    ``process.runtime.name``/``version``) follow the OTel semantic conventions
    and are CLOSED sets: readings outside the known values fold to
    :data:`_ATTR_OTHER`, and the runtime version is clamped to ``major.minor``
    (the patch level adds cardinality without answering anything the minor
    does not -- the beacon's rule). ``host.cpu.logical_count`` is what lets
    ``kirocrew.process.cpu.seconds`` be normalized into a machine percentage
    downstream: the core count exists only client-side, so it must travel
    with the data.

    Deliberately absent:

      * the distribution channel. The beacon's data-minimization pass REMOVED
        its channel field because channel sharply narrows the crowd a stable id
        hides in (a nightly population is small by definition). Stamping it on
        every metric payload would quietly undo that decision; re-adding it is
        a consent-inventory question, not a code convenience.
      * an install type (desktop / cli / remote-gateway). There is no reliable
        detection today; a guessed label would be confidently wrong.
    """
    attrs: "dict[str, str | int]" = {"service.name": _SERVICE_NAME}
    try:
        # Process identity, SEPARATE from install identity: several
        # telemetry-enabled processes run per install, and without a
        # per-process resource their gauges/counters interleave into one
        # corrupted series at any OTLP backend.
        attrs["process.pid"] = int(os.getpid())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("process.pid resource attr unavailable: %s", exc)
    try:
        attrs["service.version"] = beacon.release(__version__)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("service.version resource attr unavailable: %s", exc)
    try:
        os_type = platform.system().lower()
        attrs["os.type"] = os_type if os_type in _KNOWN_OS_TYPES else _ATTR_OTHER
        machine = platform.machine().lower()
        attrs["host.arch"] = _ARCH_BY_MACHINE.get(machine, _ATTR_OTHER)
        runtime = platform.python_implementation().lower()
        attrs["process.runtime.name"] = (
            runtime if runtime in _KNOWN_RUNTIME_NAMES else _ATTR_OTHER
        )
        # beacon.python_minor() owns the major.minor clamp and records why the
        # patch level is dropped; a second spelling here would be a rule with
        # two owners.
        attrs["process.runtime.version"] = beacon.python_minor()
        cores = os.cpu_count()
        if cores:
            attrs["host.cpu.logical_count"] = int(cores)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("environment resource attrs unavailable: %s", exc)
    try:
        install_id = beacon.install_id(create=False)
        if install_id:
            attrs["service.instance.id"] = install_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("stable install id unavailable: %s", exc)
    return attrs


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

    # Resolve egress destinations BEFORE any reader is started. Two reasons:
    # the seam belongs to the edition and a PlatformCompositionError from it must
    # not be folded into "telemetry init failed" below, and resolving first means
    # there is nothing started to reap if it goes wrong.
    destinations = _otlp_destinations(cfg)

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
        # Opt-in OTLP egress: one reader per destination the active telemetry
        # provider supplied, appended AFTER the local sink so the local reader is
        # always present and can never be replaced. No destinations => local-only,
        # no network egress (the default). Each reader joins started_readers as it
        # is constructed, so a later failure reaps every earlier one.
        otlp_names: list = []
        for dest in destinations:
            reader = _build_otlp_reader(dest, cfg)
            if reader is not None:
                started_readers.append(reader)
                otlp_names.append(dest.name)
        otlp_active = bool(otlp_names)
        # Mint the persisted install id HERE, in the live branch, so the
        # resource below is labelled from the very first export instead of
        # acquiring its identity mid-life. This is the one write on this path:
        # mkdir + mkstemp + link, race-safe per ``beacon.install_id``, once per
        # install ever, and only ever under consent True (a disabled build
        # returns before reaching this branch, so turning telemetry OFF still
        # creates nothing). It is noise against what this same path already
        # pays synchronously -- ~14ms for a changed-config load plus ~57ms of
        # SDK import, both documented in docs/system-specs/modules/metrics.md
        # -- and a failed mint costs only the attribute, never the recorder.
        try:
            beacon.install_id(create=True)
        except Exception:
            logger.debug("install id mint failed", exc_info=True)
        resource_attrs = _resource_attributes()
        provider = MeterProvider(
            metric_readers=started_readers,
            resource=Resource.create(resource_attrs),
            # One View per instrument, from histogram_bounds() (the ms families
            # plus the non-ms ones). Deliberately NOT a catch-all
            # `instrument_type=Histogram` View: the OTEL SDK applies every
            # matching View, so a catch-all alongside these would publish each
            # named instrument twice under one metric name with different
            # bounds, and the telemetry aggregator merges same-length bucket
            # arrays without comparing bounds — it would silently double the
            # counts. See the completeness guard test.
            views=[
                View(
                    instrument_name=name,
                    aggregation=ExplicitBucketHistogramAggregation(bounds),
                )
                for name, bounds in histogram_bounds().items()
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
            # leaving the machine. Destination NAMES only — the endpoint value is
            # never logged, and with more than one collector the name is the only
            # way to tell which one is live.
            logger.info(
                "telemetry OTLP export active; metrics leave this machine (%s)",
                ", ".join(otlp_names),
            )
        meter = provider.get_meter(_SCOPE)
        # Process-resource gauges (threads/fds/gc/rss/cpu) ride the same consent
        # gate: registered only on this live path, and their observable
        # callbacks run solely when a reader collects. Best-effort — a failure
        # (including an import failure in a broken install) loses these gauges,
        # never telemetry as a whole, so it must not reach the outer except.
        try:
            from kiro_crew.metrics.process_gauges import register_process_gauges

            register_process_gauges(meter)
        except Exception:
            logger.warning("process gauges unavailable", exc_info=True)
        # Install-inventory gauges (crons/skills/knowledge/MCP/toggles) ride the
        # same consent gate and the same observable-callback contract. Registered
        # in its OWN try so a failure in either set cannot cost the other one —
        # they read entirely different subsystems, and the process gauges must
        # not go dark because, say, the skills tree is unreadable.
        try:
            from kiro_crew.metrics.inventory_gauges import register_inventory_gauges

            register_inventory_gauges(meter)
        except Exception:
            logger.warning("inventory gauges unavailable", exc_info=True)
        return _Build(MetricsRecorder(meter), provider, consent)
    except Exception as exc:
        logger.warning("telemetry init failed; metrics disabled: %s", exc)
        # Reap EVERY reader already started, not just the first: an egress
        # destination adds one each, so there can be several, and leaving any of
        # them ticking is the same orphan this branch exists to prevent.
        #
        # Off this thread, because a reader shutdown performs a final export and
        # joins its ticker with the SDK's 30s default.
        _reap_readers_detached(started_readers)
        return _Build(MetricsRecorder(None), None, consent)


def _egress_degraded() -> tuple:
    """Report a provider that could not answer, then contribute no destinations.

    Separate from the call site because ``safe_context_call`` invokes a
    ``fallback_factory`` ONLY on the degrade path, which is what turns "the
    provider raised" into one WARNING a default install actually collects.
    """
    logger.warning(
        "telemetry provider raised while resolving OTLP destinations; "
        "OTLP egress disabled (local-only)"
    )
    return ()


def otlp_egress_active(cfg: "TelemetryConfig") -> bool:
    """Whether enabling collection under *cfg* would send metrics off this machine.

    The disclosure surfaces (the Privacy panel's ``otlp_configured``, and the
    config route that refuses to ENABLE collection from a switch offered as
    local-only) MUST answer this question from the same resolved destination set
    ``_build_recorder`` will attach — not from ``telemetry.otlp_endpoint``, which
    is only how the DEFAULT provider happens to name a destination. An edition
    that supplies its own collector would otherwise leave the panel reporting
    "nothing is exported" while metrics leave the machine: two answers to one
    consent question, which is the defect this seam must not introduce.

    RAISES when posture cannot be established, and that is deliberate: the two
    directions of "fail closed" are OPPOSITE here. At BUILD time an unresolvable
    provider must not export, so ``_otlp_destinations`` degrades to ``()`` and
    collection continues local-only. At GATE time it must not read as "no
    egress" — a caller that saw ``False`` would permit an enable that the
    provider, once recovered, turns into egress. So callers handle the raise: the
    config route answers 409, and the panel reports egress rather than promising
    local-only.

    A provider that does not IMPLEMENT the method is a different case and answers
    ``False``, not a raise. Providers are matched structurally with no inherited
    default and the contract assertion compares only a version integer, so an
    edition built before this seam composes fine and simply has no method. That
    is a KNOWN answer — it contributes no destination through this seam, exactly
    as ``_build_recorder`` concludes on the same host — so treating it as
    "unknown" would make the two surfaces disagree, report egress that provably
    cannot happen, and brick the dashboard's enable switch. An ``AttributeError``
    raised INSIDE an implementation still propagates: only an absent attribute is
    read as no-destinations.
    """
    provider = current_context().telemetry
    resolve = getattr(provider, "otlp_destinations", None)
    if resolve is None:
        return False
    return bool(_filter_metric_destinations(resolve(cfg)))


def _filter_metric_destinations(
    supplied: "Iterable[OtlpDestination] | None",
) -> "tuple[OtlpDestination, ...]":
    """Keep only the destinations this core would build a METRIC reader for.

    Deny-by-default: a destination is kept only when it positively names an
    ``endpoint`` AND the ``"metrics"`` signal. A descriptor that is
    half-populated, or one aimed at a signal this core does not emit, contributes
    nothing rather than being coerced into an egress nobody asked for.
    """
    kept = []
    for dest in supplied or ():
        endpoint = str(getattr(dest, "endpoint", "") or "").strip()
        signals = frozenset(getattr(dest, "signals", ()) or ())
        if not endpoint or "metrics" not in signals:
            continue
        kept.append(dest)
    return tuple(kept)


def _otlp_destinations(cfg: "TelemetryConfig") -> "tuple[OtlpDestination, ...]":
    """Resolve the metric egress destinations the active edition supplies.

    The core owns WHAT may leave this machine (consent, attribute sanitisation,
    the views, batching); an edition owns only WHERE it goes, which it declares
    through ``TelemetryProvider.otlp_destinations``. The public default turns
    ``telemetry.otlp_endpoint`` into exactly one destination, so a standalone
    build behaves as it did when this module constructed that exporter itself.

    Lenient by design, unlike :func:`otlp_egress_active`: on this path an
    unresolvable provider must not export, so it contributes nothing and
    collection continues local-only.
    """
    try:
        supplied = safe_context_call(
            lambda: current_context().telemetry.otlp_destinations(cfg),
            # A raising provider contributes NOTHING, but it must not do so
            # quietly: safe_context_call's own log_message is debug-level, and a
            # default install collects WARNING and above, so relying on it alone
            # is how a broken provider becomes an undiagnosable permanent no-op.
            # fallback_factory is invoked only on the degrade path (and inside the
            # except), which makes it the hook for one loud line; log_message
            # still carries the traceback at debug for whoever wants it.
            fallback_factory=_egress_degraded,
            log_message="telemetry provider raised in otlp_destinations",
        )
    except PlatformCompositionError:
        # Deliberate departure from the usual re-raise. For an EGRESS seam the
        # closed state is "no destinations", so a host that cannot compose its
        # companion must land there rather than turn a metric call into a raise:
        # boot_platform already fail-closes on composition for the paths where
        # that is the security-relevant answer, and processes exist that never
        # boot the platform at all (the MCP sidecar). Degrading here removes
        # egress; it can never add any.
        logger.warning("platform context unavailable; OTLP egress disabled (local-only)")
        return ()

    return _filter_metric_destinations(supplied)


def _build_otlp_reader(dest: "OtlpDestination", cfg: object) -> Optional["_ReaderT"]:
    """Build one OTLP/HTTP metric reader for *dest*, or None when unavailable.

    Egress is OFF by default: reaching here at all means an edition named a
    destination (the public default does so only when ``telemetry.otlp_endpoint``
    is a non-empty string). The OTLP exporter lives in the separate
    ``otlp`` package extra (install its exporter directly -- see
    :mod:`kiro_crew.extras`), not the hard dependency set. If a host opts in without
    installing it, we log a warning and degrade to local-only rather than
    crashing telemetry init. The exporter sees only what the MetricsRecorder
    facade lets through: attributes are sanitised before they reach any reader,
    and call sites are required to pass low-cardinality constants rather than
    prompts, content, tokens, paths or user ids. That sanitisation is defence in
    depth over that requirement, not a substitute for it, so egress is only as
    safe as the call sites feeding it.

    The export CADENCE stays a core decision, read from
    ``telemetry.export_interval_seconds`` — a destination says where to send, not
    how often to send. TEMPORALITY is a core decision for the same reason: both
    of this provider's readers describe the same instruments, so they must encode
    them the same way unless the operator says otherwise
    (:func:`kiro_crew.metrics.temporality.otlp_preference`).
    """
    endpoint = dest.endpoint
    # Callable directly (not only via _build_recorder), so make sure the lazily
    # imported SDK symbols this needs are bound.
    if not _load_otel():
        logger.warning("opentelemetry not importable; OTLP egress disabled")
        return None
    # After the gate: this module imports the OTel metrics SDK at module scope,
    # and the whole point of _load_otel is that a default-off host never pays
    # for it.
    from kiro_crew.metrics import temporality

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
    except ImportError:
        # Never log the configured endpoint: URLs may contain credentials in
        # userinfo or query parameters. The destination NAME is safe by contract
        # and is enough for diagnosis without exposing the value.
        logger.warning(
            "OTLP destination %r is configured but opentelemetry-exporter-otlp-"
            "proto-http is not installed; OTLP egress disabled (local-only)",
            dest.name,
        )
        return None
    try:
        kwargs: dict = {"endpoint": endpoint}
        # The same DELTA map the local sink uses, so one MeterProvider's two
        # destinations cannot report the same instrument differently. Omitted
        # entirely when the operator has set the OTel temporality variable: the
        # exporter applies an explicit dict ON TOP of whichever base that
        # variable chose, so passing it unconditionally would override an
        # operator who asked for CUMULATIVE. See metrics.temporality.
        preference = temporality.otlp_preference()
        if preference is not None:
            kwargs["preferred_temporality"] = preference
        # Passed only when supplied so the exporter keeps its own defaults (and
        # its env-var fallbacks) for everything an edition did not set.
        if dest.session is not None:
            # An authenticated transport. requests re-evaluates Session.auth per
            # request, which is what lets a credential that rotates mid-process
            # keep working where a header frozen at construction would 401. Static
            # per-destination headers ride on Session.headers, so they need no
            # separate field on the descriptor.
            kwargs["session"] = dest.session
        exporter = OTLPMetricExporter(**kwargs)
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
        logger.warning(
            "OTLP exporter init failed for destination %r; egress disabled (local-only)",
            dest.name,
        )
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
