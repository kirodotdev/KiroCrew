# Metrics Telemetry Module

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
| `provider.py` | Consent gate + process-global recorder (`get_recorder()`) + graceful `shutdown()` / `reset_for_testing()`. When enabled, wires a `PeriodicExportingMetricReader` to the local JSONL exporter. Installs **one `View` per instrument** from `_HISTOGRAM_BUCKETS_MS`, each with its own `ExplicitBucketHistogramAggregation` boundaries (see below) — deliberately NOT a catch-all `instrument_type=Histogram` View. |
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
sees the same data points as the local sink: the `MetricsRecorder` facade
sanitises attributes before they reach ANY reader, and call sites are required
to pass low-cardinality constants rather than prompts, content, tokens, paths or
user ids. That sanitisation is defence in depth over the requirement, not a
substitute for it, so egress is only as safe as the call sites feeding it.

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

**Must never be recorded:** prompts, message/tool content, token counts,
filesystem paths, user ids, and secrets. `telemetry.otlp_endpoint` is
schema-sensitive so credential-bearing collector URLs are masked by config
API/UI consumers as well as omitted from logs.

That is a contract on call sites — they emit only low-cardinality enum-like
attribute values — backed at the `MetricsRecorder` facade by the `schema.py`
guardrails (see below), which redact a string matching a known credential shape,
or clearing the entropy backstop, to `"[REDACTED]"` before it reaches an
instrument. Namespace validation is exhaustive; attribute redaction is defence
in depth with a bounded reach (`schema.py` documents where the entropy backstop
can and cannot fire), so it narrows the blast radius of a call site that breaks
the contract rather than making one impossible.

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
| `kirocrew.session.startup.duration` | histogram (ms) | `outcome` (`ready` / `auth_required` / `error`), `spawned` (bool), `backend` (`kiro`) + `phase` (`total` / `spawn_init` / `session_new` / `session_load` / `set_model`), `channel` (conversation source), `resumed` (bool) on the kiro path | Two sites. **claude**: `acp/client.py::AcpClient.ensure_ready()` — times cold-start (spawn + session init) and emits in a `finally` so every exit path is measured, with no `phase` attr. **kiro** (default): `providers/acp.py::_emit_kiro_startup_metric` — one `phase=total` point PLUS one point per internal phase; `spawned` is unconditionally `True` because `_start_kiro_runtime_impl` always spawns a fresh runtime (the warm fast-path returns before reaching either site and is NOT measured). `outcome` defaults to `"error"` so an unexpected exception is never mislabeled `"ready"`. Consumers MUST treat only the end-to-end point (`phase` absent or `total`) as a startup — the phase points are components of one startup. `channel` comes from `messaging.link::telemetry_channel_of`, a closed label set (an unrecognised key classifies as `other`, never the key itself) answering WHICH surface paid the cost; `resumed` separates the `session/load` path from `session/new`. `session_load` is recorded only when a resume was attempted, and `session_new` only when `create_session` actually ran, so a resumed startup never reports a near-zero `session_new`. |
| `kirocrew.session.pool.decision` | counter | `outcome` (`hit` / `miss_empty` / `bypass_resume` / `bypass_stateless` / `bypass_cwd` / `bypass_env` / `disabled` / `other`), `channel` | `session.py::SessionManager._record_pool_decision`, one point per `get_or_create` warm-pool decision. Exactly one reason is reported per decision — the disqualifiers form a disjunction, so branch order picks the reported reason, not the outcome. Deliberately a counter rather than an attribute on the startup histogram: "was the pool used" and "how long did startup take" are separate questions, and crossing them would multiply every phase series. `bypass_resume` quantifies how often a `resume_sid` disqualifies a session from the pool. Values are pinned by `session.POOL_DECISIONS`. |
| `kirocrew.session.resume.outcome` | counter | `outcome` (`loaded` / `fallback_replay` / `no_session_file`), `channel` | `providers/acp.py::_emit_kiro_startup_metric`, emitted only when a resume was attempted. Distinguishes a lossless native resume from the degraded fallback (fresh `session/new` plus history replay on the Kiro Crew side, taken when `session/load` exhausts `_RESUME_MAX_ATTEMPTS` against a stale lock) and from a resume skipped because the session file was gone. |
| `kirocrew.turn.duration` | histogram (ms) | `outcome` (`ok` / `timeout` / `error`), `session_source` (via `validation.infer_use_case`) | `dashboard/chat_runner.py::_emit_turn_metric`, called at EVENT_COMPLETE after `persist_token_record_async`. `_turn_outcome` maps stop_reason (`""`/`end_turn`/`stop`/`completed` → ok). One histogram powers turn latency p50/p90 AND fault rate. The value is `duration_ms or elapsed_ms`: the acp provider always reports `TurnUsage.duration_ms == 0` (only claude_code fills it), so the caller must pass the locally measured wall clock as `elapsed_ms` or nothing is ever emitted. A still-zero value skips the emit deliberately — absence renders as "no data", whereas a recorded 0 would render as a plausible 0ms p50. **What it measures:** the wall clock starts at turn start, so a turn parked on an interactive tool-approval prompt counts operator thinking time. No finer-grained source exists on the acp path, so this is "turn wall-clock", not pure model latency — a high p90 can mean slow approvals rather than a slow model. |
| `kirocrew.mcp.backend.acquire.duration` | histogram (ms) | `warm` (bool — `not was_spawned`) | `mcp_gateway/gatewayd.py::_emit_backend_acquire_metric` — ensure_backend pre-flight + lazy-spawn paths; acquire-only duration captured before attach_stub/create_task overhead. |
| `kirocrew.mcp.lazy_load.count` / `.duration` | counter + histogram (ms) | `transport` (`stdio`) | `mcp_gateway/gatewayd.py::_emit_lazy_load_metrics` — legacy lazy-spawn path (also emits backend.acquire). |
| `kirocrew.mcp.warm_pool.acquire` | counter | `result` (`hit` / `miss`) | `mcp_gateway/prewarm.py::HotKeyStore.record_outcome` (emitted outside the lock). |
| `kirocrew.skill.lazy_load.count` / `.duration` | counter + histogram (ms) | `hit` (bool) | `skills.py::SkillsLoader.load_skill` via `_emit_lazy_load_metric` (best-effort; never breaks skill loading). |
| `kirocrew.gateway.boot.duration` | histogram (ms) | `server` (`dashboard` / `api`), `outcome` (`ready`) | `dashboard/server.py::start_dashboard` / `start_api_server` — boot-to-ready: wall-clock from the server's `start_time` until full init completes and it is about to accept traffic. Emitted via `metrics/http_metrics.py::record_boot_to_ready`. Best-effort; never blocks startup. |
| `kirocrew.gateway.request.duration` | histogram (ms) | `method` (fixed HTTP-verb allowlist, else `OTHER`), `route_template` (matched aiohttp canonical TEMPLATE, e.g. `/api/artifacts/{slug}`, else `__unknown__`), `status_class` (`1xx`..`5xx` / `other`) | `metrics/http_metrics.py::make_route_latency_middleware` — outermost gateway middleware on BOTH `start_dashboard` and `start_api_server`. Times full in-gateway HTTP handling; upgraded WebSocket connections and `text/event-stream` SSE responses are excluded so connection/turn lifetime cannot pollute request latency. **Bounded cardinality** (see below). |

### Histogram bucket boundaries: per instrument, not shared

Boundaries live in `metrics/provider.py::_HISTOGRAM_BUCKETS_MS`, a metric-name →
boundaries map from which one `View(instrument_name=…)` is built per instrument.
Three families, each sized to its instrument's measured range:

| Family | Range | Instruments |
|---|---|---|
| `_FAST_BUCKETS_MS` | 0.5ms – 60s | `gateway.request`, `mcp.backend.acquire`, `skill.lazy_load` |
| `_STARTUP_BUCKETS_MS` | 1ms – 60s | `session.startup`, `mcp.lazy_load`, `gateway.boot` |
| `_TURN_BUCKETS_MS` | 1s – 1h | `turn.duration` |

**Why not one shared array.** A single 1ms–60s array previously served every
histogram through a catch-all `View(instrument_type=Histogram)`. Its ceiling was
sized for session startup, so the first `kirocrew.turn.duration` sample ever
recorded (227589ms — an agent turn is a whole agent loop including tool
round-trips and any wait on an interactive approval) landed in the `+Inf`
overflow bucket. Since `_pct_from_buckets` can only report an overflow bucket's
LOWER bound, the aggregator returned `p50 == p90 == 60000` — a ceiling artifact
presented as a real latency, while `mean`/`max` (exact, from `sum`/`max`) stayed
correct beside it. `p50 == p90` is the signature of this failure. Instruments in
this system span six orders of magnitude (sub-ms pooled acquires to multi-minute
agent turns), so one array must sacrifice either fast-end resolution or slow-end
truth.

