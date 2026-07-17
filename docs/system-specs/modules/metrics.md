# Metrics Telemetry Module

Last Updated: 2026-07-13 (M2: acceleration + turn metrics, /api/telemetry/startup handler, explicit-bucket View + DELTA temporality)

## Overview

Local-first metrics telemetry built on the OpenTelemetry SDK (Apache-2.0 / CNCF).
The trunk is designed so future work is purely adding instrument calls at call
sites — never changing this plumbing. **Default OFF** (`telemetry.enabled:
false`): all metric call sites are cheap no-ops and nothing is written or
exported, byte-identical to no telemetry (mirrors the `mcp_gateway.enabled` /
`skills.lazy_load` opt-in convention).

Source: `src/kiro_crew/metrics/` — `schema.py`, `recorder.py`, `provider.py`,
`local_exporter.py`. Tests: `test/metrics/`.

## Components

| File | Purpose |
|------|---------|
| `schema.py` | Namespace constants (`NS_CORE = "kirocrew."`, `NS_GENAI = "gen_ai."`, `NS_APP_PREFIX = "app."`) + `validate_name` / `validate_attrs` / `redact` guardrails. Documents the low-cardinality contract. |
| `recorder.py` | `MetricsRecorder` — facade over the OTEL `Meter`. Every metric passes namespace + privacy guardrails BEFORE reaching an instrument. Instrument-cache creation is lock-guarded (atomic check-then-create). Best-effort: a telemetry failure never propagates to the caller. `meter=None` = no-op recorder. |
| `provider.py` | Consent gate + process-global recorder (`get_recorder()`) + graceful `shutdown()` / `reset_for_testing()`. When enabled, wires a `PeriodicExportingMetricReader` to the local JSONL exporter. Installs a `View` applying `ExplicitBucketHistogramAggregation(_LATENCY_BUCKETS_MS)` (1ms–60s boundaries) to EVERY histogram so bucket-derived p50/p90 stay meaningful across the full startup / acquire / lazy-load range (OTEL's default buckets top out at 10s). |
| `local_exporter.py` | `JsonlMetricExporter` — appends one JSON line per export cycle to `<dir>/metrics-YYYY-MM-DD-<pid>.jsonl` (default dir `~/.kirocrew/metrics`). Per-pid single-writer shards; dir 0o700 / file 0o600. Nothing egresses the host; OTLP is a deferred, separate opt-in. Declares DELTA `preferred_temporality` for Counter/UpDownCounter/Histogram so each export cycle writes the delta since the last — daily aggregation reduces to an element-wise sum of bucket counts across cycles/PIDs and stays correct across restarts and day boundaries. |

## Guardrails (contract C4)

- **Namespace**: core callers must use `kirocrew.*` or `gen_ai.*`; app callers
  must use `app.<app_id>.*` and cannot spoof the core/gen_ai namespaces
  (`validate_name` raises `ValueError`, the recorder swallows it, nothing is
  recorded).
- **Privacy**: string attribute values pass `redact()` — AKIA/ASIA keys,
  `SecretAccessKey=`, private-key headers, 40+ char hex, JWT shapes,
  `password=`/`token=` patterns, base64-encoded credential variants, and a
  Shannon-entropy heuristic all yield `"[REDACTED]"`. The first-party
  `kiro_crew.security` scrubbers (`redact_credentials`,
  `redact_exfiltration_urls` — both return `(cleaned, warnings)` tuples) are
  also consulted. Long non-suspicious strings are truncated to
  `MAX_ATTR_VALUE_LEN` (128).
- **Cardinality**: metric names + attribute values must be low-cardinality
  constants; attribute count is capped at `MAX_ATTR_COUNT` (32). Instrument
  caches are keyed by name and never evicted.

## Configuration

`TelemetryConfig` in `config/loader.py` (section `telemetry` in
`~/.kirocrew/config.json`):

| Field | Default | Meaning |
|-------|---------|---------|
| `enabled` | `false` | Main switch. Off = no-op recorder, nothing written. |
| `local_dir` | `""` | JSONL shard dir; empty = `~/.kirocrew/metrics`. `~` expansion supported. |
| `export_interval_seconds` | `60` | Flush interval (floored to 1). |

## Instrumented signals

| Metric | Type | Attrs | Site |
|--------|------|-------|------|
| `kirocrew.session.startup.duration` | histogram (ms) | `outcome` (`ready` / `auth_required` / `error`), `spawned` (bool) | `acp/client.py::AcpClient.ensure_ready()` — times cold-start (spawn + session init) and emits in a `finally` so every exit path is measured. The warm fast-path is NOT measured. `outcome` defaults to `"error"` so an unexpected exception is never mislabeled `"ready"`. |
| `kirocrew.turn.duration` | histogram (ms) | `outcome` (`ok` / `timeout` / `error`), `session_source` (via `validation.infer_use_case`) | `dashboard/chat_runner.py::_emit_turn_metric`, called at EVENT_COMPLETE after `persist_token_record_async`. `_turn_outcome` maps stop_reason (`""`/`end_turn`/`stop`/`completed` → ok). One histogram powers turn latency p50/p90 AND fault rate. |
| `kirocrew.mcp.backend.acquire.duration` | histogram (ms) | `warm` (bool — `not was_spawned`) | `mcp_gateway/gatewayd.py::_emit_backend_acquire_metric` — ensure_backend pre-flight + lazy-spawn paths; acquire-only duration captured before attach_stub/create_task overhead. |
| `kirocrew.mcp.lazy_load.count` / `.duration` | counter + histogram (ms) | `transport` (`stdio`) | `mcp_gateway/gatewayd.py::_emit_lazy_load_metrics` — legacy lazy-spawn path (also emits backend.acquire). |
| `kirocrew.mcp.warm_pool.acquire` | counter | `result` (`hit` / `miss`) | `mcp_gateway/prewarm.py::HotKeyStore.record_outcome` (emitted outside the lock). |
| `kirocrew.skill.lazy_load.count` / `.duration` | counter + histogram (ms) | `hit` (bool) | `skills.py::SkillsLoader.load_skill` via `_emit_lazy_load_metric` (best-effort; never breaks skill loading). |

Note: the fork's primary kiro chat path uses `AcpSessionProvider.ensure_ready()`
(a no-op liveness check), so this histogram measures AcpClient-based cold starts
(knowledge `llm_pool`, review pools, client-internal callers).

## Dashboard handler

`dashboard/handlers/telemetry.py` — `GET /api/telemetry/startup` scans the JSONL
shards (14-day window, shard-fingerprint + 30s-TTL cache, aggregation offloaded
via `asyncio.to_thread`), aggregates the startup histogram into p50/p90 split by
cold/warm (`spawned` attr) + outcome + daily series, the turn histogram into a
`turn` block (stats + outcome counts + `fault_rate`), and generically surfaces
every other `kirocrew.*` metric (`other` list) so new emit call-sites appear
without a handler change. Percentiles are interpolated from bucket counts (made
meaningful by the DELTA temporality + explicit-bucket View). Security: the
user-configurable `telemetry.local_dir` and each shard pass `validate_file_path`
(sensitive-path check) before any read. Cross-process: metrics are emitted by
the ACP/gateway processes, so reading the durable shards is the only correct
path (an in-memory reservoir in the dashboard process would never see them).

## Circular-import rule

`metrics/provider.py` imports `config.loader` at module top; call sites reached
from inside `config.loader`'s import chain (e.g. `acp/client.py`) MUST import
`get_recorder` lazily (inside the function) so the provider is never loaded
during that chain.
