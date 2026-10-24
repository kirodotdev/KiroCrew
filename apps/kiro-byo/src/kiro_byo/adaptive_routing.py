"""Runtime evidence for deterministic Tiga model routing.

The adaptive layer deliberately does *not* use another LLM to choose a model.
It turns bounded, content-free TurnTelemetry into recent performance evidence
that the deterministic router can blend with static/curated model priors.

Only facts we can actually observe are adapted today:

* speed: recent first-visible and total turn latency, relative to peers;
* reliability: recent success/error outcomes, including the current task class.

Quality/cost priors remain catalog facts until Kiro BYO has a trustworthy,
provider-neutral signal for them. This prevents "learning" made-up facts from
Token counts or model names.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .policy_packs import AdaptiveRoutingProfile
from .telemetry import TurnTelemetryStore
from .runtime_trace import RuntimeTraceStore


def _profile(value: AdaptiveRoutingProfile | None) -> AdaptiveRoutingProfile:
    return value if value is not None else AdaptiveRoutingProfile()


def task_profile_label(value: str) -> str:
    return {
        "vision": "Vision",
        "code": "Code",
        "tool": "Tool",
        "long_context": "Long Context",
        "research": "Research",
        "casual": "Casual",
        "concrete": "Concrete Model",
    }.get(str(value or ""), str(value or "Unknown"))


def confidence_label(samples: int, profile: AdaptiveRoutingProfile | None = None) -> str:
    profile = _profile(profile)
    n = max(0, int(samples or 0))
    if n >= profile.confidence_high:
        return "high"
    if n >= profile.confidence_medium:
        return "medium"
    if n >= 1:
        return "low"
    return "none"


def _recency_weight(timestamp: float, now: float, profile: AdaptiveRoutingProfile) -> float:
    if not timestamp or timestamp <= 0:
        return 0.35
    age = max(0.0, now - timestamp)
    half_life_seconds = max(1.0, profile.half_life_days * 24.0 * 60.0 * 60.0)
    return max(0.08, 0.5 ** (age / half_life_seconds))


def _weighted_mean(values: Sequence[tuple[float, float]]) -> float | None:
    clean = [(float(value), max(0.0, float(weight))) for value, weight in values if math.isfinite(float(value))]
    total = sum(weight for _value, weight in clean)
    if total <= 0:
        return None
    return sum(value * weight for value, weight in clean) / total


def _median(values: Iterable[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(clean) if clean else None


@dataclass(frozen=True)
class RuntimeModelEvidence:
    provider_id: str
    model_id: str
    samples: int = 0
    successful_samples: int = 0
    task_samples: int = 0
    success_rate: float | None = None
    task_success_rate: float | None = None
    reliability: float = 0.94
    reliability_prior: float = 0.94
    median_ttft_ms: float | None = None
    median_total_ms: float | None = None
    latency_index: float | None = None
    runtime_speed: float | None = None
    confidence: str = "none"
    sample_full: int = 24
    task_sample_full: int = 16

    @property
    def sample_factor(self) -> float:
        return min(1.0, max(0.0, float(self.samples)) / float(max(1, self.sample_full)))

    @property
    def task_factor(self) -> float:
        return min(1.0, max(0.0, float(self.task_samples)) / float(max(1, self.task_sample_full)))


@dataclass(frozen=True)
class AdaptiveRoutingSnapshot:
    evidence: Mapping[tuple[str, str], RuntimeModelEvidence]
    turns: int
    task_profile: str
    generated_at: float
    profile: AdaptiveRoutingProfile = AdaptiveRoutingProfile()

    def for_model(self, provider_id: str, model_id: str) -> RuntimeModelEvidence:
        return self.evidence.get(
            (str(provider_id), str(model_id)),
            RuntimeModelEvidence(
                str(provider_id), str(model_id),
                reliability=self.profile.reliability_prior,
                reliability_prior=self.profile.reliability_prior,
                sample_full=self.profile.sample_full,
                task_sample_full=self.profile.task_sample_full,
            ),
        )

    @property
    def observed_models(self) -> int:
        return sum(1 for row in self.evidence.values() if row.samples > 0)


_SNAPSHOT_CACHE: dict[tuple[Any, ...], AdaptiveRoutingSnapshot] = {}
_SNAPSHOT_CACHE_LOCK = threading.Lock()


def snapshot_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_profile: str = "",
    now: float | None = None,
    profile: AdaptiveRoutingProfile | None = None,
) -> AdaptiveRoutingSnapshot:
    adaptive_profile = _profile(profile)
    now = float(now if now is not None else time.time())
    task_key = str(task_profile or "")
    buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        provider = str(row.get("provider_id") or "").strip()
        model = str(row.get("served_model") or "").strip()
        if not provider or not model:
            continue
        buckets.setdefault((provider, model), []).append(row)

    interim: dict[tuple[str, str], dict[str, Any]] = {}
    for key, model_rows in buckets.items():
        weighted_success: list[tuple[float, float]] = []
        task_weighted_success: list[tuple[float, float]] = []
        latency_values: list[tuple[float, float]] = []
        ttfts: list[float] = []
        totals: list[float] = []
        successes = 0
        task_samples = 0
        for row in model_rows:
            timestamp = float(row.get("timestamp") or 0.0)
            weight = _recency_weight(timestamp, now, adaptive_profile)
            success = 1.0 if str(row.get("outcome") or "") == "success" else 0.0
            weighted_success.append((success, weight))
            if success:
                successes += 1
            row_profile = str(row.get("task_profile") or "")
            if task_key and row_profile == task_key:
                task_samples += 1
                task_weighted_success.append((success, weight))
            if success:
                first = float(row.get("first_visible_ms") or -1.0)
                total = float(row.get("total_ms") or -1.0)
                if first >= 0:
                    ttfts.append(first)
                if total >= 0:
                    totals.append(total)
                if total > 0:
                    # Total latency dominates perceived completion time; TTFT
                    # remains meaningful without letting one streaming style own
                    # the entire speed score.
                    first_for_index = max(0.0, first) if first >= 0 else total * 0.20
                    latency_weight_total = adaptive_profile.latency_ttft_weight + adaptive_profile.latency_total_weight
                    latency_weight_total = max(1e-9, latency_weight_total)
                    ttft_weight = adaptive_profile.latency_ttft_weight / latency_weight_total
                    total_weight = adaptive_profile.latency_total_weight / latency_weight_total
                    metric = ttft_weight * math.log1p(first_for_index) + total_weight * math.log1p(total)
                    latency_values.append((metric, weight))

        weighted = _weighted_mean(weighted_success)
        reliability = (
            sum(value * weight for value, weight in weighted_success) + adaptive_profile.reliability_prior * adaptive_profile.reliability_prior_weight
        ) / (sum(weight for _value, weight in weighted_success) + adaptive_profile.reliability_prior_weight)
        task_weighted = _weighted_mean(task_weighted_success) if task_weighted_success else None
        latency_index = _weighted_mean(latency_values)
        interim[key] = {
            "samples": len(model_rows),
            "successful_samples": successes,
            "task_samples": task_samples,
            "success_rate": weighted,
            "task_success_rate": task_weighted,
            "reliability": max(0.0, min(1.0, reliability)),
            "median_ttft_ms": _median(ttfts),
            "median_total_ms": _median(totals),
            "latency_index": latency_index,
        }

    # Runtime speed is deliberately *relative to currently observed peers*.
    # Absolute latency varies by task, network and Provider. Rank-based scoring
    # remains stable without pretending a global 5s threshold means the same
    # thing for every workload.
    latency_ranked = sorted(
        ((key, row["latency_index"]) for key, row in interim.items() if row["latency_index"] is not None),
        key=lambda item: float(item[1]),
    )
    runtime_speed: dict[tuple[str, str], float] = {}
    if len(latency_ranked) == 1:
        runtime_speed[latency_ranked[0][0]] = adaptive_profile.runtime_speed_single
    elif len(latency_ranked) > 1:
        denom = float(len(latency_ranked) - 1)
        for rank, (key, _value) in enumerate(latency_ranked):
            percentile = 1.0 - (rank / denom)
            # Avoid extreme 0/1 claims from a tiny bounded history.
            runtime_speed[key] = adaptive_profile.runtime_speed_floor + adaptive_profile.runtime_speed_span * percentile

    evidence: dict[tuple[str, str], RuntimeModelEvidence] = {}
    for key, row in interim.items():
        evidence[key] = RuntimeModelEvidence(
            provider_id=key[0],
            model_id=key[1],
            samples=int(row["samples"]),
            successful_samples=int(row["successful_samples"]),
            task_samples=int(row["task_samples"]),
            success_rate=row["success_rate"],
            task_success_rate=row["task_success_rate"],
            reliability=float(row["reliability"]),
            reliability_prior=adaptive_profile.reliability_prior,
            median_ttft_ms=row["median_ttft_ms"],
            median_total_ms=row["median_total_ms"],
            latency_index=row["latency_index"],
            runtime_speed=runtime_speed.get(key),
            confidence=confidence_label(int(row["samples"]), adaptive_profile),
            sample_full=adaptive_profile.sample_full,
            task_sample_full=adaptive_profile.task_sample_full,
        )
    return AdaptiveRoutingSnapshot(
        evidence=evidence,
        turns=len(rows),
        task_profile=task_key,
        generated_at=now,
        profile=adaptive_profile,
    )


def runtime_failure_rows(trace: RuntimeTraceStore | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
    """Project recoverable model-role failures into reliability observations.

    A successful final turn can hide a provider fallback, failed Glitter worker
    or failed reviewer. Those are real reliability facts for the model that
    failed even though TurnTelemetry correctly records the final turn as a
    success. RuntimeTrace already stores these events without prompt content in
    metadata mode, so the adaptive layer can use them without inventing a new
    content ledger.
    """
    trace = trace or RuntimeTraceStore()
    try:
        events = trace.recent(limit)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for event in events:
        name = str(event.get("event") or "")
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        provider = model = ""
        if name == "model.route.fallback":
            provider = str(data.get("from_provider") or "")
            model = str(data.get("from_model") or "")
        elif name == "model.relay.worker_failed":
            provider = str(data.get("provider_id") or "")
            model = str(data.get("model") or "")
        elif name.startswith("model.execution.") and name.endswith(".failed"):
            provider = str(data.get("review_provider") or "")
            model = str(data.get("review_model") or "")
        if not provider or not model:
            continue
        out.append({
            "timestamp": float(event.get("timestamp") or 0.0),
            "provider_id": provider,
            "served_model": model,
            "outcome": "error",
            "task_profile": "",
            "first_visible_ms": -1.0,
            "total_ms": -1.0,
            "adaptive_source": name,
        })
    return out


def current_snapshot(
    *, task_profile: str = "", limit: int | None = None,
    profile: AdaptiveRoutingProfile | None = None,
) -> AdaptiveRoutingSnapshot:
    """Return a snapshot cached until telemetry or routing-failure trace changes.

    Glitter may route several worker invocations in one turn. Re-reading the
    bounded ledgers for each worker would add pointless disk I/O, so all task
    profiles share a tiny mtime-keyed process cache.
    """
    profile = _profile(profile)
    limit = int(limit if limit is not None else profile.history_limit)
    store = TurnTelemetryStore(max_records=max(500, limit))
    trace = RuntimeTraceStore()
    try:
        stat = store.path.stat()
        telemetry_mtime = int(stat.st_mtime_ns)
        telemetry_size = int(stat.st_size)
    except OSError:
        telemetry_mtime = 0
        telemetry_size = 0
    try:
        trace_stat = trace.path.stat()
        trace_mtime = int(trace_stat.st_mtime_ns)
        trace_size = int(trace_stat.st_size)
    except OSError:
        trace_mtime = 0
        trace_size = 0
    # Keep each ledger's identity as independent key components. Folding two
    # mtimes with XOR (and summing sizes) collided whenever the two files shared
    # an mtime or merely swapped values, which could silently serve a stale
    # routing snapshot. Fixed-width ints make a precise key cheap, so there is
    # no need to compress identity at the cost of correctness.
    key = (
        str(store.path),
        telemetry_mtime, telemetry_size,
        trace_mtime, trace_size,
        int(limit), str(task_profile or ""), profile,
    )
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        rows = store.recent(limit)
    except OSError:
        rows = []
    rows = list(rows) + runtime_failure_rows(trace, limit=limit)
    snapshot = snapshot_from_rows(rows, task_profile=task_profile, profile=profile)
    with _SNAPSHOT_CACHE_LOCK:
        # Evict the single oldest entry (dict preserves insertion order) rather
        # than clearing the whole cache, so one new key cannot wipe every other
        # task profile's valid snapshot and cause routing-wide re-reads.
        while len(_SNAPSHOT_CACHE) >= 24:
            _SNAPSHOT_CACHE.pop(next(iter(_SNAPSHOT_CACHE)))
        _SNAPSHOT_CACHE[key] = snapshot
    return snapshot


def blend_speed(
    prior: float, evidence: RuntimeModelEvidence, *, max_alpha: float,
    profile: AdaptiveRoutingProfile | None = None,
) -> float:
    profile = _profile(profile)
    runtime = evidence.runtime_speed
    if runtime is None or evidence.samples <= 0:
        return max(0.0, min(1.0, float(prior)))
    alpha = max(0.0, min(profile.speed_blend_ceiling, float(max_alpha))) * evidence.sample_factor
    return max(0.0, min(1.0, (1.0 - alpha) * float(prior) + alpha * float(runtime)))


def reliability_adjustment(
    evidence: RuntimeModelEvidence, *, strength: float,
    profile: AdaptiveRoutingProfile | None = None,
) -> float:
    """Small bounded score adjustment from observed success/reliability.

    This is a reliability signal, *not* a claim that telemetry directly measures
    model intelligence. Task-specific evidence receives extra weight only after
    enough matching turns exist.
    """
    profile = _profile(profile)
    if evidence.samples <= 0:
        return 0.0
    # Policy data already bounds the requested blend. The core only enforces a
    # universal [0, 1] safety range instead of imposing another hidden Type
    # personality constant.
    strength = max(0.0, min(1.0, float(strength)))
    baseline = float(evidence.reliability_prior)
    global_delta = (evidence.reliability - baseline) * strength * evidence.sample_factor
    task_delta = 0.0
    if evidence.task_success_rate is not None and evidence.task_samples > 0:
        task_delta = (
            float(evidence.task_success_rate) - baseline
        ) * strength * profile.reliability_task_weight * evidence.task_factor
    return max(
        profile.reliability_adjustment_floor,
        min(profile.reliability_adjustment_ceiling, global_delta + task_delta),
    )