**Why there is no catch-all fallback.** The OTEL SDK applies EVERY matching
View, not the first. A per-instrument View plus a catch-all therefore publishes
the same metric name twice with different bounds. The SDK offers no negation in
View matching, so the catch-all had to be removed rather than narrowed.

**Boundary generations never merge.** Bounds are baked into each data point at
record time, so any boundary change makes the 14-day scan window straddle two
incompatible generations of one metric. `handlers/telemetry.py::_Hist` therefore
groups data points by their EXACT `explicit_bounds` and reports statistics from a
single group — **the one holding the newest data point**. Selection is by recency,
not volume: majority selection would let a stale generation keep winning while it
out-counted the new one, so right after a boundary change the old bounds would be
reported for up to the whole 14-day window — for `turn.duration` that means
continuing to serve the ceiling-pinned percentiles this grouping exists to remove,
while omitting the new samples. Recency makes the change take effect on the first
post-change sample; the reported population is then small but truthful, and
`count` says so. (`count` remains a tie-break for data points carrying no
`time_unix_nano`.) **Outcome tallies
are grouped too** — accumulated inside `_Hist` rather than alongside it, because
scoping only the buckets and count would leave the outcome breakdown summing
across generations: the page would show N turns beside an outcome bar totalling
more than N, and a `fault_rate` computed over a different population than the
latency next to it. `other_generations` (0 = a clean window; >0 = the window
straddles a boundary change and only the dominant generation is reported) and
`total_count` (samples across EVERY generation) are therefore returned by
`_Hist.stats()` itself, so they travel with every set of numbers they qualify —
the `startup` blocks, the `turn` block, each `other` histogram, and each
per-attribute split.

The dashboard renders the PAIR, not the generation count: it shows
"showing 1,134 of 2,926 samples" beside the affected figure. A generation count
is an internal unit a reader cannot convert into missing data, so "1 older
generation" left a truncated `n=1134` unreconcilable against the `2837 hit`
counter next to it, while the shown/total pair is directly comparable to both.
`other_generations` remains the structural fact (how many incompatible groups the
window holds) and stays in the response for diagnostics.

They are deliberately NOT pasted on by the response builder per block. That is
how it shipped, and the generic `other` instruments were never given the field:
the MCP acquire card reported one generation's `n` (1,154 of 2,926 real samples)
beside a full-window counter, with nothing anywhere saying a generation had been
dropped. A statistic and the caveat that makes it readable are one value, not
two.

This is load-bearing, not defensive: the historical shared array and
`_TURN_BUCKETS_MS` have the SAME bucket-count length, so a length-only check
would have merged them positionally — adding a pre-change sample from the old
`+Inf` bucket into the new `+Inf` bucket and reporting **p90 = 3,600,000ms (one
hour)**, while a 5s sample landed in a 5-minute bucket. Grouping also keeps
`count`/`sum`/`min`/`max` consistent with the percentiles; accumulating those
across generations while only one generation's buckets survived would describe a
mean over one population and percentiles over another.

**Completeness is therefore load-bearing.** With no catch-all, a histogram
missing from the map silently falls back to OTEL's default 10s-ceiling
boundaries — reintroducing the same class of bug. `test/metrics/
test_provider_bucket_views.py` scans the source for `kirocrew.*.duration` metric
names and fails when one has no map entry (and when a map entry has no emitting
call site). It also pins the no-duplicate-streams property and asserts the
227589ms regression sample no longer overflows. **When adding a duration
histogram, add it to `_HISTOGRAM_BUCKETS_MS`.**

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

**`other` histogram splits (`_OTHER_SPLIT_ATTRS`).** An `other` histogram also
carries a `splits` map (`"attr=value"` -> the same stats shape) for a NAMED set
of low-cardinality attributes — currently `warm` only. This exists so one side of
a split can be reported alone: the dashboard's cold-spawn figure is
`acquire.splits["warm=false"]`. Splitting on every attribute present was
rejected because `gateway.request.duration` carries method+route, which would
grow one sub-histogram per endpoint and force an arbitrary truncation cap on the
payload; a named boolean keeps the split two entries wide with no cap.

Note that `kirocrew.mcp.lazy_load.*` is NOT the cold-spawn signal even though its
name suggests it. It is emitted only from the legacy pre-`ensure_backend` spawn
path, which modern stubs never take, so it records nothing on a current
deployment (0 data points across 47 shards / 12 days observed) while real cold
spawns are recorded on the acquire histogram under `warm=false`. The instrument
stays because that legacy path can still execute for an old stub; the dashboard
does not read it.

**Startup phase gating.** Only the end-to-end startup point (`phase` absent, as
on the claude path, or `phase=total` from the kiro path) feeds the startup
totals — count, cold/warm split, outcome, daily series, and the bucket
distribution. Per-phase points are aggregated separately into
`startup.phases[]` (`{name, count, p50_ms, p90_ms, …}`). Counting them as
startups multiplies the startup count by the number of phases and sums several
unrelated latency distributions into one set of buckets, which renders as a
spurious multi-modal "distribution".

**Failures are reported as counts, not as success rates.** Both outcome
instruments are fail-closed — `AcpClient.ensure_ready` and
`AcpSessionProvider._start_kiro_runtime` each default their outcome to `error`
and overwrite it with `ready` only on the success path, and `_turn_outcome` maps
a real `stop_reason` — so a zero here is a measurement rather than an instrument
that cannot report bad news. What a *rate* over these populations cannot do is
survive rounding: a window of ~1400 startups makes one failed startup 99.93%,
which renders as a flawless `100%`. A rate had no reachable value between
"perfect" and a problem big enough to clear half a percent, and it saturated in
the direction that hides failure. A count has no such ceiling, so the page shows
the absolute number of non-`ready` startups and of non-`ok` turns. Where a rate
is still shown (turn fault rate, which answers a different question — faults per
unit of work), a non-zero value below the rounding threshold renders as `<1%`
and never in the success colour.

Both figures count *everything* outside the success value rather than naming the
failure values. Enumerating them (`error` + `timeout`) silently excluded any
third outcome — `auth_required`, and the `unknown` that shards predating the
attribute aggregate under — which put the displayed count and the rate beside it
on two different populations.

**`context` block.** The response also carries per-turn context-window
occupancy — `{turns, p50_pct, p90_pct, max_pct, sessions[]}` — sourced from the
per-turn token row store below, NOT from the OTEL shards: occupancy is a
per-session ratio and slot keys are unbounded-cardinality, which must not become
a metric label. `sessions[]` is EVERY session in the window ordered by peak
occupancy (bounded only by a payload backstop of 500), each reporting peak
plus the LATEST turn's identity (agent/model/surface) and absolute
used/window. Rows whose window is missing or zero are skipped rather than
defaulted. The block is `null` when no row carries the fields, and is served
independently of the `telemetry.enabled` switch because those rows are always
written (the panel therefore renders it even with OTEL export off).

## Per-turn token usage row store

Separate from the OTEL histogram sink above (`~/.kiro/crew/metrics/`, DELTA
histograms for trends/alerting), the gateway also keeps a **per-turn row store**
for cost and context analytics: one JSON object per model-spending turn appended
to `<data home>/usage/tokens/YYYY-MM-DD.jsonl` (shards partitioned by the user's
local date). It is always on (not gated by `telemetry.enabled`) and never
egresses the host. Written by `dashboard/handlers/usage.py::persist_token_record`
(sync) / `persist_token_record_async` (the chat hot path — builds the record
on-loop, offloads the append via `asyncio.to_thread`); both are best-effort
(exceptions swallowed, no fsync). Aggregated for the dashboard by
`_parse_token_history` (30-day window, shard-fingerprint + 120s-TTL cache).

Each row (`_build_token_record`) carries:

