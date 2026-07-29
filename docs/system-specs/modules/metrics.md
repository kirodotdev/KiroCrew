# Metrics Telemetry Module

Last Updated: 2026-07-24 (explicit retention opt-in, installable OTLP extra, WebSocket/SSE lifetime exclusion)

## Overview

Local-first metrics telemetry built on the OpenTelemetry SDK (Apache-2.0 / CNCF).
The trunk is designed so future work is purely adding instrument calls at call
sites — never changing this plumbing. **Default OFF** (`telemetry.enabled:
false`): all metric call sites are cheap no-ops and nothing is written or
exported, byte-identical to no telemetry (mirrors the `mcp_gateway.enabled` /
`skills.lazy_load` opt-in convention).

Source: `src/kiro_crew/metrics/` — `schema.py`, `recorder.py`, `provider.py`,
`local_exporter.py`, `http_metrics.py`. Tests: `test/metrics/`.

## Components

| File | Purpose |
|------|---------|
| `schema.py` | Namespace constants (`NS_CORE = "kirocrew."`, `NS_GENAI = "gen_ai."`, `NS_APP_PREFIX = "app."`) + `validate_name` / `validate_attrs` / `redact` guardrails. Documents the low-cardinality contract. |
| `recorder.py` | `MetricsRecorder` — facade over the OTEL `Meter`. Every metric passes namespace + privacy guardrails BEFORE reaching an instrument. Instrument-cache creation is lock-guarded (atomic check-then-create). Best-effort: a telemetry failure never propagates to the caller. `meter=None` = no-op recorder. |
| `provider.py` | Consent gate + process-global recorder (`get_recorder()`) + graceful `shutdown()` / `reset_for_testing()`. When enabled, wires a `PeriodicExportingMetricReader` to the local JSONL exporter. Installs a `View` applying `ExplicitBucketHistogramAggregation(_LATENCY_BUCKETS_MS)` (1ms–60s boundaries) to EVERY histogram so bucket-derived p50/p90 stay meaningful across the full startup / acquire / lazy-load range (OTEL's default buckets top out at 10s). |
| `local_exporter.py` | `JsonlMetricExporter` — appends one JSON line per export cycle to `<dir>/metrics-YYYY-MM-DD-<pid>.jsonl` (default dir `~/.kiro/crew/metrics`). Per-PID single-writer shards keep append + rotation lock-free, so concurrent exporters do not lose DELTA cycles. A private `.metrics.lock` serializes only retention sweeps; pruning skips canonical shards owned by live PIDs or modified within the safety window. **Bounded retention (rec #14):** shards rotate before an append exceeds `max_total_mb`; closed/expired shards are pruned directly by age and oldest-first size. Pruning is throttled to at most once per 300s and fully best-effort. Dir mode is 0o700, file mode 0o600, and nothing egresses the host. Declares DELTA `preferred_temporality` for Counter/UpDownCounter/Histogram so daily aggregation is an element-wise sum across cycles/PIDs. |
| `http_metrics.py` | Gateway HTTP observability (rec #1): `record_boot_to_ready()` (boot-to-ready histogram) + `make_route_latency_middleware()` (per-route latency, wired as the outermost middleware on both `start_dashboard`/`start_api_server`). Bounds `route_template` cardinality via `collect_route_templates()` (build-time snapshot) + `route_template()` (`__unknown__` fallback); clamps `method` to a fixed allowlist and `status_class` to `1xx`..`5xx`/`other`. Upgraded WebSocket connections and `text/event-stream` SSE responses are excluded because their handler elapsed time is connection/turn lifetime, not HTTP request latency. Best-effort — a telemetry failure never alters a response. |

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
`~/.kiro/crew/config.json`):

| Field | Default | Meaning |
|-------|---------|---------|
| `enabled` | `false` | Main switch. Off = no-op recorder, nothing written. |
| `local_dir` | `""` | JSONL shard dir; empty = `~/.kiro/crew/metrics`. `~` expansion supported. |
| `export_interval_seconds` | `60` | Flush interval (floored to 1). |
| `retention_days` | `0` | Age pruning is disabled by default to preserve pre-existing history on upgrade. Set a positive day window to opt in (rec #14). |
| `max_total_mb` | `0` | Size pruning is disabled by default to preserve pre-existing history on upgrade. Set a positive opportunistic directory budget to opt in; protected active writers can temporarily exceed it (rec #14). |
| `otlp_endpoint` | `""` | Opt-in OTLP/HTTP metrics endpoint (e.g. `http://localhost:4318/v1/metrics`). **Empty = no network egress (default).** When set, aggregated metrics are ALSO pushed to this collector in addition to the local JSONL sink; requires `pip install "kirocrew[otlp]"` (rec #1). |

Field validation (`TelemetryConfig.__post_init__`): `export_interval_seconds`
below 1 is floored to 1; negative `retention_days` / `max_total_mb` are clamped
to `0` (cap disabled) rather than being interpreted as "prune everything".

## Opt-in, retention bounds & egress (rec #14 / rec #1)

**Default posture — nothing collected, nothing leaves the host.**
`telemetry.enabled` defaults `false`, so every metric call site is a cheap no-op
and no file is written. Even once local collection is enabled, `otlp_endpoint`
defaults empty, so **no data ever leaves the machine unless the operator
explicitly sets an OTLP endpoint.**

**Easy opt-in (two equivalent ways):**
- **Config flag:** set `"telemetry": {"enabled": true}` in `~/.kiro/crew/config.json`.
- **Env var:** export `KIROCREW_TELEMETRY=1` (also accepts `true`/`yes`/`on`;
  `0`/`false`/`no`/`off` force-disables). The env var overrides the config flag
  and is handy for CI / containers / one-off debugging. It gates **local
  collection only** — it never enables network egress. Resolved by
  `provider._consent_enabled()`.

**External OTLP egress (opt-in, off by default):** setting `otlp_endpoint` adds a
second `PeriodicExportingMetricReader` alongside the local JSONL sink
(`provider._build_otlp_reader`). Install support with
`pip install "kirocrew[otlp]"`. If the endpoint is set but the package extra is
not installed, telemetry
degrades to local-only with a warning instead of crashing. The OTLP exporter
only ever sees the same redacted, low-cardinality data points as the local sink
(the `MetricsRecorder` facade sanitises attributes before they reach ANY
reader), so opting in cannot leak prompts, content, tokens, paths, user ids, or
secrets.

**Bounded local retention (rec #14, explicit opt-in):** both destructive caps
default to `0`, so upgrading cannot delete existing telemetry history. Operators
can opt in independently to age and/or size bounds:
- *Age cap* — set `retention_days` to a positive window (for example `7`); shards
  whose mtime is older than that window are eligible for deletion.
- *Total-size cap* — set `max_total_mb` to a positive budget (for example `128`);
  before an append would
  exceed the live-shard budget, the exporter rotates that shard and opens a
  fresh canonical writer. Closed shards are then deleted oldest-first until the
  combined size is under budget. The active writer is retained; with
  multiple process-local writers, enforcement remains opportunistic rather than
  a strict instantaneous directory-wide byte ceiling. In the worst case, live
  protected shards can temporarily approach the number of active writers times
  `max_total_mb` before those writers rotate and closed shards become eligible
  for oldest-first deletion.
- *Both caps are independently opt-in* and can be disabled again by setting
  the value to `0`.
- After an operator enables a cap, before the first destructive plan in each
  exporter process, retention emits
  one fixed, path-free warning and defers deletion for a full 300-second prune
  interval. Operators can set either cap to `0` during that window. The notice
  is process-local; there is no persistent migration marker or format to carry
  into future releases.
- Per-PID append + rotation are lock-free. A private cross-process
  `.metrics.lock` serializes only prune sweeps; contention skips pruning but
  never discards the DELTA payload already appended. Canonical shards owned by
  live PIDs or modified within the 300-second safety window are not deleted.
- Pruning is throttled (≤ once per 300s), runs only AFTER a successful append,
  and considers only regular files matching the exact generated grammar
  `metrics-YYYY-MM-DD-PID[-ROTATION_NS].jsonl`; broad-prefix lookalikes, invalid
  dates, symlinks, and the lock sidecar are excluded. It is fully best-effort —
  a rotation/prune failure is logged and swallowed, never breaking export.

**Never recorded:** prompts, message/tool content, token counts, filesystem
paths, user ids, and secrets. `telemetry.otlp_endpoint` is schema-sensitive so
credential-bearing collector URLs are masked by config API/UI consumers as well
as omitted from logs. Enforced structurally at the `MetricsRecorder`
facade via the `schema.py` guardrails (see below) — call sites emit only
low-cardinality enum-like attribute values, and any string that looks like a
credential/PII is redacted to `"[REDACTED]"` before it reaches an instrument.

Tests: `test/metrics/test_local_exporter.py` (retention: direct age cap,
oldest-first size cap, live-writer protection, live-shard rotation, non-blocking
prune lock, append survives prune contention, both-disabled,
broad-prefix/malformed shard lookalikes ignored, export-then-prune never raises),
`test/metrics/test_provider.py` (default-off, env-var opt-in/opt-out,
OTLP `None` by default = no egress, OTLP reader built when endpoint set, degrade
when extra missing), `test/metrics/test_schema.py` (redaction / namespace).

## Instrumented signals

| Metric | Type | Attrs | Site |
|--------|------|-------|------|
| `kirocrew.session.startup.duration` | histogram (ms) | `outcome` (`ready` / `auth_required` / `error`), `spawned` (bool) | `acp/client.py::AcpClient.ensure_ready()` — times cold-start (spawn + session init) and emits in a `finally` so every exit path is measured. The warm fast-path is NOT measured. `outcome` defaults to `"error"` so an unexpected exception is never mislabeled `"ready"`. |
| `kirocrew.turn.duration` | histogram (ms) | `outcome` (`ok` / `timeout` / `error`), `session_source` (via `validation.infer_use_case`) | `dashboard/chat_runner.py::_emit_turn_metric`, called at EVENT_COMPLETE after `persist_token_record_async`. `_turn_outcome` maps stop_reason (`""`/`end_turn`/`stop`/`completed` → ok). One histogram powers turn latency p50/p90 AND fault rate. |
| `kirocrew.mcp.backend.acquire.duration` | histogram (ms) | `warm` (bool — `not was_spawned`) | `mcp_gateway/gatewayd.py::_emit_backend_acquire_metric` — ensure_backend pre-flight + lazy-spawn paths; acquire-only duration captured before attach_stub/create_task overhead. |
| `kirocrew.mcp.lazy_load.count` / `.duration` | counter + histogram (ms) | `transport` (`stdio`) | `mcp_gateway/gatewayd.py::_emit_lazy_load_metrics` — legacy lazy-spawn path (also emits backend.acquire). |
| `kirocrew.mcp.warm_pool.acquire` | counter | `result` (`hit` / `miss`) | `mcp_gateway/prewarm.py::HotKeyStore.record_outcome` (emitted outside the lock). |
| `kirocrew.skill.lazy_load.count` / `.duration` | counter + histogram (ms) | `hit` (bool) | `skills.py::SkillsLoader.load_skill` via `_emit_lazy_load_metric` (best-effort; never breaks skill loading). |
| `kirocrew.gateway.boot.duration` | histogram (ms) | `server` (`dashboard` / `api`), `outcome` (`ready`) | `dashboard/server.py::start_dashboard` / `start_api_server` — boot-to-ready: wall-clock from the server's `start_time` until full init completes and it is about to accept traffic. Emitted via `metrics/http_metrics.py::record_boot_to_ready`. Best-effort; never blocks startup. |
| `kirocrew.gateway.request.duration` | histogram (ms) | `method` (fixed HTTP-verb allowlist, else `OTHER`), `route_template` (matched aiohttp canonical TEMPLATE, e.g. `/api/artifacts/{slug}`, else `__unknown__`), `status_class` (`1xx`..`5xx` / `other`) | `metrics/http_metrics.py::make_route_latency_middleware` — outermost gateway middleware on BOTH `start_dashboard` and `start_api_server`. Times full in-gateway HTTP handling; upgraded WebSocket connections and `text/event-stream` SSE responses are excluded so connection/turn lifetime cannot pollute request latency. **Bounded cardinality** (see below). |

### Bounded cardinality of `kirocrew.gateway.request.duration` (rec #1)

The per-route latency label `route_template` is **never** the concrete request
path, query, id, or body — it is the aiohttp route TEMPLATE
(`/api/items/{item_id}`), whose `{…}` placeholders are constants baked into the
route table. The bounding is structural: `collect_route_templates(app)` snapshots
the finite set of registered templates once (lazily, on first request, after all
routes — including edition-contributed and post-middleware routes — are present),
and `route_template()` returns a value ONLY if it is a member of that frozen set;
anything else (an unmatched 404 aiohttp `SystemRoute`, or a template not in the
snapshot) collapses to the single sentinel `__unknown__`. Therefore the distinct
`route_template` label values are bounded by `len(known_templates) + 1`, a
constant fixed at startup that cannot grow with traffic. Combined with the fixed
`method` allowlist (≤ 8 values) and the fixed `status_class` domain (6 values),
total series are bounded by `(len(known_templates) + 1) × 8 × 6`. The test
`test/metrics/test_gateway_http_metrics.py::test_bounded_cardinality_under_many_distinct_ids`
proves this against real OTEL data points: 100 distinct ids yield exactly ONE
`route_template` value. **Privacy:** the only request-derived labels are
`method` / `route_template` / `status_class` — no prompt, content, token, path,
query, user id, or secret is ever recorded, and every string label still passes
the recorder's `redact()` guardrail.

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