| field | type | meaning |
|-------|------|---------|
| `_type` | str | always `"tokens"` (record discriminator) |
| `ts` | str | ISO-8601 local timestamp of the turn |
| `slot` | str | chat slot / session key |
| `provider` | str | LLM backend (`acp` / `claude_code` / `bedrock` / …), `""` if unknown |
| `model` | str | resolved model id for the turn; `"auto"` when the completed turn only exposes the Auto request; `""` if no model source is available |
| `input` / `output` | int | prompt / completion tokens (structurally `0` on the ACP backend — kiro-cli bills credits only) |
| `cache_create` / `cache_read` | int | cache-write / cache-read tokens |
| `cost` | float | provider-reported USD cost (`0.0` on ACP) |
| `credits` | float | kiro-cli per-turn credit spend (float-coerced) |
| `turns` | int | provider `num_turns` |
| `duration_ms` | int | provider-reported turn duration (`0` on ACP) |
| `surface` | str | **(#647)** dispatch origin — `dashboard`, `cron`, `subagent`, `monitor`, `heartbeat`, `webhook`, `task_runner`, `workflow`; `""` if unset |
| `agent` | str | **(#647)** agent id resolved for the turn; `""` if unset |
| `context_used` | int | **(#647)** context-window tokens occupied after the turn (int-coerced) |
| `context_window` | int | **(#647)** served context-window size in tokens (int-coerced) |
| `ctx_blocks` | dict[str,int] | per-turn injection breakdown: context block label → **characters** (never tokens); non-positive / non-numeric sizes dropped; `{}` when the turn injected nothing |
| `phase` | str | `session_start` (the first turn's one-off injection) vs `per_turn` (every later turn); `""` if unset |

The `surface` / `agent` / `context_used` / `context_window` fields (all #647) and
the later `ctx_blocks` / `phase` pair are all **additive** — every field defaults
(`""` / `{}` / `0`) so existing callers stay valid and shards predating a field
(which lack its key) remain parseable; readers must tolerate their absence.
`context_used` / `context_window`
are read from the provider at the persist call site via
`usage.read_context_tokens(source)`, which calls the provider's public
`context_used_tokens()` / `context_window_tokens()` accessors
(`providers/base.py`, implemented for ACP in `providers/acp.py` +
`acp/session_provider.py`) behind `getattr` guards and returns `(0, 0)` on any
missing accessor or exception — so non-ACP providers and test doubles record
zeros and the analytics helper never breaks the turn it measures. `surface`
lets background turn-dispatch surfaces (cron/subagent/monitor/heartbeat/webhook/
task_runner/workflow) attribute their spend; zero-token surfaces (cron
`script=`/`command=` modes, heartbeat maintenance ticks) never call a model and
must not write a row.

**Per-turn injection breakdown (`ctx_blocks` / `phase`).** `ctx_blocks` is
produced by `context_blocks.split_blocks(prompt, user_chars=…)`, which attributes
the FINAL assembled prompt to the blocks that produced it by matching the bracket
markers the assembly emits (`[CRITICAL RULES`, `[Memory`, `[Skills:]`,
`[USER PROFILE]`, `[UI LANGUAGE]`, `[CURRENT USER REQUEST`, the trailing
reply-format contract, …) rather than counting at each of the ~30 append sites.
Reading the OUTPUT means the attribution cannot drift from what was actually
sent. `_MARKERS` is deliberately kept in sync with EVERY opener the assembly can
emit — including the identity/session banners (`[USER PROFILE]`, `[UI LANGUAGE]`,
`[CHANNEL]`, `[INCOGNITO SESSION]`, `[TEMPORARY SESSION]`, the cancelled-turn
preamble) and the openers added AFTER `build_message` returns (`[THEME PERSONA]`,
the re-injected `[Previous chat history for this tab …]`, `[Hook context]` — in
both emitted spellings, with and without the colon — and the `[System: …]`
regenerate line): a marker absent from `_MARKERS` does NOT surface as its own
bucket, it
folds into the PRECEDING recognised block and mislabels those bytes, so the set
must stay complete. Only leading bytes before the first marker fall outside every
block and surface as `unclassified`. The returned sizes sum EXACTLY to `len(prompt)` (closure); the user's
own text is carved into the `your_message` label using the exact `(start, end)`
span that `build_message` reports through its `user_span_out` out-parameter. That
span is authoritative because `build_message` is the only code that sees every
transform applied to the turn: a `HOOK_MODIFY` transform hook can REWRITE it
wholesale, marker neutralization rewrites forgeable boundary markers (changing the
length of anything ahead of the user's text), and the return path folds
`_MULTIBYTE_TABLE` punctuation over the whole message (`—` → `--`, `…` → `...`).
A caller measuring the pre-transform message cannot know the post-transform
offsets, so it passes the bounds of the user's text *within the text it hands
over* and `build_message` maps them forward. When a transform hook has replaced
the turn, the caller's bounds describe text that no longer exists, so the hook's
output is attributed to `your_message` in full — it IS the user's turn at that
point. Steps that PREPEND to the finished prompt after `build_message` returns
(the incognito/temporary notice, re-injected history, hook context, the
regenerate system line) slide that span, so the caller re-derives it once from the
length delta and **verifies the shifted slice still holds the same text**; if it
does not, the span is dropped and the reconstruction below is used rather than
persisting a wrong attribution. The legacy `user_chars` / `user_offset` reconstruction stays as the
fallback for callers that do not assemble through `build_message`.
Sizes are **characters, not tokens** — characters are exact, free
and tokenizer-independent, whereas the only tokenizer available here is OpenAI's
BPE, which would add a systematic unknown error against a Claude backend, and
comparing one turn against another (the whole point of the breakdown) wants an
exact unit rather than an approximate one. `phase` separates the one-off
session-start injection from the much smaller per-turn one so a reader never
pools the two populations.
**Non-finite floats are rejected at the single write chokepoint.**
`_write_token_record` dumps with `allow_nan=False` and, only on the resulting
`ValueError`, rewrites non-finite floats to `0.0` — so the common path pays no
per-turn scan. The guard lives there rather than per field because provider
floats (`cost_usd`, `credits`) reach the record unvalidated and a float is
exactly what a bad one looks like, so a type check cannot catch it. Without it,
`json.dumps` writes the bare tokens `NaN` / `Infinity`, which are **not** valid
JSON while `json.loads` still accepts them: one such row travels silently into a
`web.json_response` body no browser can parse, taking down every panel that
reads the store instead of losing one turn's numbers. `cost_breakdown`
additionally skips non-finite rows on read, because shards written before this
guard can already contain them.

**Subagent turns are excluded from both readers.** `usage.is_session_slot(slot)`
drops any row whose `telemetry_channel_of` is `subagent`, at the point the row is
READ rather than where it is grouped — so the window totals, the prior-period
deltas, `by_model`, `by_channel`, `by_category`, the context bands, the priciest
turn and the occupancy percentiles are all computed over one population and cannot
contradict each other. A subagent is a fragment of another session's turn rather
than a session with its own lifecycle, and its usage row carries no field pointing
back at the session that spawned it, so it can be neither listed nor attributed.
The consequence is deliberate and worth stating: these totals are the totals of the
sessions the panel lists, NOT of the account.

**Read side.** `usage.context_occupancy(days)` aggregates these rows into
per-turn occupancy percentiles plus a per-session peak ranking (own
shard-fingerprint + 30s-TTL cache, same contract as `_parse_token_history`), and
`handlers/telemetry.py` serves it as the `context` block of
`GET /api/telemetry/startup` (a plain module-scope import — `handlers.usage`
imports nothing from `dashboard.handlers`, so there is no cycle to dodge).
`usage.context_trace(slot, days)` is the per-session drill-down: it returns each
turn's `ctx_blocks` in chronological order plus per-block `totals`,
`injected_chars`, `user_chars` (the `your_message` label) and
`estimated_other_chars` — the un-instrumented remainder of the window: kiro-cli's
own base prompt + tool catalogue + steering AND the conversation transcript and
tool output accumulated over the session (occupancy is cumulative, `injected` is
only this turn's injection). It is expressed in characters via
`_EST_CHARS_PER_TOKEN` (≈4) and clamped to `0` when occupancy is unknown or the
subtraction would go negative. Because it mixes fixed kiro overhead with the
growing conversation it is surfaced as **"Not measured"** (never "Kiro built-in")
and always tagged an estimate — it is not a claim that the bytes are Kiro's or
unremovable. Rows
predating the field carry no `ctx_blocks` and are skipped, not zero-filled, so
the trace starts where the recording does. `handlers/telemetry.py::api_context_trace`
serves it as `GET /api/telemetry/context-trace?slot=<session key>` (`400` when
`slot` is missing or blank), independent of the `telemetry.enabled` switch since
these rows are always written.

**Where it renders.** The breakdown is a **per-session side-panel tab**
(`ViewKind` `context`, opened from the panel's `+` menu directly under Logs), not
a section of the global Telemetry page. It is a **developer surface**: the `+`
menu offers it only when Developer Mode is on (Settings > Developer, the
`mc-dev-mode` consent gate the standalone Developer page also uses), so ordinary
users never see it. The tab is scoped to the chat slot it was
opened from, which is why it carries no session picker: a per-turn drill-down that
first asks the reader to choose a session cannot say anything until they do, and
Logs — the other "what actually happened in THIS session" view — already sets the
precedent. It refetches on an interval because the trace gains a row per turn, so
a tab left open would otherwise go stale. The Telemetry page keeps the aggregate
`context` occupancy block only.

Both readers stay OUT of the OTEL metric pipeline for the same reason: occupancy
and the per-turn breakdown are per-session, per-turn detail, and slot keys are
unbounded-cardinality labels that must never become metric labels (the
`context_occupancy` docstring states the same rule). The bounded half — block
label → size, aggregated — is what could belong on a metric; this per-session
half is the drill-down. Without these readers the fields were write-only:
recorded on every turn, read by nothing.

`usage.cost_breakdown(days)` is the second reader, same cache contract, serving
the `cost` block of the same endpoint. It answers "where did the credits go"
from fields the row store already carries — no new instrumentation:

| sub-block | derivation |
|-----------|-----------|
| totals | `credits` summed over the window, plus the preceding window of equal length and the delta between them |
| `by_model` | per-`model` credits, share, credits-per-turn, and per-model delta vs the prior window. **Every model, never truncated** — a top-N cut hides exactly the cheap-model-creep this block exists to show |
| `by_channel` | same shape, keyed by `telemetry_channel_of(slot)` |
| `context_bands` | mean credits per turn bucketed by absolute `context_used`, which is what makes the cost/context relationship legible (a turn at 900k costs ~4.7x one at 100k) |
| `conversations` | EVERY session in the window (not a top-N), each with its `category`, the unollapsed `channel` beneath it, peak occupancy, span, per-turn growth rate, and a projected turns-to-compaction. Named `conversations` for payload compatibility; the entity is a session |
| `by_category` | same shape as `by_model`, keyed by `usage.session_category(slot)` — the taxonomy the panel groups by: `bg` for an unattended session (cron, heartbeat, task runner), otherwise the transport (`dashboard`, `telegram`, `slack`, …) |

**Channel comes from the slot key, not from `surface`.** `surface` cannot
separate transports: `chat_runner` stamps `surface="dashboard"` for every turn
that flows through it regardless of where the message arrived from, so a
Telegram turn is booked as dashboard spend (observable in the row store as a
`telegram_*` slot carrying `surface="dashboard"`). The slot key is assigned by
the transport that created the session and is therefore the only field that
distinguishes them.

**A conversation's `title` is attached by the endpoint, from two sources in
order.** `cost_breakdown` names nothing — slot keys are all the row store holds.
`handlers/telemetry._with_conversation_titles` resolves the live slot's
`display_title` first, so a rename shows before it has been flushed; a session
with no live slot falls back to the `title` on its transcript's metadata line,
read through `ConversationLog.get_metadata` off the event loop and keyed by
`slot_transcript_key(slot)`, which is what folds a channel-born slot onto the
channel's own transcript. Only an explicit metadata title counts: `list_sessions`
would answer with the first user message and then with the session key, which
turns a ranking label into prompt text and leaves no way to tell a named
conversation from an unnamed one. A row with neither source reports an absent
title rather than its key. Both sources pass the same two scanners on the way
out, so where a title came from cannot change what leaves the endpoint.

**The growth slope is fitted per segment, never across the window.** Occupancy
is a sawtooth: a compaction drops it back toward empty, and 7 of the 8
top-spending conversations measured carry one to four such drops. A secant from
the first turn to the last therefore crosses discontinuities and is dragged down
by every reset, understating the live rate by ~47x on real data — one 104-turn
conversation projected 795 turns of headroom while its current segment climbed
at 4%/turn, i.e. ~17 turns from compaction. Erring toward "plenty of room" is
the damaging direction for a figure whose only purpose is a warning, so the
slope is fitted on the stretch since the last fall larger than
`_COMPACTION_DROP_PCT` (sub-threshold drift is ordinary jitter in what counts
toward `context_used`, not a compaction). Growth and the projection are
**withheld** unless that CURRENT segment holds `_COST_MIN_GROWTH_TURNS` points —
a long conversation freshly past a compaction knows nothing about its new
trajectory, and withholding is the honest answer rather than extrapolating from
two or three points.

Tests: `test/test_usage.py` (`TestReadContextTokens`,
`TestBuildTokenRecordContextFields`, `TestBuildTokenRecordCtxBlocks`,
`TestPersistTokenRecord*`), `test/metrics/test_context_occupancy.py`
(aggregation, skips, latest-turn wins), `test/metrics/test_cost_breakdown.py`
(channel attribution incl. the bare dashboard slot form, no-truncation,
prior-window deltas, band bucketing, post-compaction slope, growth
withholding, non-finite rejection) and `test/metrics/test_telemetry_titles.py`
(title redaction + cache purity, and the closed-conversation fallback: the
canonical transcript key, live-wins-over-persisted, one read per shared
transcript, and no title where the metadata line names none), plus the
`context-trace` endpoint.

## Circular-import rule

`metrics/provider.py` imports `config.loader` at module top; call sites reached
from inside `config.loader`'s import chain (e.g. `acp/client.py`) MUST import
`get_recorder` lazily (inside the function) so the provider is never loaded
during that chain.

## Anonymous outbound telemetry (`beacon.py`, `apps/install_receipt.py`)

There are now **four independent** telemetry paths. Keep them straight:

| Path | Purpose | Data | Egress | Switch |
|------|---------|------|--------|--------|
| OTEL metrics (`metrics/`) | Ops observability | DELTA histograms / counters | **Never** (local JSONL; OTLP only if the operator sets an endpoint) | `telemetry.enabled` (**off**) |
| Token row store (`usage/tokens/`) | Cost + context analytics | One row per model-spending turn | **Never** | always on |
| **Beacon (`beacon.py`)** | **Product analytics** | One anonymous ping per install per day | **Yes — to the KiroCrew endpoint** | `telemetry.beacon_enabled` (**on**) |
| **Install receipt (`apps/install_receipt.py`)** | **Official app adoption** | One anonymous receipt after a successful official-catalog install/update | **Yes — to the same endpoint** | `telemetry.beacon_enabled` (**on**) |

`src/kiro_crew/beacon.py`, tests `test/test_beacon.py`. Fired from
`slack/gateway.py::run_gateway` on a **detached daemon thread** (never awaited —
a 5s blocking `urllib` call must not delay boot or pin interpreter exit), and
skipped entirely under `--test-mode` so the offline E2E gate cannot egress.

### Why it is NOT part of the OTEL trunk

Four independently disqualifying reasons — do **not** "consolidate" them later:

1. **The privacy guardrail eats the payload.** `MetricsRecorder._guard()` runs
   every attribute through `schema.redact()`. A 64-char sha256 install id
   becomes `"[REDACTED]"` (40+-hex rule), so DAU would silently compute as 1.
   Ids that merely *survive* redaction are no better: `schema.py` mandates
   low-cardinality enum-like values and the instrument cache never evicts, so a
   per-machine id is precisely the "cardinality bomb" that contract prevents.
2. **OTLP egress is an extra.** `opentelemetry-exporter-otlp-proto-http` ships
   in `kirocrew[otlp]`, not the default dependency set, so a beacon riding it
   would measure only users who installed an optional extra.
3. **`telemetry.enabled` is a published no-egress promise** (config help, this
   spec, the dashboard panel all say "nothing leaves this machine"). Hanging an
   outbound heartbeat off it would retroactively change what that switch means.
4. **Shape mismatch.** OTEL carries pre-aggregated data points; DAU needs one
   row per install per day deduped at query time. Aggregating away the id makes
   DAU uncomputable.

The one thing it *does* share is the atomic create-once file pattern from
`handlers_system.py::_get_telemetry_salt` (`os.link`, owner-only mode,
in-memory fallback).

### What `DailyActiveInstances` means (and does not)

The metric is **`DailyActiveInstances`** — deliberately not "DAU" and not
"Installations". The distinction matters because it is easy to misread the number:

- It measures **activity, not installs.** The `install_id` is a *persistent
  identity* generated once at first run, not a per-install event counter. A copy
  that runs on ten days contributes to ten daily buckets with the same id;
  `COUNT(DISTINCT install_id)` **per day** therefore answers "how many copies ran
  today". Install *volume* is a separate metric (`NewInstallations`, from the
  `first_seen` bit, plus model-CDN first-fetch).
- It is **not DAU**, because the denominator is not a person and cannot be. There
  is no account system — only a random per-data-home id — so one operator on
  three machines counts as **3**, and three people sharing one machine count as
  **1**. Calling it DAU would invite the reader to treat "14" as 14 people.
- The over-count is **larger here than for a typical CLI**: KiroCrew supports
  pods, worktree previews, and `KIROCREW_HOME` overrides, so one person on one
  machine easily has several data homes. Dev homes and CI are suppressed, but a
  user's own pods are not.

`Instance` is the honest middle: it names a *running thing* (not the act of
installing) without claiming to have resolved it to a human.

### The five fields (a fixed allowlist)

`payload()` is the only producer; there is no caller-supplied field, so the
shape cannot be widened from a call site.

| Field | Example | Why |
|-------|---------|-----|
| `id` | 32-char hex | Random UUID4. Dedup key for `COUNT(DISTINCT)`. |
| `v` | `0.1.2` | Version adoption. **Release only** — every build stamp is stripped by `release()`. |
| `py` | `3.12` | **Minor only.** Answers "when can the floor move off 3.10". |
| `dist` | `dmg` | Which install path users take. Clamped to a fixed set. Baked at build time (see below). |
| `first_seen` | `1`/`0` | One bit → new-install and "launched once, never again" rate. |

#### Four fields were REMOVED — do not re-add them

`chan` (release channel), `os`, `arch` and `gov` (governance posture) were part of
this payload and are gone. Each was individually low-cardinality and individually
defensible; the problem was **collective**. The install id is stable, so every
attribute on the request is correlated with every other, and channel + OS +
architecture + governance posture together partition the population far more
finely than any one of them suggests — a nightly, ARM, governed-and-verified
install is a small crowd to hide in even though no single field is identifying.

So the rule for this payload is not "is this value low-cardinality?" but **"how
much smaller does this make the crowd this install hides in?"** For all four the
answer was "too much for what it bought":

- **`chan`** — a nightly install is a small population by definition, which is
  exactly what makes it identifying when paired with a stable id. **This one is a
  real capability loss, not a relocation:** `release()` strips the prerelease label,
  so a nightly `0.1.2-nightly.<stamp>` and a stable `0.1.2` both send `v=0.1.2` and
  are indistinguishable on the wire. Channel-split adoption is no longer answerable
  from the beacon. It remains observable from the release feeds / CDN fetch counts
  (`feed/<channel>/latest-cli.json`), which are per-artifact and carry no install
  id — a better source for that question anyway.
- **`os` / `arch`** — the platform mix is answerable from download/CDN telemetry,
  which is per-artifact and carries no install id at all: a strictly better source
  for the same question.
- **`gov`** — governance adoption is an enterprise-fleet question, and a fleet
  operator knows their own posture. Correlating it with a stable id bought a
  number nobody was blocked on.

The client is authoritative: `beacon._fields()` returns exactly four keys (plus
`id`) and `test/test_beacon.py` asserts the key set, so a re-addition fails a test
rather than shipping quietly. `website/src/test/PrivacyPanel.test.tsx` asserts the
four names are **absent** from the user-facing disclosure for the same reason.

**Historical rows keep the old params.** Log lines already in S3 carry
`chan`/`os`/`arch`/`gov` forever, and un-upgraded clients keep sending them until
they update. That is harmless — the aggregator simply no longer reads them, so the
fields age out of the analysis without a migration.

#### Why `dist` is baked into the artifact

`dist` answers "which install path do users actually take", so it has to describe
the **artifact**, not the environment the artifact happens to run in.

Resolution order is **baked module → env var → `"source"`**:

1. `kiro_crew/_build_info.py`, generated by `scripts/stamp-distribution.sh` and
   written by each packaging path. Authoritative because it ships inside the
   artifact and a running install cannot change it. Imported once at module
   import into `beacon._BAKED_DISTRIBUTION` (an optional-dependency
   `try/except ImportError`, since the module exists only in a packaged
   artifact); that binding is also the seam tests patch, because writing a real
   file into the installed package is process-wide shared state that races under
   the default `-n auto`.
2. `KIROCREW_DISTRIBUTION`, kept as a build/test override.
3. `DEFAULT_DISTRIBUTION` (`"source"`), the correct answer for a git checkout,
   where the module is absent (and gitignored, so it is never committed).

The env var is deliberately the *weaker* source. It is inherited by every child
process and settable by anyone with a shell, so a stray export in a profile would
relabel that host's daily count. An unknown baked value falls through to the env
var rather than going on the wire, so a bad stamp can never emit an unclamped
value.

| Path | Where it stamps | Value |
|---|---|---|
| Wheel | `.github/workflows/build-wheel.yml`, before `python -m build` | `wheel` |
| macOS desktop | `packaging/build-desktop.sh` → `build_backend` | `dmg` |
| Linux desktop | `packaging/build-desktop.sh` → `build_backend` | `appimage` |
| Container | `docker/Dockerfile`, after the wheel install | `docker` |
| Git checkout | nothing | `source` |

Two non-obvious constraints:

- The desktop stamp writes into the **installed** tree inside each backend
  bundle, not the repo. A universal macOS build produces two backends and both
  must carry it, and stamping `$ROOT` would leave the module in a developer's
  checkout.
- The container **re-stamps after installing the wheel**. The wheel it installs
  is already stamped `wheel`, so without the overwrite every container reports
  the wheel channel. It overwrites the module rather than setting the env var,
  because the baked value outranks the env var by design.
- The value is derived from the electron-builder **target**, not the host OS
  (`mac.target: dmg`, `linux.target: AppImage`). A Linux host also builds wheels,
  so branching on the host would mislabel them. Windows ships an NSIS
  installer, which has no value in `KNOWN_DISTRIBUTIONS`, and reports `source`
  until one is added to both the frozenset and the stamping script.

The three tests that execute `stamp-distribution.sh` skip when bash cannot run
it, probed by actually invoking the script rather than by `shutil.which("bash")`:
Windows resolves that name to a WSL launcher stub which then fails and cannot
see a Windows path. `test_stamp_script_exists` is asserted unconditionally so a
deleted script still fails on a host without bash.

`test/test_beacon.py::TestDistributionStamp` pins the precedence, the clamp, the
gitignore entry, and that the script accepts every `KNOWN_DISTRIBUTIONS` value,
so adding a channel to the frozenset without teaching the script fails a test
instead of failing at release time.

#### Why `v` is clamped

`__version__` is **not** low-cardinality in the field. Dev and nightly builds
carry a per-build timestamp (`0.1.2-nightly.20260731t065756`,
`0.1.2.dev20260731065756`), so sending it raw both fingerprints and misleads:

* **It fragmented the one number the field exists to produce.** A real
  57-install `0.1.2` population reported as 35 plus a fringe of one-install
  series — which reads as adoption decay when nothing changed.
* **It was silently lossy.** The aggregator caps each breakdown at
  `BREAKDOWN_LIMIT` (25) per day. Past that, real low-install releases fall
  below the cut and vanish from **both** CloudWatch and the permanent rollup —
  and a long-tail release is exactly the one you most need to know is still
  running. The limit now queries `LIMIT+1` and **reports** truncation (log line
  plus a `truncated` key in the invocation result) instead of dropping quietly.
* **It was a fingerprint.** A near-unique per-build value combined with a stable
  install id picks out individual machines, which is precisely the correlated-
  attribute hazard the rest of the allowlist is designed to avoid.

The channel is **discarded**, not moved to a field of its own (see "Four fields
were REMOVED" above). `beacon.channel()` and its markers are deleted along with
it, so there is no dormant helper for a future call site to re-wire.

**Normalized on both sides.** `release()` clamps at the client, but a client only
stops sending raw stamps once it **upgrades**, and rows already in S3 keep them
forever. So the aggregator derives `v` from the raw `v` param in SQL
(`_VERSION_EXPR`) as well. That makes the metric correct for pre-clamp clients and
means re-aggregating a historical day yields the clean value. An unparseable stamp
becomes the single bounded bucket `unknown`, never a new metric.

**Anti-fingerprinting is a design constraint, not a side effect.** Every value
is a low-cardinality constant or coarse bucket, because the id is stable and
attributes on the same request are therefore correlated: enough precise
attributes (exact patch level, CPU count, RAM, timezone, uptime) would combine
into a quasi-unique identifier even though each is individually "anonymous".
Do not add precise numeric or high-cardinality fields.

**Never sent:** prompts, model output, file contents, paths, repo names,
credentials, hostname, username, IP. Notably it does **not** reuse
`handlers_system._get_owner_hash()` — that is `HMAC(salt, hostname + ":" +
username)`; it stays local-only, and sending it would change its character.

### Fail-open is the load-bearing contract

**No beacon failure may ever block, delay, or surface an error in a user action.**
A telemetry path that can fail a turn is worse than no telemetry, so this is
enforced structurally, not by care:

- **Boot is never delayed.** `run_gateway` starts `beacon.send` on a *detached
  daemon* thread and never joins or awaits it. Measured cost to the boot path
  with a beacon hanging 30s: **0.08 ms** (the thread spawn). `daemon=True` also
  means it cannot pin interpreter exit. `TestFailOpen` pins both, plus a source
  assertion against a refactor to `await asyncio.to_thread(...)`, which would
  silently reintroduce up to `HTTP_TIMEOUT_SECS` of boot delay.
- **Every failure returns `False` silently.** Verified across ten real modes:
  DNS failure, connection refused, TLS handshake failure, HTTP 500, HTTP 403
  (corporate block), socket timeout, captive-portal garbage bytes, malformed
  URL, network-unreachable, and disk-full on the stamp write.
- **Both filesystem probes are inside the guard.** `should_send()` and
  `payload()` touch the data home, so they run *inside* `send()`'s `try` — an
  unwritable home raises `PermissionError` from `config_dir()`, and a container
  whose UID has no passwd entry makes `Path.home()` raise **`RuntimeError`**
  (not `OSError`). Both are caught; the documented in-memory-id fallback is what
  engages.
- **`http.client.HTTPException` is named explicitly** in the except tuple: it
  subclasses `Exception` directly, so it is neither `OSError` nor `ValueError`.
  Without it, `http.client.InvalidURL` / `BadStatusLine` escaped into the daemon
  thread and `threading.excepthook` printed a traceback to gateway stderr on
  every boot.
- **Even an unexpected exception class is contained** — it dies inside the daemon
  thread and the main thread is unaffected.
- **`status()` never tracebacks.** A diagnostic has to be readable precisely when
  something is broken, so its probes are guarded too.

The one thing deliberately *not* swallowed is a stamp-write failure's
consequence: `_mark_sent()` runs only after a delivered request, so a failed send
retries later rather than silently losing the day.

### Default-ON with four suppressions

`telemetry.beacon_enabled` defaults **true** and gates the repo's only default-on
egress family: the heartbeat and official-app install receipts.
`telemetry_permitted()` suppresses both when `KIROCREW_TELEMETRY_DISABLED` is
truthy, an enterprise **governance ceiling** pins `capabilities.telemetry` off,
the config toggle is false, the process looks like **CI**, `KIROCREW_HOME` is
**non-default** (dev home / pod / worktree preview), or this install has never sent
and `dashboard.privacy_acked` is still false (the first-egress gate below).
`beacon.should_send()` adds the heartbeat-only daily throttle; receipts are
event-based and do not use it.

Each suppression carries a stable `Verdict.code` from `beacon.REASONS` alongside the
English `reason`, so the dashboard can translate the outcome instead of printing an
operator diagnostic.

### Enterprise opt-out: the `capabilities.telemetry` governance scope

The Settings toggle, the CLI and the env var are all **operator** controls: a user
on the machine can flip any of them, and so can the agent for the first two. A
managed fleet often may not egress to a vendor endpoint at all, which needs a
control the running app **cannot** undo. That is the `capabilities.telemetry`
`SCOPE_CATALOG` capability row (`capability_default=True`, so policy-absence keeps
the documented default-on behavior for standalone users).

It is read from the trust-root `security_policy.json`, which lives in
`security._SENSITIVE_HOME_DIRS` — the agent can neither read nor rewrite its own
ceiling, which is what makes this enterprise-*pinnable* where a `config.json` field
would only be a suggestion. Authoring is a normal capability block:

```json
{"version": 1, "boot": {"fail_closed": true},
 "capabilities": {"telemetry": {"enabled": false}}}
```

Enforced at **four** chokepoints — one send gate plus **every** write path to
`telemetry.beacon_enabled`, because any one alone is a half-control:

| Chokepoint | Behavior when pinned |
|---|---|
| `beacon.should_send()` | Refuses the send, reason `disabled by governance policy (capabilities.telemetry)` — ranked **above** the config flag so a managed host reports the policy, not the local value |
| `PATCH /api/config/kirocrew` | **403** on `telemetry.beacon_enabled=true`; writing `false` is always allowed (tightest-wins — a narrower local choice composes with the ceiling) |
| `kirocrew telemetry enable` | Exits **1** without writing config.json; `disable` still works |
| `kirocrew config set [--local] telemetry.beacon_enabled true` | Exits **1** without writing. Easy to miss: the *generic* setter reaches the same key, and `--local` writes `config.local.json`, which takes **precedence** over the base file — so leaving it ungated would make it the one remaining way to store `true` on a pinned host |

**Adding a fifth write path means adding a fifth gate.** The rule is that no path
may leave a pinned host storing `true`; `test_beacon.py::TestGenericConfigSetterIsGated`
and `test_config_patch.py` pin the existing ones.

Without the write refusals a pinned host could sit storing `beacon_enabled: true`
behind a toggle that does nothing — the same false-promise-on-a-privacy-control
failure the overlay check already guards against.

**Level-1 POLICY only.** The probe requires `layer == "policy"`, so a Level-2
*profile* pinning `capabilities.telemetry` does not suppress the beacon: the probe
runs from the boot thread with no session, and a bare not-permitted test would read
a transient deny-all-profile race as an administrator pin on an ungoverned host.
Full rationale in [governance.md](governance.md) → "Anonymous telemetry".

`beacon.is_governance_pinned_off()` is the public probe, surfaced as
`governance_override` on `GET /api/telemetry/beacon` and as the strongest of the
three "pinned" notes in the Privacy panel (it outranks the env-var and overlay
notes, which would otherwise offer remedies the ceiling makes pointless).

**It fails CLOSED** (`fail_closed=True`), like `capabilities.theme_install` and
`capabilities.publish`. The two dispositions look symmetric and are not: a wrong
DENY loses one heartbeat, but a wrong PERMIT **egresses from a fleet that
explicitly forbade egress** — breaking the exact promise the administrator was
given, on a payload that leaves the machine. `fail_closed` also makes
`governance_permits` audit the degrade as a critical SEL event, which is precisely
the condition under which a silent degrade-to-permit would be indefensible.

The probe distinguishes **two** failure sources, because conflating them produces a
different bug in each direction:

| Source | Identified by | Disposition |
|---|---|---|
| Evaluator could not answer (degrade, or the call/import itself died) | `reason` starts with `GOVERNANCE_ERROR_REASON` | **pinned** — fail closed |
| Evaluator answered, but from Level 2 | `layer == "profile"` | **not** a pin (see the policy-only note above) |

The second row is load-bearing: a transient deny-all-profile race on a host with no
policy at all arrives as an ordinary `Decision`, so no `except` can catch it, and
reading it as a pin would blame an administrator who does not exist.
`TestGovernancePin` pins all three outcomes.

**The enforcement decision is SEL-audited; the probe is not.** `should_send` passes
`audit_tool="beacon_send"`, routing through `vet_and_audit` so a suppressed
heartbeat lands a `governance_decision` record. `status()` passes `audit=False`
because it backs `GET /api/telemetry/beacon`, which the Privacy panel refetches —
auditing an inspection would flood the trail.

`is_default_home()` compares against `~/.kiro/crew` **directly, never against
`config_dir()`** — `config_dir()` *honors* `KIROCREW_HOME`, so comparing the two
always matches and the suppression would never fire (a real bug caught by
`TestDefaultHomeDetection`).

`kirocrew telemetry status | disable | enable` — `status` prints the exact
payload and never materializes an id (`install_id(create=False)`). Its numbered,
choose-one opt-out list leads with `kirocrew telemetry disable` because that choice
persists to `config.json` and survives a new shell. Each method is a separate visual
block: the environment override groups separately labelled macOS/Linux, PowerShell,
and Command Prompt syntax, followed by the equivalent config key.

`beacon.is_env_opted_out()` is the public probe for "the env var pins this off".
It exists so the dashboard can distinguish *off because the stored flag is false*
(a toggle can flip it) from *off because the environment says so* (a config write
would be accepted and then have no effect).

### In-product opt-out (Settings → Privacy toggle)

The GUI twin of `kirocrew telemetry disable`. It writes the **same** key —
`telemetry.beacon_enabled` via `PATCH /api/config/kirocrew` — so the two controls
cannot disagree and the choice survives restarts and upgrades.

- Only the **boolean** is dashboard-editable. `telemetry.beacon_endpoint` is
  deliberately absent from `_EDITABLE_CONFIG`: exposing it would let a dashboard
  caller redirect the heartbeat to an arbitrary host.
- `GET /api/telemetry/beacon` (`handlers/telemetry.py::api_beacon_status`) feeds
  the control. It returns the **stored** flag (`enabled`) *and* the **effective**
  verdict (`would_send` + `reason`), because the env var, a CI host, a
  non-default data home, or a `config.local.json` overlay each suppress sending
  independently of the flag. A privacy control that reads "on" while something
  else silences the beacon — or "off" while it still sends — is a false promise,
  so the panel states which one is in force.
- `env_override` reports specifically whether `KIROCREW_TELEMETRY_DISABLED` pins
  the state; when it does, the toggle is **disabled** rather than offering a
  write that cannot take effect.
- `overlay_override` does the same for a `config.local.json` entry.
  `config.local.json` deep-merges **over** `config.json` — the file the toggle
  writes — so an entry there would let the switch snap back to the overlay's
  value after a successful save. The endpoint reports it and the panel disables
  the toggle and names the file (the same case `kirocrew telemetry disable`
  detects and reports; see `cli_commands._telemetry`). The probe is best-effort:
  a missing, unreadable, or malformed overlay reports "not pinned", since
  `enabled` already carries the authoritative effective value.
- The handler routes `KiroCrewConfig.load()` and the overlay probe through
  `asyncio.to_thread` — both stat/read files, and this runs on the aiohttp event
  loop, where a synchronous read stalls every other request behind it.
- The endpoint is read-only, never materializes an install id
  (`status(create=False)`), does not return the id itself, and fails **toward
  off** on an unreadable config — a diagnostic must not 500, and must never claim
  telemetry is on when that cannot be proven.

### In-product privacy disclosure

The dashboard discloses the default-on beacon without turning disclosure into a
consent flow:

- The **first-run Privacy chapter** is the disclosure surface. There is no
  passive banner above the routed content: a dismissible strip competed with the
  chapter that already discloses the same thing, and its "Privacy details" link
  was the only thing it added.
- **Settings → Privacy** is the durable surface. It explains the
  default-on, at-most-daily beacon and its exact five fields: random
  installation id, app version, Python minor version, installation channel, and
  first-run flag. `PrivacyPanel.test.tsx` asserts both that all five are named and
  that the four **removed** fields are absent, so this copy cannot drift from
  `beacon.payload()` in either direction.
  It also states the excluded data, carries the **opt-out toggle** (above), and
  keeps the status/disable commands beneath it — the CLI remains documented
  because it is the only way to override a `config.local.json` overlay and the
  only control on a headless host.
- The **mandatory first-run Privacy chapter** (`components/PrivacyChapter.tsx`,
  chapter 2 of 3) shows the same disclosure and the same toggle in the
  `OnboardingChapterShell` layout, so a new user sees what is sent and can opt out
  before reaching the product. Every path out of chapter 1 routes through it,
  including "Skip all", and it offers no skip and no Escape. It is still **not a
  consent gate**: Continue is always enabled and never requires a choice, because
  the default is a decision the user can change here, in Settings → Privacy, or
  from the CLI.
- **The first heartbeat is withheld until that chapter is acknowledged.** The
  gateway starts the beacon thread at boot, before the dashboard has rendered, so
  an ungated fresh install would ping before the user could decline: an opt-out
  offered only after the fact. Continue persists `dashboard.privacy_acked` (and
  `kirocrew telemetry enable|disable` sets it too, for headless hosts), which
  `beacon.telemetry_permitted` reads. The gate is **first-egress only**
  (`beacon.is_first_send()`): an install that has already sent is past the
  disclosure, and keying every heartbeat on the flag would silence it permanently
  rather than once. `dashboard.privacy_acked` falls back to `dashboard.onboarded`,
  so a user who finished first run before the chapter existed is never re-gated.
  It gates the install receipt too, via the shared consent ladder.
- The disclosure copy and the control are single-sourced in
  `components/PrivacyDisclosure.tsx` (`PrivacyDisclosureSections`,
  `TelemetryToggle`, `PrivacyCommandList`) and consumed by both surfaces, so the
  first-run explanation and the durable panel cannot drift.
- The durable surface distinguishes local usage/context records from optional
  performance metrics. Performance metrics are off by default and remain local
  when enabled, with one explicit exception: they egress only when the operator
  configures an OTLP endpoint.
- The panel renders a **stable `reason_code`** (`beacon.REASONS`), never the
  sibling `reason` string. `reason` is untranslated operator prose
  (`already sent today (2026-08-04)`) kept for logs and bug reports; interpolating
  it put a developer diagnostic on screen in all 10 languages. An unrecognized
  code falls back to a generic translated note rather than rendering a raw key.
- These surfaces add no tracking. They delay exactly one thing, the first
  heartbeat, until the disclosure has been shown, and otherwise only explain
  behavior and controls that already exist.

### Official-app install receipt (`apps/install_receipt.py`)

A successful `install_from_registry` call emits one best-effort receipt only when
the selected row came from the bundled or edition-contributed official catalog.
The row's `_registry` marker is the authoritative negative discriminator: a
user-configured external registry row is refused before dispatch. Local-path
installs and self-registration never call the sender. This provenance gate keeps
private/corporate app names on the host.

The sender mirrors the beacon's posture: the same endpoint, the factored shared
consent ladder (`telemetry.beacon_enabled`, `KIROCREW_TELEMETRY_DISABLED`, the
`capabilities.telemetry` ceiling, CI/test suppression, and non-default-home
suppression), a detached daemon thread, a 5-second timeout, and silent failure.
It intentionally does **not** share the beacon's daily throttle because each
successful app install is a separate event.

The exact GET route is:

```text
/b/1/install/<app-slug>?t=<token>&k=<fresh|update>&v=<release>
```

| Location/field | Contract |
|----------------|----------|
| `<app-slug>` | Public official-catalog identifier in the path. No custom-source or local app slug is eligible. |
| `t` | First 32 hex characters of `HMAC-SHA256(key=receipt_secret, msg=b"app-install:" + app_slug)`, where `receipt_secret` is a 64-hex random secret generated on first use, stored owner-only as `app_receipt_secret` under the data home, and **never transmitted anywhere** — deliberately independent of the beacon install id, which the collector already holds from every heartbeat and could otherwise use to recompute tokens for public slugs and link one installation's receipts across apps. Deterministic for one installation and app, different across apps, and joinable neither into an installed-app profile nor to any heartbeat row. If the secret cannot be read or created, no receipt is sent. |
| `k` | Exactly `fresh` or `update`, derived from whether the app was installed before the successful call. Updates must not inflate adoption rank. |
| `v` | KiroCrew release normalized by `beacon.release()`; build stamps are removed. |

`test/test_install_receipt.py` mocks the network and pins every suppression,
token, URL, provenance, success-only, and fresh/update property. Beacon tests
remain the regression contract for the shared gate's original behavior.

#### Server-side `AppInstallations` rollup (infrastructure follow-up)

The telemetry-account owner must extend the existing CloudFront → S3 → Athena →
Lambda aggregation outside this repository. The product-side contract is:

1. Select only `/b/1/install/<slug>` rows with an official catalog slug, a
   32-character lowercase-hex `t`, `k` in `{fresh, update}`, and a normalized
   release `v`; retain the existing no-client-IP/no-User-Agent log projection.
2. For `k=fresh`, publish `AppInstallations` as the count of distinct `(app_slug,
   t)` pairs per UTC rollup window (equivalently `COUNT(DISTINCT t)` grouped by
   app slug). Duplicate delivery or reinstall from the same data home must not
   inflate the app's count.
3. Keep `k=update` in a separate update series; never add it to install rank.
   `v` may provide a bounded release breakdown, but is not part of uniqueness.
4. Feed only aggregate per-app results to catalog-ranking publication. Never
   expose or join raw tokens, and never join receipt rows to heartbeat ids.

The Athena query and aggregator Lambda live in the telemetry AWS account, so
that implementation and deployment are an explicit infra-account task outside
this repo and outside this PR.

### Cross-machine identity hazard

A snapshot restored onto a second machine must not clone the id — two hosts
sharing one would collapse into a single Daily Active Instance, and the copied
stamp would suppress the new host's sends.

This is guaranteed by **non-selection, NOT by a basename filter**, and the
distinction matters: `portability.EXPORT_EXCLUDE` and
`snapshot.NEVER_SNAPSHOT_FILES` are matched by BASENAME over the staged
`workspace/`, `plan_memory/` and `skills/` trees, so registering a beacon
filename there would silently drop any **user** file that happens to share the
name — real data loss on restore, in exchange for nothing. Root-level export
copies a hard-coded allowlist (`config.json`, `hooks.json`, `crons.json`,
`notifications.jsonl`, `project_dir`, `workspace_dir`) and snapshot staging
copies an explicit per-component list (`CORE_FILES`); neither names a beacon
file, so the root paths are never selected in the first place.

`TestSnapshotAndPortabilityRegistration` pins the whole property in both
directions: the names are absent from both filters, absent from the export
allowlist and `CORE_FILES`, and a workspace file sharing either name survives.

### Bounded, symlink-safe state reads

All beacon state goes through `_read_state()`, never `Path.read_text()`, with
three guards:

1. **Regular files only** (`lstat` + `S_ISREG`). `read_text` FOLLOWS symlinks, so
   a link at `beacon_install_id` pointing at `/dev/zero` made the read allocate
   unboundedly until OOM — inside the gateway's beacon thread. The check also
   rejects FIFOs (whose `open()` blocks forever) and device nodes, without ever
   opening them.
2. **Bounded length** (`_MAX_STATE_BYTES`, 4 KiB). Even a regular file can be
   enormous if a log is rotated onto the name.
3. **Lenient decode** (`errors="replace"`). A strict decode raises
   `UnicodeDecodeError`, which is a `ValueError` and **not** an `OSError`, so it
   escaped the callers' handlers and killed `kirocrew telemetry status`.

Anything unreadable returns `""`, which every caller already treats as
absent/corrupt — so the id regenerates rather than merely not crashing. The
`/dev/zero` and FIFO tests use a real thread timeout, because the failure mode is
"never returns", which a plain assertion cannot catch.

### Server side (account 116101834266, us-west-2)

Zero application code — the access log **is** the data product:

```
client ─GET /b/1/<id>?v&py&dist&first_seen─> CloudFront E1YM983XX3ASBM
                                     │ CloudFront Function returns 204 at the edge
                                     ▼
        standard logging v2 → s3://kirocrew-beacon-logs (PERMANENT, tiered)
                                     ▼
              Athena kirocrew_analytics.beacon_logs (partition projection)
                                     ▼
        kirocrew-beacon-aggregator Lambda (daily 00:20 UTC) writes BOTH:
                    ├── CloudWatch KiroCrew/Product  → dashboard (~15-month view)
                    └── kirocrew_analytics.beacon_daily → PERMANENT record
```

**Metrics published.** `DailyActiveInstances`, `BeaconPings`, `NewInstallations`,
`ActiveByVersion`, `ActiveByPython`, `ActiveByDistribution`, and `ActiveByCountry`.
`ActiveByChannel` / `ActiveByOS` / `ActiveByArch` were removed with their source
fields; their historical CloudWatch points and rollup rows are left in place (the
data is real for the days it covers — deleting it would be rewriting history to
match a current schema).

**`country` is retained, and is the one field the client does not send.** It is
derived at the CloudFront edge and is coarse (a 2-letter code); the IP it comes
from is never written to storage, since the log delivery does not select `c-ip` at
all. Dropping it would mean removing `c-country` from the delivery's
`recordFields`, which shifts every column in the TSV — the Glue table's six columns
are positional, so new log lines would silently misparse (the `status = '204'`
filter would match nothing) until the table was migrated. Keeping the least
identifying field in the set was the better trade than a schema migration on a live
pipeline.

### Retention: S3/Athena is permanent, CloudWatch is a 15-month view

**CloudWatch cannot be the durable store.** Metric data is retained for at most
**15 months** and expires on a **rolling** basis (1-min → 15 days, 5-min → 63
days, 1-hour → 455 days), and that ceiling is not configurable. So the dashboard
is inherently a ~15-month window, by AWS design rather than by our choice.

The permanent record is therefore two things in S3:

- **Raw logs** — `s3://kirocrew-beacon-logs`. The lifecycle policy has **no
  `Expiration` on any rule**; objects only *transition* (Standard → Standard-IA
  at 90d → Glacier Instant Retrieval at 365d) to cut cost. Glacier **Flexible
  Retrieval / Deep Archive are deliberately avoided** — they require an async
  restore before a read, which would silently break the long-range Athena
  queries this design exists to support. Versioning is on; only *noncurrent*
  versions are pruned (365d).
- **Daily rollup** — `kirocrew_analytics.beacon_daily` (Parquet, stays in
  Standard forever). One small row per `(day, metric, dimension, value)`. This
  is what makes "permanent" *useful*: raw logs grow linearly forever, so a
  multi-year dashboard query would scan every line ever written, while the
  rollup keeps such queries fast and cheap.

`_persist_rollup()` is **idempotent** — it deletes the target day's rows before
inserting, so a backfill or a retry after a partial failure cannot double-count.
It runs **last** in the handler, after the CloudWatch puts, because CloudWatch is
best-effort presentation while the rollup is the durable store: a rollup failure
must surface as a Lambda invocation error (visible on the dashboard's error
widget) rather than being masked by an otherwise-successful metric write.

**Destructive-rewrite guard (do not remove).** That same idempotent delete makes
an empty query result *destructive*: it rewrites a day to nothing. A `if not
facts` check is **not** sufficient, because `DailyActiveInstances`, `BeaconPings`
and `NewInstallations` are appended **unconditionally** — as zeros — so an empty
day still reaches the delete with three all-zero rows.

This is not hypothetical: on **2026-07-31** the scheduled 00:20 UTC run queried
`day=30`, a partition whose logs did not exist (the feature shipped at 02:36 UTC
that morning, and the first delivered log object was `2026-07-31-04`). The run
reported `SUCCEEDED` with no error, published zeros, and **wiped the rollup to
zero rows** — the durable record was destroyed by a "successful" invocation. The
guard now skips the rewrite unless at least one fact is non-zero (an all-zero day
carries no information, so skipping is lossless), and an empty partition logs an
explicit `WARNING` naming the partition, because a silent success was what made
the failure hard to see.

**Object Lock is deliberately NOT enabled.** It would make the logs literally
undeletable, which sounds like "permanent" but is the wrong trade for a
privacy-sensitive dataset: it would also remove our own ability to purge after an
operator mistake, a schema error, or a future deletion obligation. The goal is
"retained indefinitely by policy", not "physically impossible to delete".

**The aggregator cannot delete the permanent record.** Its IAM policy grants
`s3:PutObject`/`s3:DeleteObject` on `kirocrew-beacon-logs/rollup/*` **only** —
raw-log access is read-only, so the component that consumes the history has no
permission to destroy it.

**No client IP is ever stored.** The log delivery's `recordFields` selects only
`date`, `time`, `cs-uri-stem`, `cs-uri-query`, `c-country`, `sc-status` —
`c-ip`, `x-forwarded-for`, User-Agent and Cookie are simply not delivered.
Verified against a real delivered log file. This is why the design uses a
Lambda-free CDN log path rather than CDN logging with default fields, and it is
what makes the "no IP" claim structural rather than a promise.

**Aggregator timestamp rule (load-bearing):** metrics are stamped at the
**end** of the target day, clamped to `now - 1min`. CloudWatch accepts
timestamps up to two weeks old but points **24h+ old can take 48 hours** to
become queryable, while <3h old are near-immediate. Stamping midnight-of-
yesterday from an 02:30 run put every point in the 48-hour bucket and the
dashboard rendered **empty** despite the data being accepted; the clamp is
needed because a same-day backfill's 23:59 is in the future and
`PutMetricData` rejects >2h ahead. Both failure modes were hit in development.

**Model CDN.** `embeddings.py::_DEFAULT_MODEL_URL` points at
`kirocrew-models` (distribution E2UX23B48LKM6V, OAC-only bucket access). The
`_GGUF_SHA256` pin remains the sole integrity gate, so a tampered CDN object can
only fail verification. Because `_ensure_downloaded()` returns early when
`model_ready()`, a CDN request is a **first-install** signal, not a DAU signal —
the dashboard shows it as "downloads", deliberately separate from DAI.
